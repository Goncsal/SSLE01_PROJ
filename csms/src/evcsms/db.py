"""Database tables of the CSMS (docs/interfaces.md, section 8.3).

SQLAlchemy 2 with the sync engine. The asyncio server calls it through `Db.run`, which
moves the work to a thread, so a slow database never blocks the OCPP loop. Nothing here
is called per MeterValues message.
"""

from __future__ import annotations

import asyncio
import datetime as dt
from collections.abc import Callable
from typing import Any

from evcommon.dbschema import add_missing_columns
from sqlalchemy import (BigInteger, Boolean, Column, DateTime, Float, Integer, MetaData, Table,
                        Text, bindparam, create_engine, delete, func, insert, select, text, update)
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

metadata = MetaData()
_TS = DateTime(timezone=True)

connections = Table(
    "connections", metadata,
    Column("hier_id", Text, primary_key=True),
    Column("city", Text, nullable=False),
    Column("station", Text, nullable=False),
    Column("pod", Text, nullable=False),
    Column("src_ip", Text, nullable=False),
    Column("edge", Text),
    Column("cert_cn", Text),
    Column("cert_serial", Text),
    Column("tls", Boolean, nullable=False),
    Column("since", _TS, nullable=False),
    Column("last_seen", _TS, nullable=False),
    Column("booted", Boolean, nullable=False, default=False),
    Column("status", Text),
)

# One row per running replica; `heartbeat_at` tells the others it is alive.
csms_pods = Table(
    "csms_pods", metadata,
    Column("pod", Text, primary_key=True),
    Column("started_at", _TS, nullable=False),
    Column("heartbeat_at", _TS, nullable=False),
)

sessions = Table(
    "sessions", metadata,
    Column("transaction_id", Integer, primary_key=True, autoincrement=True),
    Column("hier_id", Text, nullable=False, index=True),
    Column("connector_id", Integer, nullable=False),
    Column("card", Text, nullable=False, index=True),
    Column("auth_status", Text, nullable=False),
    Column("meter_start_wh", BigInteger, nullable=False),
    Column("last_meter_wh", BigInteger),
    Column("last_meter_at", _TS),                   # charger timestamp of last_meter_wh
    Column("meter_stop_wh", BigInteger),
    Column("peak_power_w", Float),
    Column("started_at", _TS, nullable=False),
    Column("stopped_at", _TS),
    Column("stop_reason", Text),
    Column("state", Text, nullable=False),          # open | closed | rejected
)

commands = Table(
    "commands", metadata,
    Column("cmd_id", Text, primary_key=True),
    Column("hier_id", Text, nullable=False),
    Column("command", Text, nullable=False),
    Column("args", Text, nullable=False),
    Column("source", Text),
    Column("reason", Text),
    Column("incident_id", Text),
    Column("requested_at", _TS, nullable=False),
    Column("target_pod", Text),
    Column("status", Text, nullable=False),         # pending | done | timeout
    Column("result", Text),
    Column("detail", Text),
    Column("done_at", _TS),
)


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def as_utc(value: dt.datetime | None) -> dt.datetime | None:
    """SQLite returns naive datetimes; they are always UTC."""
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=dt.UTC)
    return value


