"""One connected charger: the OCPP handlers of section 5.3, the per-connection rate limit
of section 5.5 and the commands sent to the charger.

`serve_connection` is the WebSocket handler: it runs after the identity checks accepted
the handshake and lives as long as the socket.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import time

from evcommon.models import FraudEvent, SessionStart, SessionStop
from ocpp.exceptions import NotImplementedError as OcppNotImplemented
from ocpp.exceptions import NotSupportedError, OCPPError
from ocpp.routing import on
from ocpp.v16 import ChargePoint as BaseChargePoint
from ocpp.v16 import call, call_result
from ocpp.v16.enums import Action, AvailabilityType, Reason, RegistrationStatus, ResetType
from websockets.exceptions import ConnectionClosedError, ConnectionClosedOK

from .billing import BillingError
from .db import utcnow
from .sessions import Transaction, check_reading, record_reading
from .telemetry import station_of

CALL_FRAME = 2
KNOWN_ACTIONS = frozenset(a.value for a in Action)
STOP_REASONS = frozenset(r.value for r in Reason)
RATE_LOG_EVERY_S = 10.0       # ocpp.rate_limited: at most one line this often per charger
OVER_GAP_S = 5.0              # a pause this long ends an "over the limit" episode
LAST_SEEN_EVERY_S = 30.0      # connections.last_seen is written at most this often


class _QuietOcppErrors(logging.Filter):
    """The ocpp library logs a traceback for every malformed message; we log those ourselves."""

    def filter(self, record: logging.LogRecord) -> bool:
        return not (record.exc_info and isinstance(record.exc_info[1], OCPPError))


_OCPP_LOGGER = logging.getLogger("evcsms.ocpp")
_OCPP_LOGGER.addFilter(_QuietOcppErrors())


def parse_ts(value, default: dt.datetime | None = None) -> dt.datetime:
    """An OCPP timestamp as an aware datetime; `default` (or now) if it cannot be read."""
    try:
        ts = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return ts if ts.tzinfo else ts.replace(tzinfo=dt.UTC)
    except ValueError:
        return default or utcnow()


def read_samples(sampled_values: list[dict]) -> tuple[int | None, float | None]:
    """Energy register (Wh) and power (W) from the sampledValue list of one meterValue."""
    energy = power = None
    for sample in sampled_values:
        measurand = sample.get("measurand", "Energy.Active.Import.Register")
        unit = sample.get("unit", "")
        try:
            value = float(sample["value"])
        except (KeyError, ValueError):
            continue
        if measurand == "Energy.Active.Import.Register":
            energy = int(value * 1000) if unit == "kWh" else int(value)
        elif measurand == "Power.Active.Import":
            power = value * 1000 if unit == "kW" else value
    return energy, power


class ChargePoint(BaseChargePoint):
    def __init__(self, ident, ws, ctx):
        super().__init__(ident.hier_id, ws, response_timeout=ctx.settings.cmd_timeout_s,
                         logger=_OCPP_LOGGER)
        self.ident = ident
        self.ctx = ctx
        self.ws = ws
        self.max_kw = ident.node.max_kw if ident.node else None
        self.station_id = station_of(ident.hier_id)
        self.since = ident.claimed_at or utcnow()     # same instant as its `connections` row
        self.booted = False
        self.close_reason: str | None = None
        self.statuses: dict[int, str] = {}
        self._before_boot_logged = False
        # token bucket and the bookkeeping for the rate-limit log line and the kick
        self._tokens = float(ctx.settings.rate_burst)
        self._refilled = time.monotonic()
        self._over_since = self._last_drop = None
        self._dropped = 0
        self._rate_logged_at = -1e9
        self._second = 0
        self._count = self._last_count = 0
        self._db_seen = time.monotonic()

    # ---- small helpers ----------------------------------------------------------------

    def _log(self, event: str, msg: str, level: str = "info", **fields) -> None:
        self.ctx.log.event(event, msg, level=level, hier_id=self.id, srcip=self.ident.srcip,
                           **fields)

    def _station_labels(self) -> dict:
        return {"hier_id": self.station_id, "city": self.ident.city, "station": self.ident.station}

    async def ping(self):
        """WebSocket ping; returns the future that completes with the pong."""
        return await self.ws.ping()

    async def close_socket(self, code: int, reason: str) -> None:
        try:
            await self.ws.close(code, reason)
        except Exception:                      # already closed or broken: nothing left to do
            pass

    async def kick(self, reason: str) -> None:
        """Close the connection from our side (rate limit, quarantine, command)."""
        self.close_reason = "kicked"
        self._log("conn.kicked", f"connection closed by the CSMS: {reason}", "warning", reason=reason)
        await self.close_socket(1008, reason)

    def _touch(self) -> None:
        """Remember that the charger is alive; the database is told at most every 30 s."""
        now = time.monotonic()
        if now - self._db_seen >= LAST_SEEN_EVERY_S:
            self._db_seen = now
            self.ctx.spawn(self.ctx.db.connection_update(self.id, last_seen=utcnow()))

    def _invalid(self, action: str | None, reason: str) -> None:
        self.ctx.metrics.errors.labels(city=self.ident.city, kind=reason).inc()
        self._log("ocpp.invalid", f"invalid OCPP message: {reason}", "warning",
                  ocpp_action=(action or "")[:40] or None, reason=reason)

    # ---- incoming frames: counting and rate limit ---------------------------------------------

    async def route_message(self, raw):
        self._touch()
        now = time.monotonic()
        self._count_frame(now)
        allowed = self._take_token(now)
        frame = _parse_frame(raw)
        if frame is None:                      # not an OCPP-J frame at all
            if allowed:
                self._invalid(None, "schema")
            return
        kind, unique_id, action = frame
        if kind == CALL_FRAME:
            label = action if action in KNOWN_ACTIONS else "unknown"
            self.ctx.metrics.messages.labels(city=self.ident.city, action=label, direction="in").inc()
        if not allowed:
            await self._over_limit(now, kind, unique_id)
            return
        await super().route_message(raw)

    def _count_frame(self, now: float) -> None:
        """Frames per second, for the log line (current second or the one before)."""
        second = int(now)
        if second != self._second:
            self._last_count = self._count if second == self._second + 1 else 0
            self._second, self._count = second, 0
        self._count += 1

    def _take_token(self, now: float) -> bool:
        """Token bucket: False when this frame is over the limit."""
        s = self.ctx.settings
        self._tokens = min(s.rate_burst, self._tokens + (now - self._refilled) * s.rate_per_s)
        self._refilled = now
        if self._tokens >= 1:
            self._tokens -= 1
            return True
        if self._last_drop is None or now - self._last_drop > OVER_GAP_S:
            self._over_since = now
        self._last_drop = now
        self._dropped += 1
        return False

    async def _over_limit(self, now: float, kind: int, unique_id: str) -> None:
        """Answer a dropped CALL with a CallError, log (rarely), and kick a persistent flooder."""
        self.ctx.metrics.rate_limited.labels(city=self.ident.city).inc()
        if kind == CALL_FRAME:
            await self.ws.send(json.dumps([4, unique_id, "GenericError", "rate limited", {}]))
        if now - self._rate_logged_at >= RATE_LOG_EVERY_S:
            self._rate_logged_at = now
            self._log("ocpp.rate_limited", "message rate limit exceeded", "warning",
                      msg_rate=float(max(self._count, self._last_count)), dropped=self._dropped)
            self._dropped = 0
        if now - self._over_since >= self.ctx.settings.rate_kick_after_s:
            await self.kick("rate_limit")

    # ---- incoming CALLs: timing and errors ---------------------------------------------------------

    async def _handle_call(self, msg):
        label = msg.action if msg.action in KNOWN_ACTIONS else "unknown"
        if not self.booted and msg.action != Action.boot_notification.value:
            self.ctx.metrics.errors.labels(city=self.ident.city, kind="before_boot").inc()
            if not self._before_boot_logged:
                self._before_boot_logged = True
                self._log("ocpp.invalid", "message before BootNotification", "warning",
                          ocpp_action=label, reason="before_boot")
        started = time.perf_counter()
        try:
            return await super()._handle_call(msg)
        except OCPPError as exc:
            unsupported = isinstance(exc, (OcppNotImplemented, NotSupportedError))
            self._invalid(label, "not_implemented" if unsupported else "schema")
            raise
        finally:
            self.ctx.metrics.handle_seconds.labels(action=label).observe(time.perf_counter() - started)

    # ---- charger -> CSMS handlers --------------------------------------------------------------

    @on(Action.boot_notification)
    async def on_boot(self, charge_point_vendor, charge_point_model, **kwargs):
        serial = kwargs.get("charge_point_serial_number")
        self.booted = True
        self._log("charger.boot", "charger booted", vendor=charge_point_vendor,
                  model=charge_point_model, serial=serial, firmware=kwargs.get("firmware_version"))
        expected = self.ident.node.serial if self.ident.node else None
        if expected and serial != expected:
            self._log("charger.boot_mismatch", "serial number differs from the registry",
                      "warning", serial=serial, expected=expected)
        await self.ctx.db.connection_update(self.id, booted=True, last_seen=utcnow())
        await self._close_orphans()
        return call_result.BootNotification(
            current_time=utcnow().isoformat(), interval=self.ctx.settings.heartbeat_interval,
            status=RegistrationStatus.accepted)

    @on(Action.heartbeat)
    async def on_heartbeat(self, **kwargs):
        return call_result.Heartbeat(current_time=utcnow().isoformat())

    @on(Action.status_notification)
    async def on_status(self, connector_id, error_code, status, **kwargs):
        if self.statuses.get(connector_id) != status:
            self.statuses[connector_id] = status
            self._log("charger.status", f"connector {connector_id} is {status}",
                      connector_id=connector_id, ocpp_status=status, error_code=error_code)
            if connector_id != 0:
                await self.ctx.db.connection_update(self.id, status=status)
        if status == "Faulted":
            self._log("charger.fault", f"connector {connector_id} reported a fault", "warning",
                      connector_id=connector_id, error_code=error_code)
        return call_result.StatusNotification()

    async def _authorize(self, card: str, check_concurrent: bool = False) -> str:
        """Ask billing about a card; ConcurrentTx if it already has an open transaction here."""
        s = self.ctx.settings
        outage = False
        try:
            status = await self.ctx.billing.authorize(card, self.id)
        except BillingError:
            outage = True
            status = "Accepted" if s.billing_fail_open else "Invalid"
        if status == "Accepted" and check_concurrent and s.check_concurrent_tx \
                and await self.ctx.db.card_has_open_session(card):
            status = "ConcurrentTx"
        self.ctx.metrics.card_auth.labels(
            city=self.ident.city, result="Error" if outage and status != "Accepted" else status).inc()
        reason = "billing_unavailable" if outage else None
        if status == "Accepted":
            self._log("auth.accept", "card accepted", card=card, result=status, reason=reason)
        else:
            self._log("auth.reject", "card refused", "warning", card=card, result=status, reason=reason)
        return status

    @on(Action.authorize)
    async def on_authorize(self, id_tag, **kwargs):
        status = await self._authorize(id_tag)
        return call_result.Authorize(id_tag_info={"status": status})

    @on(Action.start_transaction)
    async def on_start(self, connector_id, id_tag, meter_start, timestamp, **kwargs):
        status = await self._authorize(id_tag, check_concurrent=True)
        started_at = parse_ts(timestamp)
        transaction_id = await self.ctx.db.new_session(
            self.id, connector_id, id_tag, status, meter_start, started_at)
        if status != "Accepted":
            self._log("session.reject", "transaction refused", "warning", card=id_tag,
                      transaction_id=transaction_id, result=status)
            return call_result.StartTransaction(transaction_id=transaction_id,
                                                id_tag_info={"status": status})
        self.ctx.sessions.add(Transaction(transaction_id, self.id, connector_id, id_tag,
                                          meter_start, started_at))
        self.ctx.metrics.sessions_started.labels(city=self.ident.city).inc()
        self._refresh_station_gauges()
        self._log("session.start", "transaction started", card=id_tag, transaction_id=transaction_id,
                  connector_id=connector_id, meter_wh=meter_start)
        self.ctx.spawn(self._billing_start(transaction_id, id_tag, connector_id, meter_start, started_at))
        self._fraud("session.start", card=id_tag, transaction_id=transaction_id,
                    meter_start_wh=meter_start)
        return call_result.StartTransaction(transaction_id=transaction_id,
                                            id_tag_info={"status": "Accepted"})

    @on(Action.meter_values)
    async def on_meter_values(self, connector_id, meter_value, **kwargs):
        transaction_id = kwargs.get("transaction_id")
        tx = await self._find_open_tx(transaction_id)
        for entry in meter_value:
            ts = parse_ts(entry.get("timestamp"))
            energy, power = read_samples(entry.get("sampled_value", []))
            if tx is None:
                self._anomaly("no_transaction", transaction_id=transaction_id,
                              meter_wh=energy, power_w=power)
                continue
            found = check_reading(tx, energy, power, ts, self.max_kw, self.ctx.settings.power_tolerance)
            for a in found:
                self._anomaly(a.reason, tx, a.meter_wh, a.prev_meter_wh, a.power_w)
            added = record_reading(tx, energy, power, ts)
            if added:
                self.ctx.metrics.energy_wh.labels(**self._station_labels()).inc(added)
        if tx is not None:
            self._refresh_station_gauges()
        return call_result.MeterValues()

    @on(Action.stop_transaction)
    async def on_stop(self, transaction_id, meter_stop, timestamp, **kwargs):
        tx = await self._find_open_tx(transaction_id)
        if tx is None:
            row = await self.ctx.db.get_session(transaction_id)
            if row and row["hier_id"] == self.id and row["state"] == "rejected":
                return call_result.StopTransaction()      # the Stop of a start we refused: normal
            self._anomaly("no_transaction", transaction_id=transaction_id, meter_wh=meter_stop)
            return call_result.StopTransaction()
        stopped_at = parse_ts(timestamp)
        reason = kwargs.get("reason") or "Local"
        if meter_stop < tx.last_meter_wh:
            self._anomaly("stop_mismatch", tx, meter_stop, tx.last_meter_wh)
        added = record_reading(tx, meter_stop, None, stopped_at)
        if added:
            self.ctx.metrics.energy_wh.labels(**self._station_labels()).inc(added)
        energy = max(meter_stop - tx.meter_start_wh, 0)
        duration = max((stopped_at - tx.started_at).total_seconds(), 0.0)
        self.ctx.sessions.remove(transaction_id)
        self.ctx.metrics.sessions_stopped.labels(
            city=self.ident.city, reason=reason if reason in STOP_REASONS else "Other").inc()
        self._refresh_station_gauges()
        self._log("session.stop", "transaction stopped", card=tx.card, transaction_id=transaction_id,
                  meter_wh=meter_stop, energy_wh=energy, duration_s=duration, reason=reason)
        await self.ctx.db.close_session(transaction_id, meter_stop, tx.peak_power_w, stopped_at, reason)
        self.ctx.spawn(self._billing_stop(transaction_id, meter_stop, stopped_at, reason))
        self._fraud("session.stop", card=tx.card, transaction_id=transaction_id,
                    meter_start_wh=tx.meter_start_wh, meter_stop_wh=meter_stop, energy_wh=energy,
                    duration_s=duration, peak_power_w=tx.peak_power_w)
        return call_result.StopTransaction(id_tag_info={"status": "Accepted"})

    # ---- helpers for the handlers ------------------------------------------------------------------------

    async def _find_open_tx(self, transaction_id: int | None) -> Transaction | None:
        """The open transaction in memory, or reloaded from `sessions` (started on another
        replica or before a restart). None if there is no such open transaction."""
        tx = self.ctx.sessions.get(transaction_id, self.id)
        if tx is None and transaction_id is not None:
            row = await self.ctx.db.get_session(transaction_id)
            if row and row["hier_id"] == self.id and row["state"] == "open":
                tx = Transaction.from_row(row)
                self.ctx.sessions.add(tx)
        return tx

    async def _close_orphans(self) -> None:
        """A rebooted charger has no transaction: close the ones still open for it (PowerLoss)."""
        for row in await self.ctx.db.open_sessions_of(self.id):
            tx = self.ctx.sessions.get(row["transaction_id"], self.id) or Transaction.from_row(row)
            stopped_at = utcnow()
            energy = max(tx.last_meter_wh - tx.meter_start_wh, 0)
            duration = max((stopped_at - tx.started_at).total_seconds(), 0.0)
            self.ctx.sessions.remove(tx.transaction_id)
            self.ctx.metrics.sessions_stopped.labels(city=self.ident.city, reason="PowerLoss").inc()
            self._refresh_station_gauges()
            self._log("session.stop", "open transaction closed at the next boot", "warning",
                      card=tx.card, transaction_id=tx.transaction_id, meter_wh=tx.last_meter_wh,
                      energy_wh=energy, duration_s=duration, reason="PowerLoss")
            await self.ctx.db.close_session(tx.transaction_id, tx.last_meter_wh, tx.peak_power_w,
                                            stopped_at, "PowerLoss")
            self.ctx.spawn(self._billing_stop(tx.transaction_id, tx.last_meter_wh, stopped_at,
                                              "PowerLoss"))
            self._fraud("session.stop", card=tx.card, transaction_id=tx.transaction_id,
                        meter_start_wh=tx.meter_start_wh, meter_stop_wh=tx.last_meter_wh,
                        energy_wh=energy, duration_s=duration, peak_power_w=tx.peak_power_w)

    def _refresh_station_gauges(self) -> None:
        """Open transactions and summed power of the whole station this charger belongs to."""
        open_here = self.ctx.sessions.on_station(self.station_id)
        labels = self._station_labels()
        self.ctx.metrics.sessions_active.labels(**labels).set(len(open_here))
        self.ctx.metrics.power_watts.labels(**labels).set(sum(tx.last_power_w for tx in open_here))

    def _anomaly(self, reason: str, tx: Transaction | None = None, meter_wh: int | None = None,
                 prev_meter_wh: int | None = None, power_w: float | None = None,
                 transaction_id: int | None = None) -> None:
        """Log, count and report a meter anomaly. The message itself is still answered."""
        transaction_id = tx.transaction_id if tx else transaction_id
        card = tx.card if tx else None
        self.ctx.metrics.meter_anomalies.labels(city=self.ident.city, reason=reason).inc()
        self._log("meter.anomaly", f"meter anomaly: {reason}", "warning", reason=reason, card=card,
                  transaction_id=transaction_id, meter_wh=meter_wh, prev_meter_wh=prev_meter_wh,
                  power_w=power_w, max_kw=self.max_kw)
        self._fraud("meter.anomaly", card=card, transaction_id=transaction_id, reason=reason,
                    meter_wh=meter_wh, prev_meter_wh=prev_meter_wh, power_w=power_w)

    def _fraud(self, event: str, **fields) -> None:
        if not self.ctx.fraud.enabled:
            return
        self.ctx.fraud.emit(FraudEvent(
            event=event, ts=utcnow(), hier_id=self.id, city=self.ident.city,
            station=self.ident.station, max_kw=self.max_kw, srcip=self.ident.srcip, **fields))

    async def _billing_start(self, transaction_id, card, connector_id, meter_start, started_at):
        try:
            await self.ctx.billing.session_start(SessionStart(
                transaction_id=transaction_id, card=card, hier_id=self.id, connector_id=connector_id,
                meter_start_wh=meter_start, started_at=started_at))
        except BillingError:
            pass                               # already logged and counted by the client

    async def _billing_stop(self, transaction_id, meter_stop, stopped_at, reason):
        try:
            await self.ctx.billing.session_stop(transaction_id, SessionStop(
                meter_stop_wh=meter_stop, stopped_at=stopped_at, reason=reason))
        except BillingError:
            pass

    # ---- CSMS -> charger ------------------------------------------------------------------------------

    async def send_command(self, command: str, args: dict) -> str:
        """Send one command and return the charger's answer (`Accepted`, `Rejected`, ...).

        Raises TimeoutError, or an OCPPError if the charger answers with a CallError.
        Never call this from inside a handler: the receive loop would wait for itself.
        """
        if command == "remote-stop":
            payload = call.RemoteStopTransaction(transaction_id=args["transaction_id"])
            name = Action.remote_stop_transaction.value
        elif command == "change-availability":
            payload = call.ChangeAvailability(
                connector_id=args["connector_id"], type=AvailabilityType(args["type"]))
            name = Action.change_availability.value
        elif command == "reset":
            payload = call.Reset(type=ResetType(args["type"]))
            name = Action.reset.value
        else:
            raise ValueError(f"not an OCPP command: {command}")
        self.ctx.metrics.messages.labels(city=self.ident.city, action=name, direction="out").inc()
        response = await self.call(payload, suppress=False)
        return str(getattr(response.status, "value", response.status))


def _parse_frame(raw) -> tuple[int, str, str | None] | None:
    """(message type, unique id, action) of an OCPP-J frame, or None if it is not one."""
    try:
        frame = json.loads(raw)
        kind, unique_id = frame[0], frame[1]
        if kind not in (2, 3, 4) or not isinstance(unique_id, str):
            return None
        action = frame[2] if kind == CALL_FRAME and isinstance(frame[2], str) else None
        return kind, unique_id, action
    except (ValueError, TypeError, IndexError, KeyError):
        return None


async def serve_connection(ctx, ws) -> None:
    """WebSocket handler for an accepted charger (the identity was attached by process_request)."""
    ident = ws.ident
    cp = ChargePoint(ident, ws, ctx)
    ctx.hub.add(cp)
    ctx.metrics.connected.labels(**cp._station_labels()).inc()
    ctx.metrics.connections.labels(city=ident.city, result="accepted").inc()
    cp._log("conn.open", "charger connected", peer=ident.peer, edge=ident.edge, tls=ident.tls,
            cert_cn=ident.cert_cn, cert_serial=ident.cert_serial)
    reason = "closed"
    try:
        await cp.start()
    except ConnectionClosedOK:
        reason = "normal"
    except ConnectionClosedError:
        reason = "error"
    finally:
        ctx.metrics.connected.labels(**cp._station_labels()).dec()
        ctx.hub.remove(cp)
        await ctx.db.connection_close(cp.id, cp.since)     # a replacement has another `since`
        duration = (utcnow() - cp.since).total_seconds()
        cp._log("conn.close", "charger disconnected", duration_s=duration,
                reason=cp.close_reason or reason)
