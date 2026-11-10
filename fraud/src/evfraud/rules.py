"""The six rules of section 4.4 as pure functions.

Each rule takes plain values (what the engine loaded from the database) and returns a
`Finding` or None. Nothing here touches the database, the clock or the network, so the
tests call the rules directly. A finding becomes an alert when its score reaches
`FRAUD_ALERT_SCORE`; the engine does that.

Weights (the score a rule adds when it fires):

    impossible_travel 0.95   critical  block_card only at high confidence (overlap or >= block_kmh)
    power_gap         0.90   critical  quarantine (sustained real-time feeder-vs-reported gap)
    grid_gap          0.75   warning   - (6 h cumulative ratio: alert only, honest variance is wide)
    over_energy       0.75   warning   flag_session
    meter_backwards   0.75   warning   flag_session
    peer_deviation    0.70   warning   -
    card_velocity     0.70   warning   -
"""

from __future__ import annotations

import datetime as dt
import statistics
from dataclasses import dataclass, field

from .geo import haversine_km
from .settings import Settings

WEIGHTS = {
    "impossible_travel": 0.95,
    "power_gap": 0.90,
    "grid_gap": 0.75,
    "over_energy": 0.75,
    "meter_backwards": 0.75,
    "peer_deviation": 0.70,
    "card_velocity": 0.70,
}
MIN_OWN_SESSIONS = 3          # peer_deviation: sessions of the station itself
GRID_MIN_WH = 1000.0          # grid_gap: ignore windows where the feeder barely moved
MIN_DT_H = 1 / 3600           # speeds are computed with at least one second
BACKWARDS_REASONS = ("backwards", "stop_mismatch")


@dataclass(frozen=True)
class CardEv:
    """A stored card event (start or stop)."""
    ts: dt.datetime
    card: str
    city: str
    hier_id: str
    lat: float | None = None
    lon: float | None = None
    energy_wh: int | None = None
    transaction_id: int | None = None


@dataclass
class Finding:
    rule: str
    severity: str
    score: float
    detail: str
    card: str | None = None
    hier_id: str | None = None
    other_hier_id: str | None = None
    city: str | None = None
    evidence: dict = field(default_factory=dict)
    action: str | None = None            # block_card | flag_session | quarantine | None
    dedup_key: str = ""
    cooldown_s: float = 600.0
    transaction_id: int | None = None


def station_of(hier_id: str) -> str:
    """pt.porto.st-001.cp-1 -> pt.porto.st-001 (a station id stays as it is)."""
    parts = hier_id.split(".")
    return ".".join(parts[:3])


def impossible_travel(start: CardEv, prior: list[CardEv], cfg: Settings,
                      stopped: frozenset[int] | set[int] = frozenset()) -> Finding | None:
    """The same card starts sessions in two cities less than distance / max_kmh hours apart.

    `prior` holds the card's earlier session starts (the engine limits them to the window) and
    `stopped` the transaction ids of the card's sessions that have already stopped.

    Detection and response are separate. Any implied speed above `max_kmh` is an ALERT. The
    card is only auto-BLOCKED (`action="block_card"`) on high confidence, because honest
    roaming sits right above the alert speed:

    * the sessions overlap: an earlier session in another city has not stopped (and is younger
      than `concurrent_max_age_s`, so a lost stop event does not look like a clone), the card is
      charging in two places at once; or
    * the implied speed is at least `block_kmh`, far beyond what a drive plus a margin explains.

    A start in a second city on its own is not a finding.
    """
    best: tuple[bool, float, float, float, CardEv] | None = None   # concurrent, speed, km, hours, prior
    for p in prior:
        if p.city == start.city or None in (p.lat, p.lon, start.lat, start.lon):
            continue
        gap_s = (start.ts - p.ts).total_seconds()
        hours = max(abs(gap_s) / 3600, MIN_DT_H)
        km = haversine_km(p.lat, p.lon, start.lat, start.lon)
        speed = km / hours
        concurrent = (p.transaction_id is not None and p.transaction_id not in stopped
                      and 0 <= gap_s <= cfg.concurrent_max_age_s)
        if not concurrent and speed <= cfg.max_kmh:
            continue
        if best is None or (concurrent, speed) > (best[0], best[1]):
            best = (concurrent, speed, km, hours, p)
    if best is None:
        return None
    concurrent, speed, km, hours, p = best
    high = concurrent or speed >= cfg.block_kmh
    why = "overlapping sessions" if concurrent else f"{speed:.0f} km/h >= {cfg.block_kmh:.0f}" if high else "alert only"
    return Finding(
        rule="impossible_travel", severity="critical", score=WEIGHTS["impossible_travel"],
        card=start.card, hier_id=start.hier_id, other_hier_id=p.hier_id, city=start.city,
        detail=(f"card {start.card} started a session in {p.city} and {hours * 60:.1f} min later in "
                f"{start.city}: {km:.0f} km at {speed:.0f} km/h (alert above {cfg.max_kmh:.0f}, "
                f"block at {cfg.block_kmh:.0f} or overlap): {why}"),
        evidence={"distance_km": round(km, 1), "speed_kmh": round(speed, 1),
                  "minutes_apart": round(hours * 60, 2), "max_kmh": cfg.max_kmh,
                  "block_kmh": cfg.block_kmh, "concurrent": concurrent, "high_confidence": high,
                  "from_city": p.city, "to_city": start.city,
                  "transaction_ids": [t for t in (p.transaction_id, start.transaction_id) if t]},
        action="block_card" if high else None,
        # separate keys: an alert-only hit must not mute the block-worthy one that follows
        dedup_key=f"impossible_travel:{start.card}:{'block' if high else 'alert'}", cooldown_s=900.0,
        transaction_id=start.transaction_id)


