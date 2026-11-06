"""A minimal accept-everything OCPP 1.6 server, for testing chargers against.

Prints one line per received message to stdout. Transaction ids increment.
Used both as a library by the pytest tests (`create_server`) and as a CLI:

    python sim/tests/stub_csms.py --port 9000 [--block-card CARD]... \\
        [--reject-path-substr X] [--boot-interval 30]
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import sys
from http import HTTPStatus

from ocpp.routing import on
from ocpp.v16 import ChargePoint as _CP
from ocpp.v16 import call_result
from ocpp.v16.enums import AuthorizationStatus, RegistrationStatus
from websockets.asyncio import server as ws_server


def _iso_now() -> str:
    now = dt.datetime.now(dt.UTC)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


class StubChargePoint(_CP):
    """Accepts everything except the configured blocked cards."""

    def __init__(self, charger_id, connection, blocked_cards: set[str],
                tx_counter: list[int], boot_interval: int, on_message):
        super().__init__(charger_id, connection, response_timeout=10)
        self._blocked = blocked_cards
        self._tx_counter = tx_counter
        self._boot_interval = boot_interval
        self._on_message = on_message

    @on("BootNotification")
    def on_boot(self, **kwargs):
        self._on_message("BootNotification", self.id, kwargs)
        return call_result.BootNotification(current_time=_iso_now(), interval=self._boot_interval,
                                            status=RegistrationStatus.accepted.value)

    @on("Heartbeat")
    def on_heartbeat(self, **kwargs):
        self._on_message("Heartbeat", self.id, kwargs)
        return call_result.Heartbeat(current_time=_iso_now())

    @on("StatusNotification")
    def on_status(self, **kwargs):
        self._on_message("StatusNotification", self.id, kwargs)
        return call_result.StatusNotification()

    @on("Authorize")
    def on_authorize(self, id_tag, **kwargs):
        self._on_message("Authorize", self.id, {**kwargs, "id_tag": id_tag})
        status = AuthorizationStatus.blocked.value if id_tag in self._blocked \
            else AuthorizationStatus.accepted.value
        return call_result.Authorize(id_tag_info={"status": status})

    @on("StartTransaction")
    def on_start(self, connector_id, id_tag, meter_start, **kwargs):
        self._on_message("StartTransaction", self.id,
                         {**kwargs, "connector_id": connector_id, "id_tag": id_tag,
                          "meter_start": meter_start})
        self._tx_counter[0] += 1
        status = AuthorizationStatus.blocked.value if id_tag in self._blocked \
            else AuthorizationStatus.accepted.value
        return call_result.StartTransaction(transaction_id=self._tx_counter[0],
                                            id_tag_info={"status": status})

    @on("MeterValues")
    def on_meter_values(self, **kwargs):
        self._on_message("MeterValues", self.id, kwargs)
        return call_result.MeterValues()

    @on("StopTransaction")
    def on_stop(self, **kwargs):
        self._on_message("StopTransaction", self.id, kwargs)
        return call_result.StopTransaction(id_tag_info={"status": AuthorizationStatus.accepted.value})


def create_server(host: str, port: int, blocked_cards: set[str] | None = None,
                  reject_path_substr: str | None = None, boot_interval: int = 30,
                  on_message=None):
    """Returns the (not yet entered) `websockets.asyncio.server.serve()`
    context manager for the stub CSMS, for reuse from the pytest tests."""
    blocked = blocked_cards or set()
    tx_counter = [0]
    printer = on_message or _print_message

    async def process_request(connection, request):
        if reject_path_substr and reject_path_substr in request.path:
            return connection.respond(HTTPStatus.FORBIDDEN, "rejected for testing\n")
        return None

    async def handler(ws):
        charger_id = ws.request.path.rsplit("/", 1)[-1]
        cp = StubChargePoint(charger_id, ws, blocked, tx_counter, boot_interval, printer)
        try:
            await cp.start()
        except Exception:
            pass

    return ws_server.serve(handler, host, port, subprotocols=["ocpp1.6"],
                           process_request=process_request, max_size=65536,
                           ping_interval=20, ping_timeout=20)


def _print_message(action: str, charger_id: str, payload: dict) -> None:
    print(f"{action} {charger_id} {payload}", flush=True)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="minimal accept-everything OCPP 1.6 server")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=9000)
    p.add_argument("--block-card", action="append", default=[], dest="block_cards")
    p.add_argument("--reject-path-substr", default=None)
    p.add_argument("--boot-interval", type=int, default=30)
    return p


async def _main(argv=None) -> None:
    args = build_parser().parse_args(argv)
    async with create_server(args.host, args.port, set(args.block_cards),
                             args.reject_path_substr, args.boot_interval):
        print(f"stub_csms listening on ws://{args.host}:{args.port}/ocpp", flush=True)
        await asyncio.Future()


if __name__ == "__main__":
    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        sys.exit(0)
