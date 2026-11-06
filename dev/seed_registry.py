"""Load the roster into the registry with idempotent PUTs.

Order: the city nodes with the admin token; then, with each city's own token
(this exercises delegation), the city edge, its stations and their chargers.
Bodies are validated with evcommon.models.NodeIn before anything is sent.

Environment:
    REGISTRY_URL            default http://127.0.0.1:8001
    REGISTRY_ADMIN_TOKEN    NOC token (city nodes)
    REGISTRY_TOKEN_<CITY>   one token per city, e.g. REGISTRY_TOKEN_PORTO

    python dev/seed_registry.py --roster dev/roster.yaml --local
    python dev/seed_registry.py --roster dev/roster.yaml --dry-run
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import sys
from pathlib import Path
from typing import Any

import httpx
from pydantic import ValidationError

from evcommon import hierid
from evcommon.models import NodeIn
from evcommon.roster import Roster, load_roster

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ROSTER = REPO_ROOT / "dev" / "roster.yaml"
DEFAULT_REGISTRY_URL = "http://127.0.0.1:8001"
CONTAINER_METRICS_PORT = 9300
REDACTED = "<redacted>"


@dataclasses.dataclass(frozen=True)
class Request:
    kind: str           # city | edge | station | charger
    hier_id: str
    token_label: str    # "admin" or "city:<name>"
    token_env: str      # environment variable holding the token
    body: dict[str, Any]

    @property
    def path(self) -> str:
        return f"/nodes/{self.hier_id}"


def _token_env(city: str) -> str:
    return "REGISTRY_TOKEN_" + city.upper().replace("-", "_")


def build_plan(roster: Roster, local: bool) -> list[Request]:
    plan: list[Request] = []
    for city in roster.cities:
        plan.append(Request(
            kind="city", hier_id=hierid.city(city.city), token_label="admin",
            token_env="REGISTRY_ADMIN_TOKEN",
            body={"name": city.name or city.city.capitalize()},
        ))
    for city in roster.cities:
        tenv = _token_env(city.city)
        tlabel = f"city:{city.city}"
        edge = hierid.edge(city.city)
        edge_body: dict[str, Any] = {
            "name": f"{city.name or city.city.capitalize()} edge gateway",
            "host": hierid.parse(edge).incus_instance(),
            "network": hierid.parse(edge).incus_network(),
            "dns": hierid.parse(edge).dns_name(),
        }
        if city.edge_lat is not None and city.edge_lon is not None:
            edge_body["lat"] = city.edge_lat
            edge_body["lon"] = city.edge_lon
        plan.append(Request("edge", edge, tlabel, tenv, edge_body))

        for station_id, _, st in (x for x in roster.iter_stations() if x[1].city == city.city):
            sh = hierid.parse(station_id)
            if local:
                if st.dev_metrics_port is None:
                    raise ValueError(f"{station_id}: --local needs dev_metrics_port in the roster")
                metrics = f"127.0.0.1:{st.dev_metrics_port}"
            else:
                metrics = f"{sh.dns_name()}:{CONTAINER_METRICS_PORT}"
            plan.append(Request("station", station_id, tlabel, tenv, {
                "name": st.name,
                "lat": st.lat,
                "lon": st.lon,
                "host": sh.incus_instance(),
                "network": sh.incus_network(),
                "dns": sh.dns_name(),
                "metrics_target": metrics,
            }))
            for ch in st.chargers:
                cid = hierid.charger(city.city, st.station, ch.charger)
                body: dict[str, Any] = {
                    "max_kw": ch.max_kw,
                    "connectors": ch.connectors,
                    "vendor": ch.vendor,
                    "model": ch.model,
                }
                if ch.serial:
                    body["serial"] = ch.serial
                plan.append(Request("charger", cid, tlabel, tenv, body))

    # Validate every body exactly as the registry will.
    for req in plan:
        NodeIn.model_validate(req.body)
    return plan


def _print_dry_run(plan: list[Request], base_url: str) -> None:
    for req in plan:
        body = NodeIn.model_validate(req.body).model_dump(exclude_none=True)
        print(f"PUT {base_url}{req.path}  [{req.token_label}]  {json.dumps(body, ensure_ascii=False)}")
    print(f"dry run: {len(plan)} requests, nothing sent")


def _send(plan: list[Request], base_url: str, tokens: dict[str, str]) -> int:
    counts: dict[str, dict[str, int]] = {}
    failures = 0
    with httpx.Client(base_url=base_url, timeout=10.0) as client:
        for req in plan:
            body = NodeIn.model_validate(req.body).model_dump(exclude_none=True)
            try:
                resp = client.put(req.path, json=body,
                                  headers={"Authorization": f"Bearer {tokens[req.token_env]}"})
            except httpx.TransportError as exc:
                print(f"error: cannot reach the registry at {base_url}: {exc}", file=sys.stderr)
                print("start it first (dev/run_local.sh), or set REGISTRY_URL", file=sys.stderr)
                return 1
            stat = counts.setdefault(req.kind, {"ok": 0, "failed": 0})
            if 200 <= resp.status_code < 300:
                stat["ok"] += 1
            else:
                stat["failed"] += 1
                failures += 1
                detail = resp.text[:200].replace("\n", " ")
                print(f"FAIL PUT {req.path} [{req.token_label}]: {resp.status_code} {detail}",
                      file=sys.stderr)
    for kind in ("city", "edge", "station", "charger"):
        if kind in counts:
            s = counts[kind]
            print(f"{kind:8s} ok={s['ok']} failed={s['failed']}")
    total_ok = sum(s["ok"] for s in counts.values())
    print(f"registry seeded: {total_ok} of {len(plan)} requests succeeded"
          + (f", {failures} failed" if failures else ""))
    return 1 if failures else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--roster", type=Path, default=DEFAULT_ROSTER)
    parser.add_argument("--local", action="store_true",
                        help="metrics_target is 127.0.0.1:<dev_metrics_port> instead of <dns>:9300")
    parser.add_argument("--dry-run", action="store_true", help="print the requests, send nothing")
    args = parser.parse_args(argv)

    base_url = os.environ.get("REGISTRY_URL", DEFAULT_REGISTRY_URL).rstrip("/")
    try:
        roster = load_roster(args.roster)
    except (OSError, ValueError, ValidationError) as exc:
        print(f"error: cannot load roster {args.roster}: {exc}", file=sys.stderr)
        return 2
    try:
        plan = build_plan(roster, args.local)
    except (ValueError, ValidationError) as exc:
        print(f"error: roster does not map to registry bodies: {exc}", file=sys.stderr)
        return 2

    needed = {"REGISTRY_ADMIN_TOKEN"} | {_token_env(c.city) for c in roster.cities}
    tokens = {name: os.environ.get(name, "") for name in sorted(needed)}
    missing = sorted(name for name, value in tokens.items() if not value)
    if missing:
        print(f"error: missing environment variables: {', '.join(missing)}", file=sys.stderr)
        return 2

    if args.dry_run:
        _print_dry_run(plan, base_url)
        return 0
    return _send(plan, base_url, tokens)


if __name__ == "__main__":
    sys.exit(main())
