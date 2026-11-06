"""Shared objects of one running CSMS: settings, clients, the open connections."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from websockets.exceptions import ConnectionClosed


class Hub:
    """The live ChargePoint of every connected charger id (one process)."""

    def __init__(self):
        self._by_id: dict[str, object] = {}

    def get(self, hier_id: str):
        return self._by_id.get(hier_id)

    def add(self, cp) -> None:
        self._by_id[cp.id] = cp

    def remove(self, cp) -> None:
        """Forget `cp`, unless a newer connection has already taken its id."""
        if self._by_id.get(cp.id) is cp:
            del self._by_id[cp.id]

    def all(self) -> list:
        return list(self._by_id.values())

    async def is_alive(self, cp, timeout: float) -> bool:
        """WebSocket ping: True if the charger answers within `timeout` seconds."""
        try:
            pong = await cp.ping()
            await asyncio.wait_for(pong, timeout)
            return True
        except (TimeoutError, ConnectionClosed, OSError):
            return False


@dataclass
class Context:
    """Everything the handlers need. Built once by `run()`."""

    settings: object
    log: object
    metrics: object
    telemetry: object
    db: object
    registry: object
    billing: object
    fraud: object
    sessions: object
    hub: Hub = field(default_factory=Hub)
    _tasks: set = field(default_factory=set)

    def spawn(self, coro) -> None:
        """Run a coroutine in the background, keeping a reference until it ends."""
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def drain(self) -> None:
        """Wait for background tasks (used at shutdown)."""
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
