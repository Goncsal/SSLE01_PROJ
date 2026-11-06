"""Roster queries and registry registration for one city.

Runs inside the city's edge instance, in the image venv (evcommon, httpx).

  register_city.py list --roster R --city C
      TSV: "edge<TAB>lat<TAB>lon" then "station<TAB>st-001<TAB>lat<TAB>lon<TAB>cp-1,cp-2"
  register_city.py register --roster R --city C --registry URL [--registry URL ...]
      JSON on stdin: {"token": ..., "edge": {"ip": ..., "cert_serial": ...},
                      "stations": {"st-001": "<ip>"}, "serials": {"<charger hier-id>": "<SERIAL>"}}
      The token is only read from stdin, never from arguments or files.
"""
from __future__ import annotations

import argparse
import json
import sys

import httpx
from evcommon import hierid
from evcommon.roster import load_roster


def cmd_list(args: argparse.Namespace) -> int:
    city = load_roster(args.roster).city(args.city)
    first = city.stations[0]
    elat = city.edge_lat if city.edge_lat is not None else first.lat
    elon = city.edge_lon if city.edge_lon is not None else first.lon
    print(f"edge\t{elat}\t{elon}")
    for st in city.stations:
        print("station\t{}\t{}\t{}\t{}".format(
            st.station, st.lat, st.lon, ",".join(c.charger for c in st.chargers)))
    return 0


class Registry:
    def __init__(self, urls: list[str], token: str) -> None:
        self.headers = {"Authorization": f"Bearer {token}"}
        self.urls = urls
        self.base: str | None = None
        for url in urls:
            try:
                httpx.get(f"{url}/whoami", headers=self.headers, timeout=5).raise_for_status()
                self.base = url
                break
            except httpx.HTTPError:
                continue
        if self.base is None:
            raise SystemExit("no registry reachable: " + ", ".join(urls))

    def put(self, hid: str, body: dict) -> int:
        body = {k: v for k, v in body.items() if v is not None}
        r = httpx.put(f"{self.base}/nodes/{hid}", headers=self.headers, json=body, timeout=10)
        if r.status_code not in (200, 201):
            raise SystemExit(f"PUT {hid}: {r.status_code} {r.text}")
        return r.status_code


def cmd_register(args: argparse.Namespace) -> int:
    data = json.loads(sys.stdin.read())
    city = load_roster(args.roster).city(args.city)
    reg = Registry(args.registry, data["token"])
    net = f"city-{args.city}"
    zone = f"{args.city}.pt.lab"
    counts = {"created": 0, "replaced": 0}

    def put(hid: str, body: dict) -> None:
        counts["created" if reg.put(hid, body) == 201 else "replaced"] += 1

    edge = data["edge"]
    put(hierid.edge(args.city), {
        "name": f"{city.name or args.city} edge gateway",
        "lat": city.edge_lat if city.edge_lat is not None else city.stations[0].lat,
        "lon": city.edge_lon if city.edge_lon is not None else city.stations[0].lon,
        "host": f"{args.city}-edge", "ip": edge.get("ip"), "network": net,
        "dns": f"edge.{zone}", "metrics_target": f"edge.{zone}:9113",
        "cert_serial": edge.get("cert_serial"),
    })
    for st in city.stations:
        sid = hierid.station(args.city, st.station)
        put(sid, {
            "name": st.name, "lat": st.lat, "lon": st.lon,
            "host": f"{args.city}-{st.station}", "ip": data["stations"].get(st.station),
            "network": net, "dns": f"{st.station}.{zone}",
            "metrics_target": f"{st.station}.{zone}:9300",
        })
        for ch in st.chargers:
            cid = hierid.charger(args.city, st.station, ch.charger)
            put(cid, {
                "name": f"{st.name} {ch.charger}", "max_kw": ch.max_kw,
                "connectors": ch.connectors, "vendor": ch.vendor, "model": ch.model,
                "serial": ch.serial, "cert_serial": data["serials"].get(cid),
            })
    print(f"{args.city}: registered via {reg.base}: {counts['created']} created, "
          f"{counts['replaced']} replaced")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    for name, fn in (("list", cmd_list), ("register", cmd_register)):
        sp = sub.add_parser(name)
        sp.add_argument("--roster", required=True)
        sp.add_argument("--city", required=True)
        if name == "register":
            sp.add_argument("--registry", action="append", required=True)
        sp.set_defaults(fn=fn)
    args = p.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
