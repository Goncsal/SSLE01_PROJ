"""The scorer: stores events, runs the rules, writes alerts (section 4.4).

`Scorer.ingest(event)` is the whole pipeline for one `FraudEvent`; `record_grid_sample()`
feeds the feeder-meter energy. Both return the alerts they wrote, each with the action
it asks for, so the caller (the HTTP layer, the tests) decides when to run them.
"""

from __future__ import annotations

import collections
import datetime as dt
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass

from evcommon import jsonlog, metrics
from evcommon.models import FraudAlert, FraudEvent
from sqlalchemy import delete, func, select

from . import rules
from .db import AlertRow, CardEventRow, StationStatRow
from .rules import CardEv, Finding
from .settings import Settings

EVENTS = metrics.make(metrics.FRAUD_EVENTS_TOTAL)
ALERTS = metrics.make(metrics.FRAUD_ALERTS_TOTAL)
HIGH_RISK = metrics.make(metrics.FRAUD_HIGH_RISK_CARDS)

PRUNE_AFTER_H = 48
GeoLookup = Callable[[str], "tuple[float, float] | None"]


@dataclass
class Raised:
    """An alert that was written, and what it asks for."""
    alert: FraudAlert
    finding: Finding


def noisy_or(scores: list[float]) -> float:
    out = 1.0
    for s in scores:
        out *= 1 - min(max(s, 0.0), 1.0)
    return round(1 - out, 4)


def _alert_from_row(r: AlertRow) -> FraudAlert:
    return FraudAlert(alert_id=r.alert_id, ts=r.ts, rule=r.rule, severity=r.severity, card=r.card,
                      hier_id=r.hier_id, other_hier_id=r.other_hier_id, city=r.city, score=r.score,
                      detail=r.detail, evidence=r.evidence or {})


