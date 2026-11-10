"""CLI entry point for the station simulator (docs/interfaces.md, section 9.2).

One process runs every charger of one station. Configuration is flags, with
the environment fallbacks of the table in 9.2.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import random
import re
import signal
import sys

from evcommon import hierid
from evcommon.jsonlog import setup_logging
from evcommon.models import CARD_REGEX
from evcommon.roster import load_roster

from . import __version__, attacks, metrics
from .charger import ChargerOptions, ChargerSim

_CARD_RE = re.compile(CARD_REGEX)


def _env(name: str, default: str | None = None) -> str | None:
    return os.environ.get(name, default)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="evsim", description="EV charging station simulator")
    p.add_argument("--roster", default=_env("SIM_ROSTER"))
    p.add_argument("--station", default=_env("SIM_STATION"))
    p.add_argument("--chargers", default=None, help="comma list of charger ids; default: all")
    p.add_argument("--csms-url", default=_env("SIM_CSMS_URL", "ws://127.0.0.1:9000/ocpp"))
    p.add_argument("--pki-dir", default=_env("SIM_PKI_DIR"))
    p.add_argument("--metrics-host", default="127.0.0.1")
    p.add_argument("--metrics-port", type=int, default=9300)
    p.add_argument("--log-dir", default=_env("LOG_DIR", "./logs"))
    p.add_argument("--meter-interval", type=float, default=10.0)
    p.add_argument("--arrival-rate", type=float, default=6.0)
    p.add_argument("--session-minutes", type=float, default=8.0)
    p.add_argument("--home-bias", type=float, default=1.0,
                   help="probability of drawing an unblocked card of the station's own city; "
                        "cards are always city-local (cross-city use is only the clone attack)")
    p.add_argument("--seed", type=int, default=None)
    p.add_argument("--duration", type=float, default=None)
    p.add_argument("--reconnect-max", type=float, default=30.0)
    p.add_argument("--reject-backoff", type=float, default=60.0)
    p.add_argument("--compromised", action="store_true", help="run the botnet client (9.4)")
    p.add_argument("--c2-url", default=None)
    p.add_argument("--beacon-interval", type=float, default=15.0)
    p.add_argument("--tamper", type=float, default=None, metavar="F",
                   help="under-report power and energy deltas by this factor, 0 < F < 1")
    p.add_argument("--tamper-mode", default="scale", choices=["scale", "rollback", "freeze"])
    p.add_argument("--tamper-chargers", default=None, help="comma list; default: all")
    p.add_argument("--clone-card", default=None, metavar="CARD")
    p.add_argument("--clone-sessions", type=int, default=3)
    p.add_argument("--bot-debug", action="store_true",
                   help="print botnet activity to stderr only, never to the JSON log")
    return p


def _fail(msg: str) -> int:
    print(f"evsim: {msg}", file=sys.stderr)
    return 2


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if not args.roster:
        return _fail("--roster is required")
    if not args.station:
        return _fail("--station is required")
    if args.compromised and not args.c2_url:
        return _fail("--compromised requires --c2-url")
    if args.tamper is not None and not (0 < args.tamper < 1):
        return _fail("--tamper must be a factor between 0 and 1")
    if args.clone_card is not None and not _CARD_RE.match(args.clone_card):
        return _fail("--clone-card is not a valid card id")

    try:
        roster = load_roster(args.roster)
    except Exception as exc:
        return _fail(f"cannot load roster {args.roster!r}: {exc}")

    try:
        roster.station(args.station)
    except Exception as exc:
        return _fail(f"unknown station {args.station!r}: {exc}")

    all_entries = [e for e in roster.iter_chargers() if hierid.is_under(e.hier_id, args.station)]
    if args.chargers:
        wanted = set(args.chargers.split(","))
        known = {e.charger.charger for e in all_entries}
        missing = wanted - known
        if missing:
            return _fail(f"unknown chargers for {args.station}: {sorted(missing)}")
        entries = [e for e in all_entries if e.charger.charger in wanted]
    else:
        entries = all_entries
    if not entries:
        return _fail(f"station {args.station} has no chargers")

    tamper_chargers = set(args.tamper_chargers.split(",")) if args.tamper_chargers else None

    metrics.init_exposition()
    log = setup_logging("sim", instance=args.station, log_dir=args.log_dir)
    log.info("sim.start", "station simulator started", hier_id=args.station, version=__version__)

    if args.metrics_port:
        import prometheus_client as pc
        pc.start_http_server(args.metrics_port, addr=args.metrics_host)

    seed = args.seed if args.seed is not None else random.SystemRandom().randrange(2**32)
    station_rng = random.Random(seed)
    station_labels = hierid.parse(args.station).labels()
    grid = metrics.GridMeter(station_labels)

    bot_state = attacks.BotState() if args.compromised else None
    clone_state = attacks.CloneState(args.clone_card, args.clone_sessions) if args.clone_card else None

    opts = ChargerOptions(csms_url=args.csms_url, pki_dir=args.pki_dir,
                          reconnect_max=args.reconnect_max, reject_backoff=args.reject_backoff,
                          meter_interval=args.meter_interval, arrival_rate=args.arrival_rate,
                          session_minutes=args.session_minutes, home_bias=args.home_bias)

    sims: list[ChargerSim] = []
    for entry in entries:
        tamper_cfg = None
        if args.tamper is not None and (tamper_chargers is None or entry.charger.charger in tamper_chargers):
            tamper_cfg = attacks.TamperConfig(mode=args.tamper_mode, factor=args.tamper)
        charger_rng = random.Random(station_rng.random())
        sims.append(ChargerSim(entry, roster, opts, charger_rng, log, grid, bot_state=bot_state,
                               clone_state=clone_state, tamper=tamper_cfg))

    try:
        asyncio.run(_run_station(sims, args, bot_state, log))
    except KeyboardInterrupt:
        pass
    log.info("sim.start", "station simulator stopped", hier_id=args.station, version=__version__)
    return 0


async def _run_station(sims: list[ChargerSim], args: argparse.Namespace,
                       bot_state: attacks.BotState | None, log) -> None:
    stopping = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stopping.set)
        except (NotImplementedError, AttributeError):
            pass  # not available on this platform; --duration still works

    tasks = [asyncio.create_task(s.run(stopping)) for s in sims]

    if args.compromised and bot_state is not None:
        async def on_surge(duration_s: float) -> None:
            await asyncio.gather(*(s.force_session(cid, duration_s, stopping)
                                   for s in sims for cid in s.connector_ids),
                                 return_exceptions=True)

        tasks.append(asyncio.create_task(attacks.run_botnet(
            args.station, [s.hier_id for s in sims], args.c2_url, args.beacon_interval,
            bot_state, on_surge, stopping, debug_enabled=args.bot_debug)))

    if args.duration is not None:
        async def _timer() -> None:
            await asyncio.sleep(args.duration)
            stopping.set()
        tasks.append(asyncio.create_task(_timer()))

    try:
        await stopping.wait()
    finally:
        await asyncio.gather(*tasks, return_exceptions=True)


if __name__ == "__main__":
    sys.exit(main())
