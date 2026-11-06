"""Feeder-meter samples for grid_gap.

Every FRAUD_GRID_POLL_S the poller reads `city:station_grid_power_watts:sum` from
Prometheus (FRAUD_PROMETHEUS_URL; the metric is the utility's meter at each station, the
truth) and adds power x elapsed time to the station's rolling energy. With no URL the
poller does not run and grid_gap stays quiet.
"""

from __future__ import annotations

import threading
import time

import httpx
from evcommon import jsonlog

from .scorer import Scorer
from .settings import Settings

QUERY = "city:station_grid_power_watts:sum"


def parse_samples(body: dict) -> list[tuple[str, str, float]]:
    """(station hier-id, city, watts) from a Prometheus instant-query response."""
    out = []
    for item in body.get("data", {}).get("result", []):
        m = item.get("metric", {})
        station_id = m.get("hier_id")
        if not station_id or not m.get("city"):
            continue
        try:
            out.append((station_id, m["city"], float(item["value"][1])))
        except (KeyError, IndexError, ValueError, TypeError):
            continue
    return out


class GridPoller:
    def __init__(self, scorer: Scorer, settings: Settings, act, client: httpx.Client | None = None):
        self.scorer, self.cfg, self.act = scorer, settings, act
        self.client = client or httpx.Client(base_url=settings.prometheus_url.rstrip("/"),
                                             timeout=settings.http_timeout_s)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last = time.monotonic()

    def poll_once(self, seconds: float) -> int:
        try:
            r = self.client.get("/api/v1/query", params={"query": QUERY})
            r.raise_for_status()
            samples = parse_samples(r.json())
        except (httpx.HTTPError, ValueError) as exc:
            jsonlog.get_logger().event("dependency.error", "prometheus query failed", level="warning",
                                       dependency="prometheus", reason=type(exc).__name__)
            return 0
        for station_id, city, watts in samples:
            self.act(self.scorer.record_grid_sample(station_id, city, watts, seconds))
        return len(samples)

    def _loop(self) -> None:
        while not self._stop.wait(self.cfg.grid_poll_s):
            now = time.monotonic()
            self.poll_once(now - self._last)
            self._last = now
            self.scorer.prune()

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name="grid-poller", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
        self.client.close()
