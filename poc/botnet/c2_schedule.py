#!/usr/bin/env python3
"""Thin scheduler around sim/tests/stub_c2.py for the botnet PoC (poc/botnet/deploy_c2.sh).

docs/interfaces.md v6 9.4: the stub answers every beacon with one fixed --task for the
life of the process. This wrapper does not re-implement the beacon protocol -- it just
restarts the stub with the next task at each phase boundary, so the compromised fleet is
walked through the fixed schedule idle -> flood -> surge -> stop without touching sim/.
Standard library only; run next to a copy of stub_c2.py (deploy_c2.sh pushes both files
into the same directory inside the throwaway `c2` container).

    c2_schedule.py --port 8099 --idle-s 30 --flood-s 60 --flood-rate-hz 20 \
                    --surge-s 120 --stop-s 30
"""

from __future__ import annotations

import argparse
import pathlib
import signal
import subprocess
import sys
import time

STUB = pathlib.Path(__file__).resolve().parent / "stub_c2.py"

_child: subprocess.Popen | None = None


def _stop_child() -> None:
    global _child
    if _child is not None and _child.poll() is None:
        _child.terminate()
        try:
            _child.wait(timeout=5)
        except subprocess.TimeoutExpired:
            _child.kill()
    _child = None


def _handle_signal(_signum, _frame) -> None:
    _stop_child()
    sys.exit(0)


def run_phase(host: str, port: int, task: str, rate_hz: float, duration_s: float, hold_s: float) -> None:
    global _child
    args = [sys.executable, str(STUB), "--host", host, "--port", str(port), "--task", task]
    if task == "flood":
        args += ["--rate-hz", str(rate_hz), "--duration-s", str(duration_s)]
    elif task == "surge":
        args += ["--duration-s", str(duration_s)]
    print(f"c2_schedule: phase={task} hold={hold_s:g}s", flush=True)
    _child = subprocess.Popen(args)
    time.sleep(hold_s)
    _stop_child()


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    # empty string binds every interface (the same INADDR_ANY socket.bind() accepts),
    # so the C2 is reachable from the other lab instances, not just this one
    p.add_argument("--host", default="")
    p.add_argument("--port", type=int, default=8099)
    p.add_argument("--idle-s", type=float, default=30.0)
    p.add_argument("--flood-s", type=float, default=60.0)
    p.add_argument("--flood-rate-hz", type=float, default=20.0)
    p.add_argument("--surge-s", type=float, default=120.0)
    p.add_argument("--stop-s", type=float, default=30.0)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    if not STUB.exists():
        print(f"c2_schedule: stub not found at {STUB}", file=sys.stderr)
        return 2

    run_phase(args.host, args.port, "idle", 0.0, 0.0, args.idle_s)
    run_phase(args.host, args.port, "flood", args.flood_rate_hz, args.flood_s, args.flood_s)
    run_phase(args.host, args.port, "surge", 0.0, args.surge_s, args.surge_s)
    run_phase(args.host, args.port, "stop", 0.0, 0.0, args.stop_s)
    print("c2_schedule: schedule complete", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