def over_energy(*, hier_id: str, city: str, card: str | None, transaction_id: int | None,
                energy_wh: int | None, duration_s: float | None, max_kw: float | None,
                cfg: Settings) -> Finding | None:
    """A stopped session delivered more than max_kw x duration x tolerance."""
    if energy_wh is None or duration_s is None or not max_kw:
        return None
    limit = max_kw * (duration_s / 3600) * 1000 * cfg.energy_tol
    if energy_wh <= limit:
        return None
    return Finding(
        rule="over_energy", severity="warning", score=WEIGHTS["over_energy"], card=card,
        hier_id=hier_id, city=city, transaction_id=transaction_id, action="flag_session",
        detail=(f"session {transaction_id} reported {energy_wh} Wh in {duration_s:.0f} s on a "
                f"{max_kw:g} kW charger (limit {limit:.0f} Wh)"),
        evidence={"energy_wh": energy_wh, "limit_wh": round(limit, 1), "max_kw": max_kw,
                  "duration_s": duration_s, "transaction_id": transaction_id},
        dedup_key=f"over_energy:{hier_id}:{transaction_id}", cooldown_s=86400.0)


def meter_backwards(*, hier_id: str, city: str, card: str | None, transaction_id: int | None,
                    reason: str | None, max_kw: float | None = None,
                    cfg: Settings | None = None) -> Finding | None:
    """A meter.anomaly whose reason is backwards or stop_mismatch (the register went down)."""
    if reason not in BACKWARDS_REASONS:
        return None
    return Finding(
        rule="meter_backwards", severity="warning", score=WEIGHTS["meter_backwards"], card=card,
        hier_id=hier_id, city=city, transaction_id=transaction_id, action="flag_session",
        detail=f"charger {hier_id} meter anomaly '{reason}' in session {transaction_id}",
        evidence={"reason": reason, "transaction_id": transaction_id, "max_kw": max_kw},
        dedup_key=f"meter_backwards:{hier_id}:{transaction_id}", cooldown_s=300.0)


def peer_deviation(*, station_id: str, city: str, own_wh: list[float], peer_wh: list[float],
                   cfg: Settings) -> Finding | None:
    """A station's mean energy per session is below peer_factor x the city median.

    `peer_wh` is the energy of every session in the window at the city's other stations;
    at least `min_sessions` of them are needed, and a few sessions of the station itself.
    """
    if len(own_wh) < MIN_OWN_SESSIONS or len(peer_wh) < cfg.min_sessions:
        return None
    median = statistics.median(peer_wh)
    mean = statistics.fmean(own_wh)
    if median <= 0 or mean >= cfg.peer_factor * median:
        return None
    return Finding(
        rule="peer_deviation", severity="warning", score=WEIGHTS["peer_deviation"],
        hier_id=station_id, city=city,
        detail=(f"station {station_id} averages {mean:.0f} Wh per session, "
                f"{mean / median:.0%} of the {city} median ({median:.0f} Wh)"),
        evidence={"mean_wh": round(mean, 1), "city_median_wh": round(median, 1),
                  "ratio": round(mean / median, 3), "own_sessions": len(own_wh),
                  "peer_sessions": len(peer_wh), "peer_factor": cfg.peer_factor},
        dedup_key=f"peer_deviation:{station_id}", cooldown_s=3600.0)


