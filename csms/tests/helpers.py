"""Test helpers: a fake registry, a running CSMS, and a charger client."""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import json
import socket
from dataclasses import dataclass, replace

import httpx
import websockets.asyncio.client as ws_client
from evcommon import jsonlog
from evcommon.models import Node
from evcsms.__main__ import run
from evcsms.registry_client import RegistryError
from evcsms.settings import Settings
from ocpp.routing import on
from ocpp.v16 import ChargePoint, call, call_result
from ocpp.v16.enums import Action, AvailabilityStatus, RemoteStartStopStatus

API_TOKEN = "test-token-0123456789abcdef"
CHARGER = "pt.porto.st-003.cp-2"
CARD = "04A1B2C3D4E5F6"
OTHER_CARD = "04FFEEDDCCBBAA"


def make_node(hier_id: str = CHARGER, **overrides) -> Node:
    now = dt.datetime.now(dt.UTC)
    kind = "charger" if hier_id.count(".") == 3 else "station"
    values = dict(hier_id=hier_id, kind=kind, state="active", effective_state="active",
                  created_at=now, updated_at=now, updated_by="admin", max_kw=22.0,
                  serial="EVL22-0032")
    values.update(overrides)
    return Node(**values)


class FakeRegistry:
    """Stands in for the registry client: a dict of nodes, and a switch to take it down."""

    def __init__(self, *nodes: Node):
        self.nodes = {n.hier_id: n for n in nodes}
        self.down = False
        self.dropped: list[str] = []

    async def get(self, hier_id: str):
        if self.down:
            raise RegistryError("registry is down")
        return self.nodes.get(hier_id)

    def drop(self, hier_id: str) -> None:
        self.dropped.append(hier_id)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@dataclass
