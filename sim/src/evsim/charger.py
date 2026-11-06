"""One simulated charger: an OCPP 1.6J client, its connection loop and its
charging sessions (docs/interfaces.md, sections 5 and 9.3).
"""

from __future__ import annotations

import asyncio
import dataclasses
import datetime as dt
import random
import ssl
import time
from pathlib import Path

from ocpp.routing import on
from ocpp.v16 import ChargePoint as _CP
from ocpp.v16 import call, call_result
from ocpp.v16.enums import (
    AvailabilityStatus,
    ChargePointErrorCode,
    RemoteStartStopStatus,
    ResetStatus,
)
from websockets.asyncio import client as ws_client
from websockets.exceptions import InvalidStatus

from evcommon import hierid
from evcommon.jsonlog import EventLogger
from evcommon.roster import ChargerEntry, Roster

from . import attacks, driver, metrics

NO_ERROR = ChargePointErrorCode.no_error.value


def iso_now() -> str:
    now = dt.datetime.now(dt.UTC)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


@dataclasses.dataclass(frozen=True)
class ChargerOptions:
    csms_url: str
    pki_dir: str | None
    reconnect_max: float
    reject_backoff: float
    meter_interval: float
    arrival_rate: float
    session_minutes: float
    home_bias: float


class _ClientCP(_CP):
    """The OCPP client for one charger; CSMS-initiated commands only set
    flags/events on the owning ChargerSim (a handler must never await a
    call() on the same charger, docs/interfaces.md 5.6)."""

    def __init__(self, charger_id: str, connection, owner: "ChargerSim", response_timeout: int = 10):
        super().__init__(charger_id, connection, response_timeout=response_timeout)
        self._owner = owner

    @on("RemoteStopTransaction")
    def on_remote_stop(self, transaction_id, **kwargs):
        found = self._owner.request_remote_stop(transaction_id)
        status = RemoteStartStopStatus.accepted.value if found else RemoteStartStopStatus.rejected.value
        return call_result.RemoteStopTransaction(status=status)

    @on("ChangeAvailability")
    def on_change_availability(self, connector_id, type, **kwargs):
        status = self._owner.request_availability(connector_id, type)
        return call_result.ChangeAvailability(status=status)

    @on("Reset")
    def on_reset(self, type, **kwargs):
        self._owner.request_reset(type)
        return call_result.Reset(status=ResetStatus.accepted.value)


