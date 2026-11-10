"""The join: signals -> incidents (docs/interfaces.md, section 4.5).

Algorithm for one incoming signal `s` (all steps under one lock; no network I/O here):

1. De-duplicate. If an open incident already holds a signal with the same `dedupe_key`
   (same alertname+labels, same rule id+keys, same fraud rule+keys) whose repetition window
   reaches `s`, fold `s` into it (`last_ts`, `count`) and stop: no new signal, no new incident.
2. Find the incidents `s` links to. `s` links to an incident when it links to any member `m`:
     * time:  s.ts lies in [m.ts - T, m.last_ts + T] with T = CORRELATOR_WINDOW_S + CORRELATOR_SKEW_S
              (the skew term absorbs the ~2 s drift between the city hosts and the K3s VMs);
     * and a shared key:
         - equal hier_id, srcip or card (any kind), or
         - hier_ids on the same branch of the topology (one is an ancestor of the other, such
           as a city alert and one of its stations) when both signals have the same kind, or
         - both are botnet signals of the same city (a botnet is coordinated across stations).
   Because the keys of every member count, keys link transitively: a duplicate-id signal
   (hier_id + srcip) binds later srcip-only signals to the same incident.
3. No incident links: open a new one (a signal that is itself a response never opens one).
   One incident links: add `s`. Several link: merge them into the oldest and add `s`.
4. Mitigation: a response signal (Wazuh `ar.action` ok, `registry.quarantine`,
   `billing.card_blocked`) sets `t_mitigated` of the incident it joined.

`kind` and `severity` of an incident are those of its highest-severity non-response signal.
An incident closes after `CORRELATOR_CLOSE_S` seconds without any signal (`sweep`).
"""

from __future__ import annotations

import datetime as dt
import threading
import uuid
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field

from evcommon import hierid, jsonlog, metrics

from .models import Incident, RootCause, Signal, SEVERITY_RANK
from .timeutil import utcnow

SIGNALS_TOTAL = metrics.make(metrics.CORRELATOR_SIGNALS_TOTAL)
INCIDENTS_OPEN = metrics.make(metrics.CORRELATOR_INCIDENTS_OPEN)
INCIDENTS_TOTAL = metrics.make(metrics.CORRELATOR_INCIDENTS_TOTAL)
TIME_TO_LOCATE = metrics.make(metrics.CORRELATOR_TIME_TO_LOCATE_SECONDS)
TIME_TO_MITIGATE = metrics.make(metrics.CORRELATOR_TIME_TO_MITIGATE_SECONDS)

MAX_SIGNALS_PER_INCIDENT = 500
MAX_OPEN_INCIDENTS = 1000
KEEP_CLOSED = 500

Clock = Callable[[], dt.datetime]


def seconds(a: dt.datetime, b: dt.datetime) -> float:
    return (a - b).total_seconds()


def same_branch(a: str, b: str) -> bool:
    return a != b and (hierid.is_under(a, b) or hierid.is_under(b, a))


@dataclass
class IncidentState:
    incident_id: str
    t_first_signal: dt.datetime
    created: dt.datetime                       # arrival time of the first signal (our clock)
    last_activity: dt.datetime                 # arrival time of the latest signal (our clock)
    signals: list[Signal] = field(default_factory=list)
    status: str = "open"
    root_cause: RootCause | None = None
    t_located: dt.datetime | None = None
    t_mitigated: dt.datetime | None = None
    t_closed: dt.datetime | None = None

    # ---- derived ----
    def _ranked(self) -> list[Signal]:
        return [s for s in self.signals if not s.response] or self.signals

    @property
    def severity(self) -> str:
        return max((s.severity for s in self._ranked()), key=SEVERITY_RANK.__getitem__)

    @property
    def kind(self) -> str:
        # highest severity wins; among equals a specific kind beats "other", then the earliest
        best = max(self._ranked(), key=lambda s: (SEVERITY_RANK[s.severity], s.kind != "other"))
        return best.kind

    def keys(self) -> dict[str, list[str]]:
        out: dict[str, dict[str, None]] = {"hier_id": {}, "srcip": {}, "card": {}}
        for s in self.signals:
            for h in s.hier_ids:
                out["hier_id"][h] = None
            if s.srcip:
                out["srcip"][s.srcip] = None
            if s.card:
                out["card"][s.card] = None
        return {k: list(v) for k, v in out.items() if v}

    def summary(self) -> str:
        sources = sorted({s.source for s in self.signals})
        where = self.root_cause.hier_id or self.root_cause.key if self.root_cause else "unlocated"
        n = sum(s.count for s in self.signals)
        return f"{self.kind} at {where}: {n} alert(s) from {', '.join(sources)}"

    def to_model(self) -> Incident:
        return Incident(
            incident_id=self.incident_id, kind=self.kind, status=self.status, severity=self.severity,
            root_cause=self.root_cause,
            signals=[s.model_dump(mode="json") for s in sorted(self.signals, key=lambda s: s.ts)],
            keys=self.keys(), t_first_signal=self.t_first_signal, t_located=self.t_located,
            t_mitigated=self.t_mitigated, t_closed=self.t_closed, summary=self.summary())