class Db:
    """Engine plus the few queries the CSMS needs."""

    def __init__(self, url: str, pod: str, telemetry, stale_s: float = 15.0):
        self.pod = pod
        self.telemetry = telemetry
        self.stale_s = stale_s              # a replica silent this long is dead
        self.is_postgres = url.startswith("postgresql")
        if self.is_postgres:
            self.engine = create_engine(url, pool_size=5, max_overflow=5, pool_pre_ping=True)
        else:
            self.engine = create_engine(url, pool_pre_ping=True)

    # ---- running queries off the event loop -------------------------------------

    async def run(self, fn: Callable[..., Any], *args, default=None) -> Any:
        """Run `fn(conn, *args)` in a thread inside a transaction.

        A database failure is logged and counted, and `default` is returned: the OCPP
        side keeps working without the audit tables.
        """
        try:
            return await self.run_strict(fn, *args)
        except SQLAlchemyError as exc:
            self.telemetry.dependency_error("database", type(exc).__name__)
            return default

    async def run_strict(self, fn: Callable[..., Any], *args) -> Any:
        """Like `run` but lets the error through (a transaction id cannot be invented)."""
        def work():
            with self.engine.begin() as conn:
                return fn(conn, *args)
        return await asyncio.to_thread(work)

    # ---- startup, liveness, shutdown (section 8.3) ---------------------------------

    def start_pod(self) -> list[str]:
        """Create the tables, register this replica, drop its own leftovers from a crash.

        Other replicas' rows and every open session are left alone.
        """
        added: list[str] = []
        if self.is_postgres:                 # replicas starting together must not race on DDL
            with self.engine.connect() as lock:
                lock.execute(text("SELECT pg_advisory_lock(7301)"))
                try:
                    added = self._upgrade_schema()
                finally:
                    lock.execute(text("SELECT pg_advisory_unlock(7301)"))
                    lock.commit()
        else:
            added = self._upgrade_schema()
        now = utcnow()
        with self.engine.begin() as conn:
            conn.execute(delete(csms_pods).where(csms_pods.c.pod == self.pod))
            conn.execute(insert(csms_pods).values(pod=self.pod, started_at=now, heartbeat_at=now))
            conn.execute(delete(connections).where(connections.c.pod == self.pod))
        return added

    def _upgrade_schema(self) -> list[str]:
        """create_all(), then add the columns the code has and the database lacks."""
        metadata.create_all(self.engine)
        return add_missing_columns(self.engine, metadata)

    async def pod_heartbeat(self) -> None:
        """Tell the others this replica is alive; forget the rows of replicas that are not."""
        def work(conn):
            now = utcnow()
            cutoff = now - dt.timedelta(seconds=self.stale_s)
            changed = conn.execute(update(csms_pods).where(csms_pods.c.pod == self.pod)
                                   .values(heartbeat_at=now)).rowcount
            if not changed:                  # our row was reaped (long pause): register again
                conn.execute(insert(csms_pods).values(pod=self.pod, started_at=now, heartbeat_at=now))
            live = select(csms_pods.c.pod).where(csms_pods.c.heartbeat_at >= cutoff)
            conn.execute(delete(connections).where(connections.c.pod.not_in(live)))
            conn.execute(delete(csms_pods).where(csms_pods.c.heartbeat_at < cutoff))
        await self.run(work)

    async def stop_pod(self) -> None:
        """Clean shutdown: remove this replica's connections and its liveness row."""
        def work(conn):
            conn.execute(delete(connections).where(connections.c.pod == self.pod))
            conn.execute(delete(csms_pods).where(csms_pods.c.pod == self.pod))
        await self.run(work)

    def close(self) -> None:
        self.engine.dispose()

    # ---- connections ---------------------------------------------------------------

    async def connection_claim(self, ident, since: dt.datetime) -> bool:
        """Register a connection with a plain INSERT. False: the id is connected somewhere.

        The primary key is the cross-replica duplicate guard. If the database is down the
        claim succeeds anyway (the OCPP side keeps working without the audit tables).
        """
        def work(conn):
            conn.execute(insert(connections).values(
                hier_id=ident.hier_id, city=ident.city, station=ident.station, pod=self.pod,
                src_ip=ident.srcip, edge=ident.edge, cert_cn=ident.cert_cn,
                cert_serial=ident.cert_serial, tls=ident.tls, since=since, last_seen=since,
                booted=False))
        try:
            await self.run_strict(work)
            return True
        except IntegrityError:
            return False
        except SQLAlchemyError as exc:
            self.telemetry.dependency_error("database", type(exc).__name__)
            return True

    async def connection_owner(self, hier_id: str) -> dict | None:
        """The connection row of an id plus `pod_alive` (its replica heartbeats). None: no row."""
        def work(conn):
            row = conn.execute(
                select(connections, csms_pods.c.heartbeat_at)
                .select_from(connections.outerjoin(csms_pods, connections.c.pod == csms_pods.c.pod))
                .where(connections.c.hier_id == hier_id)).first()
            if row is None:
                return None
            owner = dict(row._mapping)
            owner["since"] = as_utc(owner["since"])
            beat = as_utc(owner.pop("heartbeat_at"))
            owner["pod_alive"] = owner["pod"] == self.pod or (
                beat is not None and (utcnow() - beat).total_seconds() <= self.stale_s)
            return owner
        return await self.run(work)

    async def connection_update(self, hier_id: str, **values) -> None:
        await self.run(lambda conn: conn.execute(
            update(connections).where(connections.c.hier_id == hier_id).values(**values)))

    async def connection_close(self, hier_id: str, since: dt.datetime) -> None:
        """Delete this replica's row for the id, only the one opened at `since`."""
        await self.run(lambda conn: conn.execute(delete(connections).where(
            connections.c.hier_id == hier_id, connections.c.pod == self.pod,
            connections.c.since == since)))

    async def connection_delete(self, owner: dict) -> None:
        """Delete exactly the row `connection_owner` returned (a stale or dead one)."""
        await self.run(lambda conn: conn.execute(delete(connections).where(
            connections.c.hier_id == owner["hier_id"], connections.c.pod == owner["pod"],
            connections.c.since == owner["since"])))

    async def list_connections(self, hier_id: str | None = None) -> list[dict]:
        """Connection rows (all live replicas) with the id of the open transaction, if any."""
        def work(conn):
            cutoff = utcnow() - dt.timedelta(seconds=self.stale_s)
            live = select(csms_pods.c.pod).where(csms_pods.c.heartbeat_at >= cutoff)
            query = (select(connections).where(connections.c.pod.in_(live))
                     .order_by(connections.c.hier_id))
            if hier_id:
                query = query.where(connections.c.hier_id == hier_id)
            rows = [dict(r._mapping) for r in conn.execute(query)]
            open_tx = dict(conn.execute(
                select(sessions.c.hier_id, func.max(sessions.c.transaction_id))
                .where(sessions.c.state == "open").group_by(sessions.c.hier_id)).all())
            for row in rows:
                row["since"] = as_utc(row["since"])
                row["last_seen"] = as_utc(row["last_seen"])
                row["active_transaction_id"] = open_tx.get(row["hier_id"])
            return rows
        return await self.run(work, default=[])

    async def ping(self) -> bool:
        """True if the database answers (for /health)."""
        return await self.run(lambda conn: conn.execute(text("SELECT 1")).scalar() == 1,
                              default=False)

    # ---- sessions ----------------------------------------------------------------------

    async def new_session(self, hier_id: str, connector_id: int, card: str, auth_status: str,
                          meter_start_wh: int, started_at: dt.datetime) -> int:
        """Insert a StartTransaction and return its id: the OCPP transactionId."""
        def work(conn):
            result = conn.execute(insert(sessions).values(
                hier_id=hier_id, connector_id=connector_id, card=card, auth_status=auth_status,
                meter_start_wh=meter_start_wh, started_at=started_at,
                state="open" if auth_status == "Accepted" else "rejected"))
            return result.inserted_primary_key[0]
        return await self.run_strict(work)

    async def close_session(self, transaction_id: int, meter_stop_wh: int, peak_power_w: float,
                            stopped_at: dt.datetime, reason: str) -> None:
        await self.run(lambda conn: conn.execute(
            update(sessions).where(sessions.c.transaction_id == transaction_id).values(
                state="closed", meter_stop_wh=meter_stop_wh, last_meter_wh=meter_stop_wh,
                last_meter_at=stopped_at, peak_power_w=peak_power_w, stopped_at=stopped_at,
                stop_reason=reason)))

    async def get_session(self, transaction_id: int) -> dict | None:
        def work(conn):
            row = conn.execute(select(sessions).where(
                sessions.c.transaction_id == transaction_id)).first()
            if row is None:
                return None
            data = dict(row._mapping)
            data["started_at"] = as_utc(data["started_at"])
            data["last_meter_at"] = as_utc(data["last_meter_at"])
            return data
        return await self.run(work)

    async def open_sessions_of(self, hier_id: str) -> list[dict]:
        """Open sessions of a charger, newest first (all replicas)."""
        def work(conn):
            rows = conn.execute(select(sessions).where(
                sessions.c.hier_id == hier_id, sessions.c.state == "open")
                .order_by(sessions.c.transaction_id.desc()))
            out = []
            for row in rows:
                data = dict(row._mapping)
                data["started_at"] = as_utc(data["started_at"])
                data["last_meter_at"] = as_utc(data["last_meter_at"])
                out.append(data)
            return out
        return await self.run(work, default=[])

    async def card_has_open_session(self, card: str) -> bool:
        """ConcurrentTx: does the card have an open transaction on any replica?"""
        return await self.run(lambda conn: conn.execute(
            select(sessions.c.transaction_id).where(
                sessions.c.card == card, sessions.c.state == "open").limit(1)).first() is not None,
            default=False)

    async def flush_meters(self, readings: list[dict]) -> set[int]:
        """One batched UPDATE with the last reading of each open transaction of this replica.

        `readings` items: transaction_id, last_meter_wh, last_meter_at, peak_power_w.
        Returns the ids that are no longer open in the database (closed by another replica).
        """
        def work(conn):
            ids = [r["transaction_id"] for r in readings]
            still_open = set(conn.execute(select(sessions.c.transaction_id).where(
                sessions.c.transaction_id.in_(ids), sessions.c.state == "open")).scalars())
            batch = [{"b_id": r["transaction_id"], "b_wh": r["last_meter_wh"],
                      "b_at": r["last_meter_at"], "b_peak": r["peak_power_w"]}
                     for r in readings if r["transaction_id"] in still_open]
            if batch:
                conn.execute(update(sessions).where(sessions.c.transaction_id == bindparam("b_id"))
                             .values(last_meter_wh=bindparam("b_wh"), last_meter_at=bindparam("b_at"),
                                     peak_power_w=bindparam("b_peak")), batch)
            return set(ids) - still_open
        return await self.run(work, default=set())

    async def notify(self, channel: str, payload: str) -> None:
        """Postgres NOTIFY (delivered when the transaction commits)."""
        await self.run(lambda conn: conn.execute(
            text("SELECT pg_notify(:channel, :payload)"), {"channel": channel, "payload": payload}))

    async def command_row(self, cmd_id: str) -> dict | None:
        def work(conn):
            row = conn.execute(select(commands).where(commands.c.cmd_id == cmd_id)).first()
            return dict(row._mapping) if row else None
        return await self.run(work)

    # ---- commands (audit trail) --------------------------------------------------------------

    async def command_requested(self, cmd_id: str, hier_id: str, command: str, args: str,
                                meta: dict, target_pod: str | None) -> None:
        await self.run(lambda conn: conn.execute(insert(commands).values(
            cmd_id=cmd_id, hier_id=hier_id, command=command, args=args,
            source=meta.get("source"), reason=meta.get("reason"),
            incident_id=meta.get("incident_id"), requested_at=utcnow(),
            target_pod=target_pod, status="pending")))

    async def command_done(self, cmd_id: str, status: str, result: str,
                           detail: str | None) -> None:
        await self.run(lambda conn: conn.execute(
            update(commands).where(commands.c.cmd_id == cmd_id).values(
                status=status, result=result, detail=detail, done_at=utcnow())))
