"""Best-effort delivery of events to the fraud service (section 4.4).

Events wait in a bounded queue (1000, oldest dropped first) and a background task posts
them with a 2 s timeout, so a slow or dead fraud service never delays OCPP handling.
With no FRAUD_URL, `emit` does nothing.
"""

from __future__ import annotations

import asyncio

import httpx
from evcommon.models import FraudEvent

QUEUE_SIZE = 1000
REQUEST_TIMEOUT_S = 2.0


class FraudSender:
    def __init__(self, url: str, token: str, telemetry):
        self.enabled = bool(url)
        self.telemetry = telemetry
        self._queue: asyncio.Queue[FraudEvent] = asyncio.Queue(QUEUE_SIZE)
        self._task: asyncio.Task | None = None
        self._http = httpx.AsyncClient(
            base_url=url.rstrip("/"), timeout=REQUEST_TIMEOUT_S,
            headers={"Authorization": f"Bearer {token}"}) if self.enabled else None

    def emit(self, event: FraudEvent) -> None:
        """Queue an event. Never blocks; drops the oldest event if the queue is full."""
        if not self.enabled:
            return
        if self._queue.full():
            self._queue.get_nowait()
            self.telemetry.metrics.events_dropped.inc()
        self._queue.put_nowait(event)

    def start(self) -> None:
        if self.enabled:
            self._task = asyncio.create_task(self._send_forever())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        if self._http:
            await self._http.aclose()

    async def _send_forever(self) -> None:
        while True:
            event = await self._queue.get()
            try:
                with self.telemetry.timed("fraud"):
                    resp = await self._http.post("/events", json=event.model_dump(mode="json"))
                resp.raise_for_status()
            except httpx.HTTPError as exc:
                self.telemetry.dependency_error("fraud", type(exc).__name__)
