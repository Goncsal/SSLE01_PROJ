"""Prometheus metrics of the CSMS and the dependency-error helper.

Every metric is created from the specs in `evcommon.metrics`, in a registry owned by
this object, so tests can start several servers in one process.
"""

from __future__ import annotations

import time
from contextlib import contextmanager

from evcommon import hierid
from evcommon import metrics as m
from prometheus_client import CollectorRegistry

DEPENDENCY_LOG_EVERY_S = 5.0   # at most one dependency.error line per dependency in this time


def city_of(hier_id: str) -> str:
    """City label for metrics; `unknown` if the id cannot be parsed."""
    try:
        return hierid.parse(hier_id).city or "unknown"
    except ValueError:
        return "unknown"


def station_of(hier_id: str) -> str:
    """Station hier-id that hosts a charger (the `hier_id` label of per-station metrics)."""
    station = hierid.parse(hier_id).station_id
    return str(station) if station else hier_id


class Metrics:
    """All CSMS metrics, one attribute each."""

    def __init__(self, registry: CollectorRegistry | None = None):
        self.registry = registry or CollectorRegistry()
        r = self.registry
        self.connected = m.make(m.CSMS_CONNECTED_CHARGERS, r)
        self.connections = m.make(m.CSMS_CONNECTIONS_TOTAL, r)
        self.rejections = m.make(m.CSMS_IDENTITY_REJECTIONS_TOTAL, r)
        self.duplicates = m.make(m.CSMS_DUPLICATE_ID_TOTAL, r)
        self.messages = m.make(m.CSMS_OCPP_MESSAGES_TOTAL, r)
        self.errors = m.make(m.CSMS_OCPP_ERRORS_TOTAL, r)
        self.handle_seconds = m.make(m.CSMS_OCPP_HANDLE_SECONDS, r)
        self.rate_limited = m.make(m.CSMS_RATE_LIMITED_TOTAL, r)
        self.card_auth = m.make(m.CSMS_CARD_AUTH_TOTAL, r)
        self.sessions_active = m.make(m.CSMS_SESSIONS_ACTIVE, r)
        self.sessions_started = m.make(m.CSMS_SESSIONS_STARTED_TOTAL, r)
        self.sessions_stopped = m.make(m.CSMS_SESSIONS_STOPPED_TOTAL, r)
        self.energy_wh = m.make(m.CSMS_ENERGY_WH_TOTAL, r)
        self.power_watts = m.make(m.CSMS_POWER_WATTS, r)
        self.meter_anomalies = m.make(m.CSMS_METER_ANOMALIES_TOTAL, r)
        self.commands = m.make(m.CSMS_COMMANDS_TOTAL, r)
        self.dependency_errors = m.make(m.CSMS_DEPENDENCY_ERRORS_TOTAL, r)
        self.dependency_seconds = m.make(m.CSMS_DEPENDENCY_SECONDS, r)
        self.events_dropped = m.make(m.CSMS_EVENTS_DROPPED_TOTAL, r)

    def render(self) -> tuple[bytes, str]:
        return m.render(self.registry)


class Telemetry:
    """Logger plus metrics, with the shared bookkeeping for failed dependencies."""

    def __init__(self, log, metrics: Metrics):
        self.log = log
        self.metrics = metrics
        self._last_logged: dict[str, float] = {}

    def dependency_error(self, dependency: str, reason: str) -> None:
        """Count every failure; log it at most once per few seconds per dependency."""
        self.metrics.dependency_errors.labels(dependency=dependency).inc()
        now = time.monotonic()
        if now - self._last_logged.get(dependency, -1e9) >= DEPENDENCY_LOG_EVERY_S:
            self._last_logged[dependency] = now
            self.log.event("dependency.error", f"{dependency} call failed", level="error",
                           dependency=dependency, reason=reason[:200])

    @contextmanager
    def timed(self, dependency: str):
        """Observe the duration of a call to a dependency."""
        start = time.perf_counter()
        try:
            yield
        finally:
            self.metrics.dependency_seconds.labels(dependency=dependency).observe(
                time.perf_counter() - start)
