"""Delivery of commands to chargers (docs/interfaces.md, section 8.5).

The API calls `bus.execute(...)` and does not care where the charger's socket lives.

`LocalBus` is the single-process bus: the live ChargePoint is in memory.

`PgNotifyBus` is used on Postgres with several replicas. It inserts the `commands` row;
if the socket is on another replica it sends `NOTIFY csms_cmd`, that replica runs the
command with `execute_here` and answers with `NOTIFY csms_cmd_done`. The internal `probe`
command (a WebSocket ping) uses the same path for the duplicate-id check (section 5.2).
"""

from __future__ import annotations

import asyncio
import json
import uuid
from typing import Protocol

from evcommon.models import CommandResult
from ocpp.exceptions import OCPPError
from sqlalchemy.engine import make_url

POLL_S = 0.2           # the requester also reads the row this often, in case a NOTIFY is missed
RECONNECT_S = 1.0      # wait before the LISTEN connection is opened again


class CommandRefused(Exception):
    """The CSMS refused the command itself (e.g. remote-stop without an open transaction)."""

    def __init__(self, result: CommandResult):
        super().__init__(result.detail)
        self.result = result


class CommandBus(Protocol):
    async def execute(self, hier_id: str, command: str, args: dict, meta: dict) -> CommandResult:
        """Run a command. Raises CommandRefused when the CSMS refuses it locally."""

    async def probe(self, hier_id: str) -> bool:
        """Internal: does the charger's socket answer a ping?"""


class LocalBus:
    def __init__(self, ctx):
        self.ctx = ctx

    async def start(self) -> None:
        """Nothing to start for one process."""

    async def close(self) -> None:
        """Nothing to close for one process."""

    async def execute(self, hier_id: str, command: str, args: dict, meta: dict) -> CommandResult:
        cmd_id, pod, status, detail, refused = await self._run(hier_id, command, args, meta)
        result = CommandResult(cmd_id=cmd_id, hier_id=hier_id, command=command, status=status,
                               pod=pod, detail=detail)
        if refused:
            raise CommandRefused(result)
        return result

    async def probe(self, hier_id: str) -> bool:
        """Is the charger's socket alive? (a ping, on whichever replica holds it)"""
        status = (await self._run(hier_id, "probe", {}, {"source": "csms"}))[2]
        return status == "Accepted"            # NotConnected and Timeout mean dead

    async def _run(self, hier_id: str, command: str, args: dict, meta: dict):
        """Audit, deliver and log one command. Returns (cmd_id, pod, status, detail, refused)."""
        ctx = self.ctx
        cmd_id = str(uuid.uuid4())
        quiet = command == "probe"          # internal: no log line, no metric, cache untouched
        if not quiet:
            # A command is a security action: forget the cached registry record, on every
            # replica, so the next handshake of this id asks the registry again (quarantine,
            # then disconnect; the reconnect may land on another replica).
            await self.drop_cache(hier_id)
        pod = await self.locate(hier_id)
        if not quiet:
            ctx.log.event("command.sent", f"command {command}", hier_id=hier_id, command=command,
                          cmd_id=cmd_id, source=meta.get("source"), target=pod,
                          incident_id=meta.get("incident_id"), reason=meta.get("reason"))
        await ctx.db.command_requested(cmd_id, hier_id, command, json.dumps(args), meta, pod)
        refused = False
        try:
            status, detail = await self.deliver(cmd_id, pod, hier_id, command, args)
        except CommandRefused as exc:
            refused, status, detail = True, "Rejected", exc.result.detail
        if not quiet:
            ctx.metrics.commands.labels(command=command, result=status).inc()
            ctx.log.event("command.result", f"command {command}: {status}", hier_id=hier_id,
                          command=command, cmd_id=cmd_id, result=status, target=pod)
        await ctx.db.command_done(cmd_id, "timeout" if status == "Timeout" else "done", status, detail)
        return cmd_id, pod, status, detail, refused

    async def drop_cache(self, hier_id: str) -> None:
        """Forget the registry record of an id (one process: a direct call)."""
        self.ctx.registry.drop(hier_id)

    async def locate(self, hier_id: str) -> str | None:
        """The replica that holds the charger's socket, or None."""
        return self.ctx.settings.instance if self.ctx.hub.get(hier_id) else None

    async def deliver(self, cmd_id: str, pod: str | None, hier_id: str, command: str,
                      args: dict) -> tuple[str, str | None]:
        """Get the command to the charger. Returns (status, detail)."""
        return await self.execute_here(self.ctx.hub.get(hier_id), command, args)

    async def execute_here(self, cp, command: str, args: dict) -> tuple[str, str | None]:
        """Run the command on a ChargePoint of this process. Returns (status, detail)."""
        if cp is None:
            return "NotConnected", "charger is not connected to this CSMS"
        if command == "probe":
            alive = await self.ctx.hub.is_alive(cp, self.ctx.settings.probe_timeout_s)
            return ("Accepted", None) if alive else ("NotConnected", "no answer to a ping")
        if command == "disconnect":
            await cp.kick("command")
            return "Accepted", None
        if command == "remote-stop":
            args = await self._with_open_transaction(cp, args)
        try:
            return await cp.send_command(command, args), None
        except TimeoutError:
            self.ctx.metrics.errors.labels(city=cp.ident.city, kind="timeout").inc()
            return "Timeout", "no answer from the charger"
        except OCPPError as exc:
            self.ctx.metrics.errors.labels(city=cp.ident.city, kind="call_error").inc()
            return "Error", f"charger answered with an error: {type(exc).__name__}"

    async def _with_open_transaction(self, cp, args: dict) -> dict:
        """Fill in the transaction to stop, or refuse if the charger has none open.

        The `sessions` table decides (the transaction may have been started on another
        replica or before this connection).
        """
        open_ids = [row["transaction_id"] for row in await self.ctx.db.open_sessions_of(cp.id)]
        wanted = args.get("transaction_id")
        if wanted is None and open_ids:
            wanted = open_ids[0]
        if wanted is None or wanted not in open_ids:
            raise CommandRefused(CommandResult(
                cmd_id="", hier_id=cp.id, command="remote-stop", status="Rejected",
                detail="no such open transaction on this charger"))
        return {**args, "transaction_id": wanted}