def grid_gap(*, station_id: str, city: str, reported_wh: float, grid_wh: float,
             sessions: int = 0, prior_reported_wh: float | None = None,
             prior_grid_wh: float | None = None, cfg: Settings) -> Finding | None:
    """Energy the CSMS saw is below grid_factor x what the feeder meter measured, for good reason.

    ALERT ONLY. The cumulative ratio is too wide among honest stations (0.71-1.38 live, booking
    at session stop against a continuously integrating feeder) to isolate anything on its own,
    and honest history dilutes a real tamper; the quarantine trigger is `power_gap`.

    The CSMS books a session's energy only when it stops while the feeder meter integrates
    continuously, so a station that is simply charging reads low. Three guards separate that
    timing lag from under-reporting:

    * enough completed sessions (`grid_min_sessions`) and reported energy
      (`grid_min_reported_wh`) in the window: with none there is nothing to compare;
    * the caller passes `grid_wh` with the newest `grid_settle_s` of feeder energy left out
      (a session that has not stopped yet is not held against the reported total);
    * the same test, taken `grid_sustain_s` earlier (`prior_*`), is under the factor too, so a
      gap that only exists at this instant, and closes as sessions stop, does not fire (and
      the station was already booking energy then).
    """
    if sessions < cfg.grid_min_sessions or reported_wh < cfg.grid_min_reported_wh:
        return None

    def low(rep: float | None, grid: float | None) -> bool:
        return (rep is not None and grid is not None and grid >= GRID_MIN_WH
                and rep >= cfg.grid_min_reported_wh and rep < cfg.grid_factor * grid)

    if not low(reported_wh, grid_wh) or not low(prior_reported_wh, prior_grid_wh):
        return None
    return Finding(
        rule="grid_gap", severity="warning", score=WEIGHTS["grid_gap"], hier_id=station_id,
        city=city, action=None,
        detail=(f"station {station_id} reported {reported_wh:.0f} Wh against {grid_wh:.0f} Wh on the "
                f"feeder meter ({reported_wh / grid_wh:.0%}, limit {cfg.grid_factor:.0%}), "
                f"sustained over {cfg.grid_sustain_s / 60:.0f} min, {sessions} sessions"),
        evidence={"reported_wh": round(reported_wh, 1), "grid_wh": round(grid_wh, 1),
                  "ratio": round(reported_wh / grid_wh, 3), "grid_factor": cfg.grid_factor,
                  "window_h": cfg.window_h, "sessions": sessions,
                  "grid_settle_s": cfg.grid_settle_s, "grid_sustain_s": cfg.grid_sustain_s,
                  "prior_ratio": round(prior_reported_wh / prior_grid_wh, 3)},
        dedup_key=f"grid_gap:{station_id}", cooldown_s=3600.0)


def power_gap(*, station_id: str, city: str, samples: list[tuple[dt.datetime, float, float]],
              now: dt.datetime, cfg: Settings) -> Finding | None:
    """Feeder power exceeds the reported power by a clear margin, sustained (tamper signature).

    `samples` are (time, feeder watts, gap watts) with gap = feeder - reported, the signal
    behind the MeterUnderReporting alert. Only samples where the station is actually charging
    (feeder >= `power_gap_w`) count. In the last `power_gap_window_s` there must be at least
    `power_gap_min_samples` of them and `power_gap_frac` of them must show a gap of at least
    `power_gap_w` watts AND `power_gap_ratio` of the feeder power. Honest stations read
    feeder == reported in real time (gap 0), so cumulative booking lag never matters here.
    """
    lo = now - dt.timedelta(seconds=cfg.power_gap_window_s)
    active = [(f, g) for t, f, g in samples if lo < t <= now and f >= cfg.power_gap_w]
    if len(active) < cfg.power_gap_min_samples:
        return None
    gapped = [(f, g) for f, g in active if g >= cfg.power_gap_w and g >= cfg.power_gap_ratio * f]
    if len(gapped) < cfg.power_gap_frac * len(active):
        return None
    mean_feeder = statistics.fmean(f for f, _ in gapped)
    mean_gap = statistics.fmean(g for _, g in gapped)
    return Finding(
        rule="power_gap", severity="critical", score=WEIGHTS["power_gap"], hier_id=station_id,
        city=city, action="quarantine",
        detail=(f"station {station_id}: feeder power exceeds reported power by {mean_gap:.0f} W "
                f"({mean_gap / mean_feeder:.0%} of {mean_feeder:.0f} W) in {len(gapped)} of {len(active)} "
                f"charging samples over {cfg.power_gap_window_s / 60:.0f} min"),
        evidence={"mean_gap_w": round(mean_gap, 1), "mean_feeder_w": round(mean_feeder, 1),
                  "gap_ratio": round(mean_gap / mean_feeder, 3), "samples": len(active),
                  "gapped_samples": len(gapped), "window_s": cfg.power_gap_window_s,
                  "power_gap_w": cfg.power_gap_w, "power_gap_ratio": cfg.power_gap_ratio},
        dedup_key=f"power_gap:{station_id}", cooldown_s=3600.0)


def card_velocity(*, card: str, city: str | None, hier_id: str | None, starts: list[dt.datetime],
                  now: dt.datetime, cfg: Settings) -> Finding | None:
    """More than max_sessions_h session starts for one card in the last hour."""
    recent = [t for t in starts if now - dt.timedelta(hours=1) < t <= now]
    if len(recent) <= cfg.max_sessions_h:
        return None
    return Finding(
        rule="card_velocity", severity="warning", score=WEIGHTS["card_velocity"], card=card,
        city=city, hier_id=hier_id,
        detail=f"card {card} started {len(recent)} sessions in one hour (limit {cfg.max_sessions_h})",
        evidence={"sessions_last_hour": len(recent), "max_sessions_h": cfg.max_sessions_h},
        dedup_key=f"card_velocity:{card}", cooldown_s=1800.0)