class Server:
    settings: Settings
    log_file: str

    def url(self, hier_id: str = CHARGER) -> str:
        return f"ws://127.0.0.1:{self.settings.ocpp_port}/ocpp/{hier_id}"

    @property
    def api(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(base_url=f"http://127.0.0.1:{self.settings.api_port}",
                                 headers={"Authorization": f"Bearer {API_TOKEN}"})

    def events(self, name: str) -> list[dict]:
        with open(self.log_file, encoding="utf-8") as f:
            lines = [json.loads(line) for line in f if line.strip()]
        return [line for line in lines if line["event"] == name]


@contextlib.asynccontextmanager
async def running(tmp_path, registry=None, billing=None, probe_timeout_s=None, **env):
    """Start the real CSMS (both servers) on free ports; stop it afterwards."""
    env = {"CSMS_API_TOKEN": API_TOKEN, "CSMS_ALLOW_DIRECT": "true", "INSTANCE_ID": "test",
           "CSMS_OCPP_LISTEN_PORT": str(free_port()), "CSMS_API_LISTEN_PORT": str(free_port()),
           "DATABASE_URL": f"sqlite:///{tmp_path}/csms.db", **env}
    settings = Settings.from_env(env)
    if probe_timeout_s is not None:
        settings = replace(settings, probe_timeout_s=probe_timeout_s)
    jsonlog.setup_logging("csms", instance="test", log_dir=tmp_path, stdout=False)
    stop, ready = asyncio.Event(), asyncio.Event()
    registry = registry if registry is not None else FakeRegistry(make_node())
    task = asyncio.create_task(run(settings, registry, billing, stop=stop, ready=ready))
    await asyncio.wait_for(ready.wait(), 10)
    try:
        yield Server(settings, str(jsonlog.log_path("csms", "test", tmp_path)))
    finally:
        stop.set()
        await asyncio.wait_for(task, 10)


class Charger(ChargePoint):
    """A charger client that accepts the commands the CSMS sends."""

    @on(Action.change_availability)
    async def on_change_availability(self, connector_id, type, **kwargs):
        return call_result.ChangeAvailability(status=AvailabilityStatus.accepted)

    @on(Action.remote_stop_transaction)
    async def on_remote_stop(self, transaction_id, **kwargs):
        return call_result.RemoteStopTransaction(status=RemoteStartStopStatus.accepted)

    # ---- the calls a charger makes ----------------------------------------------------

    async def boot(self, serial="EVL22-0032"):
        return await self.call(call.BootNotification(
            charge_point_vendor="EVLab", charge_point_model="EVL-AC22",
            charge_point_serial_number=serial, firmware_version="1.0"))

    async def status(self, status="Available", connector_id=1):
        return await self.call(call.StatusNotification(
            connector_id=connector_id, error_code="NoError", status=status, timestamp=now()))

    async def authorize(self, card=CARD):
        return await self.call(call.Authorize(id_tag=card))

    async def start_tx(self, card=CARD, meter_start=1000):
        return await self.call(call.StartTransaction(
            connector_id=1, id_tag=card, meter_start=meter_start, timestamp=now()))

    async def meter(self, transaction_id, energy_wh, power_w, after_s=10):
        """A MeterValues stamped `after_s` seconds after now (the readings are 10 s apart)."""
        sample = {"context": "Sample.Periodic"}
        return await self.call(call.MeterValues(
            connector_id=1, transaction_id=transaction_id,
            meter_value=[{"timestamp": now(after_s), "sampled_value": [
                {**sample, "value": str(energy_wh), "measurand": "Energy.Active.Import.Register",
                 "unit": "Wh"},
                {**sample, "value": str(power_w), "measurand": "Power.Active.Import",
                 "unit": "W"}]}]))

    async def stop_tx(self, transaction_id, meter_stop):
        return await self.call(call.StopTransaction(
            transaction_id=transaction_id, meter_stop=meter_stop, timestamp=now(), reason="Local"))


def now(after_s: float = 0) -> str:
    return (dt.datetime.now(dt.UTC) + dt.timedelta(seconds=after_s)).isoformat()


@contextlib.asynccontextmanager
async def connected(server: Server, hier_id: str = CHARGER, headers: dict | None = None,
                    boot: bool = True):
    """A charger connected to the CSMS (booted unless `boot` is False)."""
    async with ws_client.connect(server.url(hier_id), subprotocols=["ocpp1.6"],
                                 additional_headers=headers or {}, ping_interval=None) as ws:
        charger = Charger(hier_id, ws)
        task = asyncio.create_task(charger.start())
        try:
            if boot:
                await charger.boot()
            yield charger
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def refused_status(server: Server, hier_id: str = CHARGER, headers: dict | None = None,
                         path: str | None = None) -> int | None:
    """HTTP status of a refused handshake (None if the charger was accepted)."""
    from websockets.exceptions import InvalidStatus
    url = server.url(hier_id) if path is None else f"ws://127.0.0.1:{server.settings.ocpp_port}{path}"
    try:
        async with ws_client.connect(url, subprotocols=["ocpp1.6"],
                                     additional_headers=headers or {}):
            return None
    except InvalidStatus as exc:
        return exc.response.status_code


def edge_headers(hier_id: str = CHARGER, edge: str = "pt.porto.edge", serial: str = "1A2B3C",
                 verify: str = "SUCCESS", cn: str | None = None, real_ip: str = "10.31.0.13") -> dict:
    """The headers the edge nginx would set."""
    return {"X-Real-IP": real_ip, "X-Edge-Id": edge, "X-Client-Cert-Verify": verify,
            "X-Client-Cert-DN": f"CN={cn or hier_id},OU=porto,O=EVCharge Lab",
            "X-Client-Cert-Serial": serial}


# ---- direct database access for the tests ----------------------------------------------

def table_rows(server: Server, table: str, **where) -> list[dict]:
    """Rows of a CSMS table, read with a separate connection."""
    from evcsms import db
    from sqlalchemy import create_engine, select
    tbl = db.metadata.tables[table]
    engine = create_engine(server.settings.database_url)
    try:
        with engine.begin() as conn:
            query = select(tbl)
            for column, value in where.items():
                query = query.where(tbl.c[column] == value)
            return [dict(r._mapping) for r in conn.execute(query)]
    finally:
        engine.dispose()


def insert_row(database_url: str, table: str, **values) -> None:
    from evcsms import db
    from sqlalchemy import create_engine, insert
    engine = create_engine(database_url)
    try:
        with engine.begin() as conn:
            conn.execute(insert(db.metadata.tables[table]).values(**values))
    finally:
        engine.dispose()


def open_session_row(database_url: str, hier_id: str = CHARGER, card: str = CARD,
                     meter_start_wh: int = 1000, **values) -> int:
    """An open session as another replica would have written it. Returns its transaction id."""
    from evcsms import db
    from sqlalchemy import create_engine, insert
    engine = create_engine(database_url)
    try:
        with engine.begin() as conn:
            result = conn.execute(insert(db.sessions).values(
                hier_id=hier_id, connector_id=1, card=card, auth_status="Accepted",
                meter_start_wh=meter_start_wh, started_at=dt.datetime.now(dt.UTC),
                state="open", **values))
            return result.inserted_primary_key[0]
    finally:
        engine.dispose()
