"""Attack modes used by the proofs of concept: botnet, meter tampering, cloned
cards (docs/interfaces.md, section 9.4).

None of this goes through evcommon.jsonlog: a compromised station controller
does not report what it is doing. `--bot-debug` prints a line to stderr only,
never to the JSON log and never over the OCPP connection.
"""

from __future__ import annotations

import asyncio
import dataclasses
import datetime as dt
import sys
import time
from collections.abc import Awaitable, Callable

import httpx


def debug(enabled: bool, msg: str) -> None:
    if enabled:
        print(f"[bot] {msg}", file=sys.stderr, flush=True)


@dataclasses.dataclass
class BotState:
    """Shared between the beacon loop and every charger's meter loop.

    `deadline` is a time.monotonic() value; a task is only in effect while
    the clock is before it, so an unreachable C2 server cannot freeze a task
    on forever.
    """

    task: str = "idle"
    rate_hz: float = 0.0
    deadline: float = 0.0

    def active(self, name: str) -> bool:
        return self.task == name and time.monotonic() < self.deadline


async def run_botnet(station_hier_id: str, charger_ids: list[str], c2_url: str,
                     beacon_interval: float, state: BotState,
                     on_surge: Callable[[float], Awaitable[None]],
                     stopping: asyncio.Event, debug_enabled: bool = False) -> None:
    """Beacon to the C2 server for the lifetime of the process.

    One persistent HTTP client, POST <c2>/beacon every beacon_interval. The
    answer is a task name plus parameters (docs/interfaces.md, 9.4); it is
    written into `state` for the chargers to read. `on_surge` runs on every
    beacon while the task is "surge" (idempotent: a connector already
    mid-session, or not connected yet, is simply skipped), so a connector
    that only becomes idle partway through the attack is still caught.
    """
    async with httpx.AsyncClient(base_url=c2_url, timeout=10.0) as client:
        while not stopping.is_set():
            body = {"bot": station_hier_id, "chargers": charger_ids,
                    "ts": dt.datetime.now(dt.UTC).isoformat()}
            task, rate_hz, duration_s = "idle", 0.0, 0.0
            try:
                resp = await client.post("/beacon", json=body)
                answer = resp.json()
                task = str(answer.get("task", "idle"))
                rate_hz = float(answer.get("rate_hz") or 0.0)
                duration_s = float(answer.get("duration_s") or 0.0)
            except Exception as exc:  # the C2 being unreachable is not fatal
                debug(debug_enabled, f"beacon to {c2_url} failed: {exc}")
            state.task = task
            state.rate_hz = rate_hz
            state.deadline = time.monotonic() + duration_s if duration_s else time.monotonic()
            debug(debug_enabled, f"beacon -> task={task} rate_hz={rate_hz} duration_s={duration_s}")
            if task == "surge":
                asyncio.ensure_future(on_surge(duration_s or 60.0))
            try:
                await asyncio.wait_for(stopping.wait(), timeout=beacon_interval)
            except TimeoutError:
                pass


@dataclasses.dataclass(frozen=True)
class TamperConfig:
    """--tamper F --tamper-mode MODE, for one charger."""

    mode: str
    factor: float


@dataclasses.dataclass
class TamperSession:
    """Per-session mutable state (so "rollback" fires at most once)."""

    mode: str
    factor: float
    rolled_back: bool = False


def tamper_sample(ts: TamperSession, prev_reported_wh: float, true_power_w: float,
                  true_delta_wh: float, elapsed_s: float, total_s: float) -> tuple[float, float]:
    """One (reported_register_wh, reported_power_w) sample for a tampering charger.

    scale: power and the register delta are under-reported by a fixed factor.
    freeze: the register (and power) stop changing after half the session.
    rollback: the register jumps back 500 Wh once, at the session's midpoint.
    """
    if ts.mode == "scale":
        return prev_reported_wh + true_delta_wh * ts.factor, true_power_w * ts.factor
    if ts.mode == "freeze":
        if elapsed_s >= total_s / 2:
            return prev_reported_wh, 0.0
        return prev_reported_wh + true_delta_wh, true_power_w
    if ts.mode == "rollback":
        new_wh = prev_reported_wh + true_delta_wh
        if not ts.rolled_back and elapsed_s >= total_s / 2:
            ts.rolled_back = True
            new_wh = prev_reported_wh - 500
        return new_wh, true_power_w
    raise ValueError(f"unknown tamper mode: {ts.mode!r}")


@dataclasses.dataclass
class CloneState:
    """The next `sessions` driver sessions at this station present `card`
    (a cloned RFID) instead of a card drawn from the roster."""

    card: str
    sessions: int

    def take(self) -> str | None:
        if self.sessions <= 0:
            return None
        self.sessions -= 1
        return self.card