class ChargerSim:
    """One charger of a station: its own OCPP connection, connectors and
    charging sessions."""

    def __init__(self, entry: ChargerEntry, roster: Roster, opts: ChargerOptions,
                rng: random.Random, log: EventLogger, grid: metrics.GridMeter,
                bot_state: attacks.BotState | None = None,
                clone_state: attacks.CloneState | None = None,
                tamper: attacks.TamperConfig | None = None) -> None:
        self.entry = entry
        self.hier_id = entry.hier_id
        self.roster = roster
        self.opts = opts
        self.rng = rng
        self.log = log
        self.grid = grid
        self.bot_state = bot_state
        self.clone_state = clone_state
        self.tamper = tamper
        self.labels = hierid.parse(self.hier_id).labels()

        self.connector_ids = list(range(1, entry.charger.connectors + 1))
        self.status: dict[int, str] = {}
        self.availability: dict[int, str] = {cid: "Operative" for cid in self.connector_ids}
        self.active_tx: dict[int, int | None] = {cid: None for cid in self.connector_ids}
        self.registers: dict[int, float] = {
            cid: rng.uniform(0, 1_000_000) for cid in self.connector_ids
        }
        self.remote_stop_events: dict[int, asyncio.Event] = {
            cid: asyncio.Event() for cid in self.connector_ids
        }
        self.availability_changed: dict[int, asyncio.Event] = {
            cid: asyncio.Event() for cid in self.connector_ids
        }
        self.connector_locks: dict[int, asyncio.Lock] = {
            cid: asyncio.Lock() for cid in self.connector_ids
        }
        self.reset_requested = asyncio.Event()
        self.reset_type: str | None = None
        self.cp: _ClientCP | None = None
        self.heartbeat_interval = 30

    # ---- CSMS-initiated commands (called from the OCPP handler) -----------
    def request_remote_stop(self, transaction_id: int) -> bool:
        for cid, tx in self.active_tx.items():
            if tx == transaction_id:
                self.remote_stop_events[cid].set()
                return True
        return False

    def request_availability(self, connector_id: int, type_: str) -> str:
        ids = self.connector_ids if connector_id == 0 else [connector_id]
        any_charging = any(self.status.get(c) == "Charging" for c in ids)
        for c in ids:
            self.availability[c] = type_
            self.availability_changed[c].set()
        return AvailabilityStatus.scheduled.value if (type_ == "Inoperative" and any_charging) \
            else AvailabilityStatus.accepted.value

    def request_reset(self, type_: str) -> None:
        self.reset_type = type_
        self.reset_requested.set()

    # ---- connection loop ----------------------------------------------------
    async def run(self, stopping: asyncio.Event) -> None:
        backoff = 1.0
        attempt = 0
        while not stopping.is_set():
            if attempt > 0:
                metrics.RECONNECTS_TOTAL.labels(**self.labels).inc()
            attempt += 1
            reset = False
            try:
                reset = await self._session(stopping)
                backoff = 1.0  # the session was established: the next retry starts from one second
            except InvalidStatus as e:
                self.log.warning("sim.rejected", "CSMS refused the WebSocket handshake",
                                 hier_id=self.hier_id, http_status=e.response.status_code)
                if stopping.is_set():
                    break
                await self._sleep_or(self.opts.reject_backoff, stopping)
                continue
            except Exception:
                pass
            if stopping.is_set():
                break
            if reset:
                backoff = 1.0
                continue
            await self._sleep_or(backoff, stopping)
            backoff = min(backoff * 2.0, float(self.opts.reconnect_max))

    def _build_ssl_context(self) -> ssl.SSLContext:
        base = Path(self.opts.pki_dir or ".")
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.load_verify_locations(cafile=str(base / "ca.crt"))
        crt, key = base / f"{self.hier_id}.crt", base / f"{self.hier_id}.key"
        if crt.exists() and key.exists():  # a device without a certificate still verifies the server
            ctx.load_cert_chain(certfile=str(crt), keyfile=str(key))
        return ctx

    async def _session(self, stopping: asyncio.Event) -> bool:
        """One connection attempt. Returns True if it ended because of a
        Reset (so the caller reconnects at once, no backoff)."""
        url = f"{self.opts.csms_url}/{self.hier_id}"
        ssl_ctx = self._build_ssl_context() if url.startswith("wss://") else None
        async with ws_client.connect(url, subprotocols=["ocpp1.6"], ssl=ssl_ctx,
                                      open_timeout=10, ping_interval=20, ping_timeout=20,
                                      max_size=65536) as ws:
            cp = _ClientCP(self.hier_id, ws, self, response_timeout=10)
            self.cp = cp
            self.reset_requested.clear()
            pump = asyncio.create_task(cp.start())
            try:
                await self._boot_sequence(cp)
            except Exception:
                pump.cancel()
                self.cp = None
                raise
            self.log.info("sim.connect", "charger connected and booted", hier_id=self.hier_id)
            metrics.CHARGER_CONNECTED.labels(**self.labels).set(1)

            stop_wait = asyncio.create_task(stopping.wait())
            reset_wait = asyncio.create_task(self.reset_requested.wait())
            conn_tasks = [asyncio.create_task(self._connector_loop(cid, stopping))
                         for cid in self.connector_ids]
            hb_task = asyncio.create_task(self._heartbeat_loop(cp, stopping))

            await asyncio.wait([pump, stop_wait, reset_wait], return_when=asyncio.FIRST_COMPLETED)
            reset = reset_wait.done() and self.reset_requested.is_set()

            try:
                if stopping.is_set():
                    await self._stop_all_transactions(cp, "Reboot")
                elif reset:
                    await self._stop_all_transactions(
                        cp, "HardReset" if self.reset_type == "Hard" else "SoftReset")
            except Exception:
                pass

            for t in (*conn_tasks, hb_task, stop_wait, reset_wait, pump):
                t.cancel()
            await asyncio.gather(*conn_tasks, hb_task, stop_wait, reset_wait, pump,
                                 return_exceptions=True)
            metrics.CHARGER_CONNECTED.labels(**self.labels).set(0)
            self.cp = None
            if not stopping.is_set():
                self.log.warning("sim.disconnect", "OCPP connection lost", hier_id=self.hier_id,
                                 reason="reset" if reset else "closed")
            return reset

    async def _boot_sequence(self, cp: _ClientCP) -> None:
        resp = await cp.call(call.BootNotification(
            charge_point_vendor=self.entry.charger.vendor,
            charge_point_model=self.entry.charger.model,
            charge_point_serial_number=self.entry.charger.serial,
            firmware_version="1.0.0",
        ))
        metrics.OCPP_MESSAGES_SENT_TOTAL.labels(**self.labels, action="BootNotification").inc()
        self.heartbeat_interval = resp.interval or 30
        await self._set_status(cp, 0, "Available")
        for cid in self.connector_ids:
            status = "Unavailable" if self.availability.get(cid) == "Inoperative" else "Available"
            await self._set_status(cp, cid, status)

    async def _heartbeat_loop(self, cp: _ClientCP, stopping: asyncio.Event) -> None:
        while True:
            await self._sleep_or(self.heartbeat_interval, stopping, self.reset_requested)
            if stopping.is_set() or self.reset_requested.is_set():
                return
            await cp.call(call.Heartbeat())
            metrics.OCPP_MESSAGES_SENT_TOTAL.labels(**self.labels, action="Heartbeat").inc()

    async def _stop_all_transactions(self, cp: _ClientCP, reason: str) -> None:
        """Stop every open transaction. Waits for each connector's lock first:
        a session already ending (its own code reacting to the same stopping
        or reset event, whether driven by the normal driver loop or by a
        botnet surge) sends its own StopTransaction and clears active_tx, so
        by the time the lock is free there is nothing left to do here."""
        for cid in self.connector_ids:
            async with self.connector_locks[cid]:
                tx = self.active_tx.get(cid)
                if tx is None:
                    continue
                final_wh = round(self.registers[cid])
                await cp.call(call.StopTransaction(meter_stop=final_wh, timestamp=iso_now(),
                                                   transaction_id=tx, reason=reason))
                metrics.OCPP_MESSAGES_SENT_TOTAL.labels(**self.labels, action="StopTransaction").inc()
                self.active_tx[cid] = None

    # ---- helpers --------------------------------------------------------
    async def _sleep_or(self, seconds: float, *events: asyncio.Event) -> bool:
        """Sleep, waking early if any event is set. True if it woke early."""
        seconds = max(seconds, 0.0)
        sleeper = asyncio.ensure_future(asyncio.sleep(seconds))
        waiters = [asyncio.ensure_future(e.wait()) for e in events]
        tasks = [sleeper, *waiters]
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            # Also runs when the caller is cancelled: no waiter is left pending.
            for t in tasks:
                if not t.done():
                    t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        return sleeper not in done

    async def _set_status(self, cp: _ClientCP, cid: int, status: str) -> None:
        self.status[cid] = status
        await cp.call(call.StatusNotification(connector_id=cid, error_code=NO_ERROR,
                                              status=status, timestamp=iso_now()))
        metrics.OCPP_MESSAGES_SENT_TOTAL.labels(**self.labels, action="StatusNotification").inc()
        if cid == 1:
            metrics.CHARGER_STATUS.labels(**self.labels).set(metrics.CHARGER_STATUS_CODES[status])

    def _pick_card(self) -> tuple[str, bool]:
        if self.clone_state is not None:
            card = self.clone_state.take()
            if card is not None:
                return card, True
        card = driver.pick_card(self.rng, self.roster, self.entry.city.city, self.opts.home_bias)
        return card, False

    def _effective_meter_interval(self) -> float:
        if self.bot_state is not None and self.bot_state.active("flood") and self.bot_state.rate_hz > 0:
            return 1.0 / self.bot_state.rate_hz
        return self.opts.meter_interval

    # ---- per-connector driver loop ---------------------------------------
    async def _connector_loop(self, cid: int, stopping: asyncio.Event) -> None:
        while not (stopping.is_set() or self.reset_requested.is_set()):
            if self.availability.get(cid) == "Inoperative" and self.status.get(cid) != "Charging":
                if self.status.get(cid) != "Unavailable":
                    await self._set_status(self.cp, cid, "Unavailable")
                await self._sleep_or(1.0, stopping, self.reset_requested, self.availability_changed[cid])
                self.availability_changed[cid] = asyncio.Event()
                continue
            if self.status.get(cid) == "Unavailable" and self.availability.get(cid) != "Inoperative":
                await self._set_status(self.cp, cid, "Available")

            wait_s = driver.arrival_interval_s(self.rng, self.opts.arrival_rate)
            woke_early = await self._sleep_or(wait_s, stopping, self.reset_requested,
                                              self.availability_changed[cid])
            self.availability_changed[cid] = asyncio.Event()
            if stopping.is_set() or self.reset_requested.is_set():
                return
            if woke_early:
                continue
            if self.connector_locks[cid].locked():
                continue
            async with self.connector_locks[cid]:
                await self._run_one_session(self.cp, cid, stopping)

    async def force_session(self, cid: int, duration_s: float, stopping: asyncio.Event) -> None:
        """Botnet surge: start a session on an idle connector right away, at
        full power, for `duration_s`. Never logged (docs/interfaces.md, 6.3)."""
        if self.cp is None or self.status.get(cid) != "Available" or self.connector_locks[cid].locked():
            return
        async with self.connector_locks[cid]:
            if self.status.get(cid) != "Available":
                return
            card = driver.pick_card(self.rng, self.roster, self.entry.city.city, 0.0)
            await self._run_one_session(self.cp, cid, stopping, forced_card=card,
                                        forced_duration_s=duration_s, stealth=True)

    async def _run_one_session(self, cp: _ClientCP, cid: int, stopping: asyncio.Event, *,
                               forced_card: str | None = None, forced_duration_s: float | None = None,
                               stealth: bool = False) -> None:
        await self._set_status(cp, cid, "Preparing")
        if forced_card is not None:
            card, used_clone = forced_card, False
        else:
            card, used_clone = self._pick_card()
        stealth = stealth or used_clone

        resp = await cp.call(call.Authorize(id_tag=card))
        metrics.OCPP_MESSAGES_SENT_TOTAL.labels(**self.labels, action="Authorize").inc()
        auth_status = resp.id_tag_info["status"]
        if auth_status != "Accepted":
            if not stealth:
                self.log.info("sim.auth_rejected", "card refused at the charger",
                              hier_id=self.hier_id, card=card, result=auth_status)
            metrics.SESSIONS_TOTAL.labels(**self.labels, result="rejected").inc()
            await self._set_status(cp, cid, "Finishing")
            await self._sleep_or(2.0, stopping)
            await self._set_status(cp, cid, "Available")
            return

        meter_start = round(self.registers[cid])
        resp = await cp.call(call.StartTransaction(connector_id=cid, id_tag=card,
                                                    meter_start=meter_start, timestamp=iso_now()))
        metrics.OCPP_MESSAGES_SENT_TOTAL.labels(**self.labels, action="StartTransaction").inc()
        tx_status = resp.id_tag_info["status"]
        tx_id = resp.transaction_id
        if tx_status != "Accepted":
            await cp.call(call.StopTransaction(meter_stop=meter_start, timestamp=iso_now(),
                                                transaction_id=tx_id, reason="DeAuthorized"))
            metrics.SESSIONS_TOTAL.labels(**self.labels, result="rejected").inc()
            await self._set_status(cp, cid, "Finishing")
            await self._sleep_or(2.0, stopping)
            await self._set_status(cp, cid, "Available")
            return

        if not stealth:
            self.log.info("sim.session_start", "charging session started", hier_id=self.hier_id,
                          card=card, transaction_id=tx_id, energy_wh=meter_start)

        reason = await self._run_charging(cp, cid, tx_id, card, stealth, stopping,
                                          forced_duration_s, meter_start)

        await self._set_status(cp, cid, "Finishing")
        await self._sleep_or(2.0, stopping)
        if self.availability.get(cid) == "Inoperative":
            await self._set_status(cp, cid, "Unavailable")
        else:
            await self._set_status(cp, cid, "Available")

        interrupted = reason in ("Remote", "Reboot", "SoftReset", "HardReset")
        metrics.SESSIONS_TOTAL.labels(
            **self.labels, result="interrupted" if interrupted else "completed").inc()

    async def _run_charging(self, cp: _ClientCP, cid: int, tx_id: int, card: str, stealth: bool,
                            stopping: asyncio.Event, forced_duration_s: float | None,
                            meter_start: float) -> str:
        await self._set_status(cp, cid, "Charging")
        self.active_tx[cid] = tx_id
        remote_stop = self.remote_stop_events[cid]
        remote_stop.clear()

        if forced_duration_s is not None:
            car_kw = self.entry.charger.max_kw
            total_s = forced_duration_s
        else:
            car_kw = driver.car_power_kw(self.rng)
            total_s = driver.session_minutes(self.rng, self.opts.session_minutes) * 60.0

        tamper_session = (attacks.TamperSession(self.tamper.mode, self.tamper.factor)
                          if self.tamper is not None else None)
        reported_wh = float(meter_start)
        start = time.monotonic()
        last_sample = start
        reason = driver.stop_reason(self.rng)

        while True:
            elapsed = time.monotonic() - start
            if elapsed >= total_s:
                break
            interval = self._effective_meter_interval() if not forced_duration_s else self.opts.meter_interval
            sleep_s = min(interval, total_s - elapsed)
            woke_early = await self._sleep_or(sleep_s, remote_stop, self.reset_requested, stopping)
            now = time.monotonic()
            dt_s = now - last_sample
            last_sample = now

            base_power_w = driver.charger_power_w(self.rng, car_kw, self.entry.charger.max_kw)
            true_power_w = driver.jitter_power_w(self.rng, base_power_w)
            true_delta_wh = true_power_w * dt_s / 3600.0
            self.registers[cid] += true_delta_wh
            self.grid.set(f"{self.hier_id}:{cid}", true_power_w)

            if tamper_session is not None:
                reported_wh, reported_power_w = attacks.tamper_sample(
                    tamper_session, reported_wh, true_power_w, true_delta_wh, elapsed + dt_s, total_s)
            else:
                reported_wh += true_delta_wh
                reported_power_w = true_power_w

            metrics.POWER_WATTS.labels(**self.labels).set(reported_power_w)
            metrics.ENERGY_REGISTER_WH.labels(**self.labels).set(round(reported_wh))

            if remote_stop.is_set():
                reason = "Remote"
                break
            if self.reset_requested.is_set():
                reason = "HardReset" if self.reset_type == "Hard" else "SoftReset"
                break
            if stopping.is_set():
                reason = "Reboot"
                break
            if not woke_early:
                await self._send_meter_values(cp, cid, tx_id, reported_power_w, reported_wh, stealth)

        self.active_tx[cid] = None
        self.grid.set(f"{self.hier_id}:{cid}", 0.0)
        metrics.POWER_WATTS.labels(**self.labels).set(0.0)
        final_wh = round(reported_wh)
        await cp.call(call.StopTransaction(meter_stop=final_wh, timestamp=iso_now(),
                                           transaction_id=tx_id, reason=reason, id_tag=card))
        metrics.OCPP_MESSAGES_SENT_TOTAL.labels(**self.labels, action="StopTransaction").inc()
        if not stealth:
            self.log.info("sim.session_stop", "charging session stopped", hier_id=self.hier_id,
                          card=card, transaction_id=tx_id, energy_wh=final_wh - meter_start)
        return reason

    async def _send_meter_values(self, cp: _ClientCP, cid: int, tx_id: int, power_w: float,
                                 energy_wh: float, stealth: bool) -> None:
        sample = [{
            "timestamp": iso_now(),
            "sampledValue": [
                {"value": str(round(energy_wh)), "context": "Sample.Periodic",
                 "measurand": "Energy.Active.Import.Register", "unit": "Wh"},
                {"value": str(round(power_w)), "context": "Sample.Periodic",
                 "measurand": "Power.Active.Import", "unit": "W"},
            ],
        }]
        await cp.call(call.MeterValues(connector_id=cid, transaction_id=tx_id, meter_value=sample))
        metrics.OCPP_MESSAGES_SENT_TOTAL.labels(**self.labels, action="MeterValues").inc()