class Scorer:
    def __init__(self, sessions, settings: Settings, geo: GeoLookup):
        self.sessions = sessions
        self.cfg = settings
        self.geo = geo                    # station hier-id -> (lat, lon) or None
        self.lock = threading.Lock()
        # real-time power samples per station (time, feeder W, gap W); memory only: the window is minutes
        self._power: dict[str, collections.deque] = {}

    # ---- ingestion -------------------------------------------------------------------------------

    def ingest(self, ev: FraudEvent) -> list[Raised]:
        EVENTS.labels(event=ev.event).inc()
        with self.lock, self.sessions() as db:
            findings: list[Finding] = []
            station = rules.station_of(ev.hier_id)
            if ev.event == "session.start":
                findings += self._on_start(db, ev, station)
            elif ev.event == "session.stop":
                findings += self._on_stop(db, ev, station)
            else:
                f = rules.meter_backwards(hier_id=ev.hier_id, city=ev.city, card=ev.card,
                                          transaction_id=ev.transaction_id, reason=ev.reason,
                                          max_kw=ev.max_kw)
                if f:
                    findings.append(f)
            raised = [r for f in findings if (r := self._raise(db, f, ev.ts))]
            db.commit()
            self._refresh_high_risk(db, ev.ts)
            return raised

    def _on_start(self, db, ev: FraudEvent, station: str) -> list[Finding]:
        if not ev.card:
            return []
        coords = self.geo(station)
        lat, lon = coords if coords else (None, None)
        since = ev.ts - dt.timedelta(hours=self.cfg.window_h)
        prior = [self._card_ev(r) for r in db.scalars(
            select(CardEventRow).where(CardEventRow.card == ev.card, CardEventRow.kind == "start",
                                       CardEventRow.ts >= since, CardEventRow.ts <= ev.ts))]
        stopped = frozenset(t for t in db.scalars(
            select(CardEventRow.transaction_id).where(
                CardEventRow.card == ev.card, CardEventRow.kind == "stop", CardEventRow.ts >= since,
                CardEventRow.ts <= ev.ts, CardEventRow.transaction_id.is_not(None))))
        db.add(CardEventRow(ts=ev.ts, kind="start", card=ev.card, city=ev.city, hier_id=ev.hier_id,
                            lat=lat, lon=lon, transaction_id=ev.transaction_id))
        db.flush()
        here = CardEv(ev.ts, ev.card, ev.city, ev.hier_id, lat, lon, None, ev.transaction_id)
        out = []
        for f in (rules.impossible_travel(here, prior, self.cfg, stopped),
                  rules.card_velocity(card=ev.card, city=ev.city, hier_id=ev.hier_id,
                                      starts=[p.ts for p in prior] + [ev.ts], now=ev.ts, cfg=self.cfg)):
            if f:
                out.append(f)
        return out

    def _on_stop(self, db, ev: FraudEvent, station: str) -> list[Finding]:
        out: list[Finding] = []
        f = rules.over_energy(hier_id=ev.hier_id, city=ev.city, card=ev.card,
                              transaction_id=ev.transaction_id, energy_wh=ev.energy_wh,
                              duration_s=ev.duration_s, max_kw=ev.max_kw, cfg=self.cfg)
        if f:
            out.append(f)
        if ev.card:
            db.add(CardEventRow(ts=ev.ts, kind="stop", card=ev.card, city=ev.city, hier_id=ev.hier_id,
                                energy_wh=ev.energy_wh, transaction_id=ev.transaction_id))
        if ev.energy_wh is not None:
            db.add(StationStatRow(ts=ev.ts, city=ev.city, station_id=station,
                                  reported_wh=float(ev.energy_wh), sessions=1))
            db.flush()
            out += self._station_rules(db, station, ev.city, ev.ts)
        return out

    def record_grid_sample(self, station_id: str, city: str, power_w: float, seconds: float,
                           ts: dt.datetime | None = None, gap_w: float | None = None) -> list[Raised]:
        """Add the feeder-meter energy of one polling interval and re-run the station rules.

        `gap_w` is the instantaneous feeder-minus-reported power (None = unknown: power_gap
        gets no sample)."""
        ts = ts or dt.datetime.now(dt.UTC)
        with self.lock, self.sessions() as db:
            db.add(StationStatRow(ts=ts, city=city, station_id=station_id, grid_power_w=power_w,
                                  grid_wh=max(power_w, 0.0) * seconds / 3600))
            db.flush()
            findings = self._station_rules(db, station_id, city, ts, peers=False)
            if gap_w is not None:
                dq = self._power.setdefault(station_id, collections.deque())
                dq.append((ts, power_w, gap_w))
                horizon = ts - dt.timedelta(seconds=self.cfg.power_gap_window_s)
                while dq and dq[0][0] <= horizon:
                    dq.popleft()
                f = rules.power_gap(station_id=station_id, city=city, samples=list(dq), now=ts,
                                    cfg=self.cfg)
                if f:
                    findings.append(f)
            raised = [r for f in findings if (r := self._raise(db, f, ts))]
            db.commit()
            return raised

    def _station_rules(self, db, station: str, city: str, now: dt.datetime, peers: bool = True):
        since = now - dt.timedelta(hours=self.cfg.window_h)
        out = []
        def totals(at: dt.datetime) -> tuple[float, float, int]:
            """Reported energy and sessions up to `at`; feeder energy up to `at` minus the settle lag."""
            lo = at - dt.timedelta(hours=self.cfg.window_h)
            rep, n = db.execute(
                select(func.coalesce(func.sum(StationStatRow.reported_wh), 0.0),
                       func.coalesce(func.sum(StationStatRow.sessions), 0))
                .where(StationStatRow.station_id == station, StationStatRow.ts >= lo,
                       StationStatRow.ts <= at)).one()
            grid = db.scalar(
                select(func.coalesce(func.sum(StationStatRow.grid_wh), 0.0))
                .where(StationStatRow.station_id == station, StationStatRow.ts >= lo,
                       StationStatRow.ts <= at - dt.timedelta(seconds=self.cfg.grid_settle_s)))
            return rep, grid, n

        reported, grid, sessions = totals(now)
        p_rep, p_grid, _ = totals(now - dt.timedelta(seconds=self.cfg.grid_sustain_s))
        f = rules.grid_gap(station_id=station, city=city, reported_wh=reported, grid_wh=grid,
                           sessions=sessions, prior_reported_wh=p_rep, prior_grid_wh=p_grid,
                           cfg=self.cfg)
        if f:
            out.append(f)
        if peers:
            base = (StationStatRow.sessions > 0, StationStatRow.ts >= since, StationStatRow.ts <= now,
                    StationStatRow.city == city)
            own = list(db.scalars(select(StationStatRow.reported_wh)
                                  .where(StationStatRow.station_id == station, *base)))
            peer = list(db.scalars(select(StationStatRow.reported_wh)
                                   .where(StationStatRow.station_id != station, *base)))
            f = rules.peer_deviation(station_id=station, city=city, own_wh=own, peer_wh=peer,
                                     cfg=self.cfg)
            if f:
                out.append(f)
        return out

    @staticmethod
    def _card_ev(r: CardEventRow) -> CardEv:
        return CardEv(r.ts, r.card, r.city, r.hier_id, r.lat, r.lon, r.energy_wh, r.transaction_id)

    # ---- alerts ----------------------------------------------------------------------------------

    def _raise(self, db, f: Finding, now: dt.datetime) -> Raised | None:
        """Write the alert if the score reaches the threshold and it is not a repeat."""
        if f.score < self.cfg.alert_score:
            return None
        recent = db.scalar(select(func.count()).select_from(AlertRow).where(
            AlertRow.dedup_key == f.dedup_key,
            AlertRow.ts > now - dt.timedelta(seconds=f.cooldown_s)))
        if recent:
            return None
        alert = FraudAlert(alert_id=str(uuid.uuid4()), ts=now, rule=f.rule, severity=f.severity,
                           card=f.card, hier_id=f.hier_id, other_hier_id=f.other_hier_id, city=f.city,
                           score=f.score, detail=f.detail, evidence=f.evidence)
        db.add(AlertRow(alert_id=alert.alert_id, ts=alert.ts, rule=alert.rule, severity=alert.severity,
                        card=alert.card, hier_id=alert.hier_id, other_hier_id=alert.other_hier_id,
                        city=alert.city, score=alert.score, detail=alert.detail,
                        evidence=alert.evidence, dedup_key=f.dedup_key))
        ALERTS.labels(city=f.city or "unknown", rule=f.rule).inc()
        ev = f.evidence
        jsonlog.get_logger().event(
            "fraud.alert", f.detail, level="warning", rule=f.rule, score=f.score, card=f.card,
            hier_id=f.hier_id, other_hier_id=f.other_hier_id, severity=f.severity,
            distance_km=ev.get("distance_km"), speed_kmh=ev.get("speed_kmh"),
            energy_wh=ev.get("energy_wh"), max_kw=ev.get("max_kw"), alert_id=alert.alert_id,
            # what the service will do with it, and whether it is allowed to: Wazuh's active
            # responses key off these two so a dry run suppresses them as well
            auto_action=f.action or "none", dry_run=self.cfg.dry_run)
        return Raised(alert, f)

    def _refresh_high_risk(self, db, now: dt.datetime) -> None:
        HIGH_RISK.set(sum(1 for s in self._card_scores(db, now).values() if s >= self.cfg.alert_score))

    def _recent_alerts(self, db, now: dt.datetime, **where):
        q = select(AlertRow).where(AlertRow.ts > now - dt.timedelta(hours=self.cfg.window_h))
        for k, v in where.items():
            q = q.where(getattr(AlertRow, k) == v)
        return list(db.scalars(q))

    def _card_scores(self, db, now: dt.datetime) -> dict[str, float]:
        by_card: dict[str, list[float]] = {}
        for r in self._recent_alerts(db, now):
            if r.card:
                by_card.setdefault(r.card, []).append(r.score)
        return {c: noisy_or(s) for c, s in by_card.items()}

    # ---- queries ---------------------------------------------------------------------------------

    def list_alerts(self, since: dt.datetime | None, card: str | None, hier_id: str | None,
                    limit: int) -> list[FraudAlert]:
        with self.sessions() as db:
            q = select(AlertRow).order_by(AlertRow.ts.desc(), AlertRow.alert_id).limit(limit)
            if since:
                q = q.where(AlertRow.ts >= since)
            if card:
                q = q.where(AlertRow.card == card)
            if hier_id:
                q = q.where(AlertRow.hier_id == hier_id)
            return [_alert_from_row(r) for r in db.scalars(q)]

    def card_score(self, card: str) -> dict:
        now = dt.datetime.now(dt.UTC)
        with self.sessions() as db:
            rows = self._recent_alerts(db, now, card=card)
            last = db.scalar(select(func.max(CardEventRow.ts)).where(CardEventRow.card == card))
        return {"card": card, "score": noisy_or([r.score for r in rows]),
                "rules": sorted({r.rule for r in rows}), "last_seen": last}

    def charger_score(self, hier_id: str) -> dict:
        """Score of a charger: alerts about it, or about its station."""
        now = dt.datetime.now(dt.UTC)
        station = rules.station_of(hier_id)
        with self.sessions() as db:
            rows = [r for r in self._recent_alerts(db, now) if r.hier_id in (hier_id, station)
                    or r.other_hier_id == hier_id]
        return {"hier_id": hier_id, "score": noisy_or([r.score for r in rows]),
                "rules": sorted({r.rule for r in rows})}

    def card_transactions(self, card: str, now: dt.datetime) -> list[int]:
        """Transaction ids of the card's recent sessions (for flagging after a block)."""
        since = now - dt.timedelta(hours=self.cfg.window_h)
        with self.sessions() as db:
            ids = db.scalars(select(CardEventRow.transaction_id).where(
                CardEventRow.card == card, CardEventRow.kind == "start",
                CardEventRow.ts >= since, CardEventRow.transaction_id.is_not(None))
                .order_by(CardEventRow.ts))
            return sorted(set(ids))

    def station_transactions(self, station: str, now: dt.datetime) -> list[int]:
        """Transaction ids of sessions started at the station in the window (for flagging)."""
        since = now - dt.timedelta(hours=self.cfg.window_h)
        with self.sessions() as db:
            ids = db.scalars(select(CardEventRow.transaction_id).where(
                CardEventRow.hier_id.like(f"{station}.%"), CardEventRow.kind == "start",
                CardEventRow.ts >= since, CardEventRow.transaction_id.is_not(None))
                .order_by(CardEventRow.ts))
            return sorted(set(ids))

    def prune(self, now: dt.datetime | None = None) -> None:
        cutoff = (now or dt.datetime.now(dt.UTC)) - dt.timedelta(hours=PRUNE_AFTER_H)
        with self.lock, self.sessions() as db:
            db.execute(delete(CardEventRow).where(CardEventRow.ts < cutoff))
            db.execute(delete(StationStatRow).where(StationStatRow.ts < cutoff))
            db.commit()
