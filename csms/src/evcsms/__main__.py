"""Start the CSMS: the OCPP WebSocket server and the HTTP API in one asyncio loop."""

from __future__ import annotations

import asyncio
import contextlib
import signal

import uvicorn
import websockets.asyncio.server as ws_server
from evcommon import jsonlog
from evcommon import metrics as metric_specs

from . import __version__
from .api import create_app
from .billing import HttpBilling, LocalBilling
from .bus import make_bus
from .chargepoint import serve_connection
from .context import Context
from .db import Db
from .fraud import FraudSender
from .identity import IdentityChecker
from .registry_client import RegistryClient
from .sessions import SessionTable
from .settings import Settings
from .telemetry import Metrics, Telemetry


class ApiServer(uvicorn.Server):
    """uvicorn without its own signal handlers: `main` decides how the process stops."""

    @contextlib.contextmanager
    def capture_signals(self):
        yield


async def every(seconds: float, job) -> None:
    """Call `await job()` every `seconds` until cancelled."""
    while True:
        await asyncio.sleep(seconds)
        await job()


async def flush_meters(ctx: Context) -> None:
    """Write the last reading of this replica's open transactions to `sessions` (one UPDATE)."""
    open_here = ctx.sessions.all()
    if open_here:
        gone = await ctx.db.flush_meters([
            {"transaction_id": tx.transaction_id, "last_meter_wh": tx.last_meter_wh,
             "last_meter_at": tx.last_ts, "peak_power_w": tx.peak_power_w} for tx in open_here])
        ctx.sessions.forget(gone)            # closed meanwhile by another replica (reboot)


async def run(settings: Settings, registry=None, billing=None, *,
              stop: asyncio.Event | None = None, ready: asyncio.Event | None = None) -> None:
    """Run both servers until `stop` is set. `registry` and `billing` can be replaced (tests)."""
    log = jsonlog.get_logger()
    metrics = Metrics()
    telemetry = Telemetry(log, metrics)
    db = Db(settings.database_url, settings.instance, telemetry, settings.pod_stale_s)
    schema_added = db.start_pod()
    if registry is None:
        registry = RegistryClient(settings.registry_url, settings.registry_cache_s, telemetry)
    if billing is None:
        billing = (HttpBilling(settings.billing_url, settings.billing_token, telemetry)
                   if settings.billing_url else LocalBilling(settings.blocked_cards))
    fraud = FraudSender(settings.fraud_url, settings.fraud_token, telemetry)
    ctx = Context(settings, log, metrics, telemetry, db, registry, billing, fraud, SessionTable())
    bus = make_bus(ctx)
    checker = IdentityChecker(ctx, bus)
    app = create_app(ctx, bus)
    api = ApiServer(uvicorn.Config(app, host=settings.bind_host, port=settings.api_port,
                                   log_config=None, access_log=False))
    stop = stop or asyncio.Event()

    fraud.start()
    await bus.start()
    background = [asyncio.create_task(every(settings.pod_heartbeat_s, db.pod_heartbeat)),
                  asyncio.create_task(every(settings.meter_flush_s, lambda: flush_meters(ctx)))]
    api_task = asyncio.create_task(api.serve())
    async with ws_server.serve(
            lambda ws: serve_connection(ctx, ws), settings.bind_host, settings.ocpp_port,
            subprotocols=["ocpp1.6"], process_request=checker.process_request,
            max_size=65536, ping_interval=20, ping_timeout=20, close_timeout=3,
            compression=None):
        while not api.started and not api_task.done():
            await asyncio.sleep(0.05)
        note = f", added columns: {', '.join(schema_added)}" if schema_added else ""
        log.event("service.start", f"csms started (ocpp {settings.ocpp_port}, "
                  f"api {settings.api_port}{note})", version=__version__)
        if ready:
            ready.set()
        stopper = asyncio.create_task(stop.wait())
        await asyncio.wait({stopper, api_task}, return_when=asyncio.FIRST_COMPLETED)
    # leaving the `async with` closed the OCPP server and its connections
    checker.close()
    for task in background:
        task.cancel()
    await asyncio.gather(*background, return_exceptions=True)
    await flush_meters(ctx)                  # bounded loss: write the last readings once more
    api.should_exit = True
    await asyncio.gather(api_task, return_exceptions=True)
    await ctx.drain()
    await bus.close()
    await db.stop_pod()
    await fraud.stop()
    await billing.aclose()
    if hasattr(registry, "aclose"):
        await registry.aclose()
    db.close()
    log.event("service.stop", "csms stopped", version=__version__)


async def _main() -> None:
    jsonlog.setup_logging("csms")
    metric_specs.init_exposition()
    settings = Settings.from_env()
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    await run(settings, stop=stop)


def main() -> None:
    asyncio.run(_main())


if __name__ == "__main__":
    main()