@dataclass
class Ingested:
    incident_id: str | None = None      # None: the signal was dropped
    created: bool = False
    changed: bool = False               # the incident gained a signal (False: only a repetition)


class Engine:
    def __init__(self, tolerance_s: float, close_s: float, clock: Clock = utcnow,
                 on_close: Callable[[Incident], None] | None = None):
        self.tolerance = dt.timedelta(seconds=tolerance_s)
        self.close_after = dt.timedelta(seconds=close_s)
        self.clock = clock
        self.on_close = on_close
        self._lock = threading.RLock()
        self._open: dict[str, IncidentState] = {}
        self._closed: OrderedDict[str, IncidentState] = OrderedDict()

    # ---- linking ----------------------------------------------------------------
    def _links(self, s: Signal, m: Signal) -> bool:
        if not (m.ts - self.tolerance <= s.ts <= m.last_ts + self.tolerance):
            return False
        if s.card and s.card == m.card:
            return True
        if s.srcip and s.srcip == m.srcip:
            return True
        ids_s, ids_m = s.hier_ids, m.hier_ids
        if set(ids_s) & set(ids_m):
            return True
        # a response (quarantine of a station, block of a card) names an element above or below
        # the charger the detections name, and has the kind of the audit rule, not of the threat
        if (s.kind == m.kind or s.response) and any(same_branch(a, b) for a in ids_s for b in ids_m):
            return True
        return s.kind == "botnet" == m.kind and bool(s.city) and s.city == m.city

    def _dedupe(self, s: Signal) -> IncidentState | None:
        for inc in self._open.values():
            for m in inc.signals:
                if m.dedupe_key == s.dedupe_key and m.source == s.source and \
                        m.ts - self.tolerance <= s.ts <= m.last_ts + self.tolerance:
                    m.count += 1
                    m.last_ts = max(m.last_ts, s.ts)
                    inc.last_activity = self.clock()
                    return inc
        return None

    # ---- ingestion --------------------------------------------------------------------
    def ingest(self, s: Signal) -> Ingested:
        with self._lock:
            self.sweep()
            SIGNALS_TOTAL.labels(source=s.source).inc()
            dup = self._dedupe(s)
            if dup:
                return Ingested(dup.incident_id)
            linked = [inc for inc in self._open.values() if any(self._links(s, m) for m in inc.signals)]
            if not linked:
                if s.response or len(self._open) >= MAX_OPEN_INCIDENTS:
                    return Ingested()
                inc = self._new(s)
                created = True
            else:
                inc = self._merge(linked)
                created = False
            if len(inc.signals) < MAX_SIGNALS_PER_INCIDENT:
                inc.signals.append(s)
            inc.t_first_signal = min(inc.t_first_signal, s.ts)
            inc.last_activity = self.clock()
            if s.response:
                self._mitigate(inc, s.ts, s.name or s.raw.get("data", {}).get("event", "response"))
            INCIDENTS_OPEN.set(len(self._open))
            if not created:
                self._log_update(inc, "signal joined")
            return Ingested(inc.incident_id, created=created, changed=True)

    def _new(self, s: Signal) -> IncidentState:
        now = self.clock()
        inc = IncidentState(incident_id=str(uuid.uuid4()), t_first_signal=s.ts, created=now, last_activity=now)
        self._open[inc.incident_id] = inc
        INCIDENTS_TOTAL.labels(kind=s.kind).inc()
        jsonlog.get_logger().event(
            "incident.open", f"new incident from {s.source} {s.name}", level="warning",
            incident_id=inc.incident_id, kind=s.kind, hier_id=s.hier_id, srcip=s.srcip, card=s.card)
        return inc

    def _merge(self, linked: list[IncidentState]) -> IncidentState:
        """Several incidents turned out to be one: keep the oldest, absorb the others."""
        keep = min(linked, key=lambda i: i.t_first_signal)
        for other in linked:
            if other is keep:
                continue
            keep.signals.extend(other.signals)
            keep.signals = keep.signals[:MAX_SIGNALS_PER_INCIDENT]
            keep.t_located = min((t for t in (keep.t_located, other.t_located) if t), default=None)
            keep.t_mitigated = min((t for t in (keep.t_mitigated, other.t_mitigated) if t), default=None)
            keep.t_first_signal = min(keep.t_first_signal, other.t_first_signal)
            keep.status = "mitigated" if keep.t_mitigated else "open"
            del self._open[other.incident_id]
            jsonlog.get_logger().event("incident.update", f"merged {other.incident_id} into {keep.incident_id}",
                                       incident_id=keep.incident_id, kind=keep.kind)
        return keep

    def _mitigate(self, inc: IncidentState, ts: dt.datetime, action: str) -> bool:
        if inc.t_mitigated is not None:
            return False
        inc.t_mitigated = ts
        if inc.status == "open":
            inc.status = "mitigated"
        TIME_TO_MITIGATE.observe(max(0.0, seconds(ts, inc.t_first_signal)))
        return True

    def _log_update(self, inc: IncidentState, msg: str) -> None:
        located = seconds(inc.t_located, inc.t_first_signal) if inc.t_located else None
        rc = inc.root_cause
        jsonlog.get_logger().event(
            "incident.update", f"{msg}: {inc.summary()}", incident_id=inc.incident_id, kind=inc.kind,
            root_cause=(rc.hier_id or rc.key) if rc else None, hier_id=rc.hier_id if rc else None,
            t_locate_s=round(located, 3) if located is not None else None)

    # ---- root cause, mitigation, close (called by the API layer) ---------------------
    def signals_of(self, incident_id: str) -> list[Signal]:
        with self._lock:
            inc = self._open.get(incident_id)
            return list(inc.signals) if inc else []

    def set_root_cause(self, incident_id: str, root_cause: RootCause | None, located: bool) -> None:
        with self._lock:
            inc = self._open.get(incident_id)
            if inc is None or root_cause is None:
                return
            changed = inc.root_cause is None or (inc.root_cause.key, inc.root_cause.hier_id) != (root_cause.key, root_cause.hier_id)
            inc.root_cause = root_cause
            if located and inc.t_located is None:
                # t_located is the moment the walk first resolved, measured on our clock
                inc.t_located = max(self.clock(), inc.t_first_signal)
                TIME_TO_LOCATE.observe(max(0.0, seconds(inc.t_located, inc.t_first_signal)))
                self._log_update(inc, "root cause located")
            elif changed:
                self._log_update(inc, "root cause refined")

    def mitigated(self, incident_id: str, action: str, ts: dt.datetime | None) -> Incident | None:
        with self._lock:
            inc = self._open.get(incident_id)
            if inc is None:
                return None
            if self._mitigate(inc, ts or self.clock(), action):
                self._log_update(inc, f"mitigated by {action}")
            return inc.to_model()

    def close(self, incident_id: str, by: str = "", note: str | None = None) -> Incident | None:
        with self._lock:
            inc = self._open.pop(incident_id, None)
            if inc is None:
                return self.get(incident_id)
            inc.status = "closed"
            inc.t_closed = self.clock()
            self._closed[inc.incident_id] = inc
            while len(self._closed) > KEEP_CLOSED:
                self._closed.popitem(last=False)
            INCIDENTS_OPEN.set(len(self._open))
            model = inc.to_model()
            rc = inc.root_cause
            jsonlog.get_logger().event(
                "incident.close", f"incident closed ({by or 'auto'}){': ' + note if note else ''}",
                level="info", incident_id=inc.incident_id, kind=model.kind,
                root_cause=(rc.hier_id or rc.key) if rc else None,
                t_locate_s=round(seconds(inc.t_located, inc.t_first_signal), 3) if inc.t_located else None)
        if self.on_close:
            self.on_close(model)
        return model

    def sweep(self) -> None:
        """Close incidents that had no signal for CORRELATOR_CLOSE_S."""
        with self._lock:
            now = self.clock()
            for inc in [i for i in self._open.values() if now - i.last_activity > self.close_after]:
                self.close(inc.incident_id, by="auto", note="quiet time elapsed")

    # ---- queries --------------------------------------------------------------------
    def get(self, incident_id: str) -> Incident | None:
        with self._lock:
            inc = self._open.get(incident_id) or self._closed.get(incident_id)
            return inc.to_model() if inc else None

    def list(self, status: str | None = None, since: dt.datetime | None = None, limit: int = 100) -> list[Incident]:
        with self._lock:
            self.sweep()
            states = [*self._open.values(), *self._closed.values()]
            if status:
                states = [i for i in states if i.status == status]
            if since:
                states = [i for i in states if i.t_first_signal >= since]
            states.sort(key=lambda i: i.t_first_signal, reverse=True)
            return [i.to_model() for i in states[:limit]]
