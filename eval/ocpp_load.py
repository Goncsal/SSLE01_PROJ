"""Synthetic OCPP 1.6J connection-load generator (benign, no sessions, no meter data).

Each synthetic charger connects, sends BootNotification and one StatusNotification per
connector, then sends Heartbeat every --hb-interval seconds. It never starts a transaction,
so no card, billing or grid-meter data is touched and the fraud rules have nothing to judge.
It runs inside a station container (venv of the sim image) so the traffic takes the real path
station -> edge (nginx) -> CSMS. Ids must already exist in the registry.

  ocpp_load.py --station pt.aveiro.st-001 --first 3 --count 10 --url ws://edge.aveiro.pt.lab/ocpp
"""
import argparse
import asyncio
import datetime as dt
import json
import sys
import time

from ocpp.v16 import ChargePoint, call
from websockets.asyncio import client

STATS = {"connected": 0, "boots": 0, "hb": 0, "errors": 0, "refused": 0}
LAT: list[float] = []


class CP(ChargePoint):
    pass


async def one(url, hid, serial, hb, stop):
    backoff = 1.0
    while not stop.is_set():
        try:
            async with client.connect(f"{url}/{hid}", subprotocols=["ocpp1.6"], open_timeout=10,
                                      ping_interval=20, ping_timeout=20) as ws:
                cp = CP(hid, ws, response_timeout=10)
                pump = asyncio.create_task(cp.start())
                try:
                    await cp.call(call.BootNotification(
                        charge_point_vendor="EVLab", charge_point_model="EVL-AC22",
                        charge_point_serial_number=serial, firmware_version="1.0.0"))
                    STATS["boots"] += 1
                    for cid in (0, 1):
                        await cp.call(call.StatusNotification(
                            connector_id=cid, error_code="NoError", status="Available"))
                    STATS["connected"] += 1
                    backoff = 1.0
                    while not stop.is_set():
                        t0 = time.monotonic()
                        await cp.call(call.Heartbeat())
                        LAT.append(time.monotonic() - t0)
                        STATS["hb"] += 1
                        try:
                            await asyncio.wait_for(stop.wait(), hb)
                        except asyncio.TimeoutError:
                            pass
                finally:
                    STATS["connected"] -= 1 if STATS["connected"] else 0
                    pump.cancel()
        except Exception as exc:
            STATS["refused" if "429" in str(exc) or "HTTP" in str(exc) else "errors"] += 1
            if stop.is_set():
                break
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30.0)


async def main():
    p = argparse.ArgumentParser()
    p.add_argument("--station", required=True)
    p.add_argument("--first", type=int, default=3)
    p.add_argument("--count", type=int, required=True)
    p.add_argument("--url", required=True)
    p.add_argument("--hb-interval", type=float, default=5.0)
    p.add_argument("--stagger", type=float, default=0.6, help="seconds between connects (edge limit_req)")
    p.add_argument("--duration", type=float, default=None)
    a = p.parse_args()
    stop = asyncio.Event()
    tasks = []
    for i in range(a.first, a.first + a.count):
        tasks.append(asyncio.create_task(one(a.url, f"{a.station}.cp-{i}", f"EVLOAD-{i:04d}",
                                             a.hb_interval, stop)))
        await asyncio.sleep(a.stagger)
    t_end = time.monotonic() + a.duration if a.duration else None
    while not stop.is_set() and (t_end is None or time.monotonic() < t_end):
        await asyncio.sleep(10)
        lat = sorted(LAT[-500:])
        print(json.dumps({"t": dt.datetime.now(dt.UTC).isoformat(), **STATS,
                          "hb_p95_s": lat[int(len(lat) * .95)] if lat else None}), flush=True)
    stop.set()
    await asyncio.gather(*tasks, return_exceptions=True)


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