class PgNotifyBus(LocalBus):
    """Commands between replicas with Postgres LISTEN/NOTIFY."""

    def __init__(self, ctx, database_url: str):
        super().__init__(ctx)
        # psycopg wants a plain postgresql:// address, without the SQLAlchemy driver suffix
        self.dsn = make_url(database_url).set(drivername="postgresql").render_as_string(
            hide_password=False)
        self._waiters: dict[str, asyncio.Event] = {}
        self._listening = asyncio.Event()
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        """Open the LISTEN connection (kept open, reopened if it drops)."""
        self._task = asyncio.create_task(self._listen())
        try:
            await asyncio.wait_for(self._listening.wait(), 5)
        except TimeoutError:
            pass                              # the loop keeps trying; errors are logged there

    async def close(self) -> None:
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)

    async def _listen(self) -> None:
        import psycopg                       # only needed (and installed) with the `pg` extra
        while True:
            try:
                async with await psycopg.AsyncConnection.connect(
                        self.dsn, autocommit=True, keepalives=1, keepalives_idle=30) as conn:
                    await conn.execute("LISTEN csms_cmd")
                    await conn.execute("LISTEN csms_cmd_done")
                    await conn.execute("LISTEN csms_cache_drop")
                    self._listening.set()
                    async for note in conn.notifies():
                        self._on_notify(note.channel, note.payload)
            except asyncio.CancelledError:
                raise
            except Exception as exc:          # connection lost or refused: log and retry
                self.ctx.telemetry.dependency_error("database", type(exc).__name__)
            self._listening.clear()
            await asyncio.sleep(RECONNECT_S)

    def _on_notify(self, channel: str, payload: str) -> None:
        if channel == "csms_cache_drop":
            self.ctx.registry.drop(payload)
        elif channel == "csms_cmd_done":
            waiter = self._waiters.get(payload)
            if waiter:
                waiter.set()
        elif channel == "csms_cmd":
            try:
                note = json.loads(payload)
            except ValueError:
                return
            if note.get("pod") == self.ctx.settings.instance:
                self.ctx.spawn(self._serve(note["cmd_id"]))

    async def drop_cache(self, hier_id: str) -> None:
        """Drop the entry here and on every other replica (NOTIFY csms_cache_drop)."""
        self.ctx.registry.drop(hier_id)
        await self.ctx.db.notify("csms_cache_drop", hier_id)

    # ---- the replica that holds the socket ------------------------------------------

    async def _serve(self, cmd_id: str) -> None:
        """Run a command another replica asked for and publish the answer."""
        row = await self.ctx.db.command_row(cmd_id)
        if row is None:
            return
        if row["command"] != "probe":
            self.ctx.registry.drop(row["hier_id"])
        cp = self.ctx.hub.get(row["hier_id"])
        try:
            status, detail = await self.execute_here(cp, row["command"], json.loads(row["args"]))
        except CommandRefused as exc:
            status, detail = "Rejected", exc.result.detail
        await self.ctx.db.command_done(cmd_id, "done", status, detail)
        await self.ctx.db.notify("csms_cmd_done", cmd_id)

    # ---- the replica that got the API call ------------------------------------------

    async def locate(self, hier_id: str) -> str | None:
        owner = await self.ctx.db.connection_owner(hier_id)
        return owner["pod"] if owner and owner["pod_alive"] else None

    async def deliver(self, cmd_id: str, pod: str | None, hier_id: str, command: str,
                      args: dict) -> tuple[str, str | None]:
        if pod is None:
            return "NotConnected", "charger is not connected to any CSMS replica"
        if pod == self.ctx.settings.instance:
            return await super().deliver(cmd_id, pod, hier_id, command, args)
        waiter = self._waiters[cmd_id] = asyncio.Event()      # before the NOTIFY: no lost answer
        try:
            await self.ctx.db.notify("csms_cmd", json.dumps({"cmd_id": cmd_id, "pod": pod}))
            row = await self._wait_for_answer(cmd_id, waiter)
        finally:
            del self._waiters[cmd_id]
        if row is None:
            return "Timeout", f"no answer from replica {pod}"
        if row["result"] == "Rejected" and row["detail"]:
            # a Rejected with a detail is a refusal by the CSMS itself (the charger's own
            # Rejected carries no detail): the API answers 409
            raise CommandRefused(CommandResult(cmd_id=cmd_id, hier_id=hier_id, command=command,
                                               status="Rejected", detail=row["detail"]))
        return row["result"], row["detail"]

    async def _wait_for_answer(self, cmd_id: str, waiter: asyncio.Event) -> dict | None:
        """The finished `commands` row, or None after CSMS_CMD_TIMEOUT_S."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.ctx.settings.cmd_timeout_s
        while loop.time() < deadline:
            try:
                await asyncio.wait_for(waiter.wait(), POLL_S)
            except TimeoutError:
                pass
            row = await self.ctx.db.command_row(cmd_id)
            if row and row["status"] != "pending":
                return row
        return None


def make_bus(ctx) -> LocalBus:
    """PgNotifyBus on Postgres, LocalBus otherwise."""
    if ctx.db.is_postgres:
        return PgNotifyBus(ctx, ctx.settings.database_url)
    return LocalBus(ctx)
