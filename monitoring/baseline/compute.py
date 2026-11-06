#!/usr/bin/env python3
"""Turn a recorded window of the central Prometheus into baseline.yml (contract 14.7).

Per city, three series are sampled every 60 s with query_range:
  load  city:grid_power_watts:sum
  msgs  city:ocpp_messages_sent:rate1m
  auth  sum(increase(evcharge_csms_card_auth_total{result=~"Blocked|Invalid|Expired"}[10m]))
and upper = mean + 3 sigma (population standard deviation). The load bound is capped at
0.9 x installed capacity (sum of max_kw in the roster). The complete baseline.yml, header
included, is printed on stdout.

    compute.py --prometheus http://prometheus.monitoring:9090 --window 24h --roster roster.yaml
"""
import argparse
import math
import re
import sys
from datetime import datetime, timedelta, timezone

import yaml

STEP_S = 60
CAP_FRACTION = 0.9
SIGMAS = 3.0
AUTH_RESULTS = "Blocked|Invalid|Expired"
SERIES = {
    "load": 'city:grid_power_watts:sum{{city="{city}"}}',
    "msgs": 'city:ocpp_messages_sent:rate1m{{city="{city}"}}',
    "auth": 'sum(increase(evcharge_csms_card_auth_total{{city="{city}",result=~"' + AUTH_RESULTS + '"}}[10m]))',
}
_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def parse_window(text: str) -> timedelta:
    m = re.fullmatch(r"(\d+)([smhd])", text.strip())
    if not m:
        raise ValueError(f"bad window {text!r} (use 24h, 6h, 90m, ...)")
    return timedelta(seconds=int(m.group(1)) * _UNITS[m.group(2)])


def parse_end(text: str | None) -> datetime:
    if not text:
        return datetime.now(timezone.utc).replace(microsecond=0)
    d = datetime.fromisoformat(text.replace("Z", "+00:00"))
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def capacities(roster: dict) -> dict[str, float]:
    """Installed capacity per city in watts."""
    out = {}
    for c in roster["cities"]:
        kw = sum(ch["max_kw"] for s in c.get("stations", []) for ch in s.get("chargers", []))
        out[c["city"]] = kw * 1000.0
    return out


def extract_values(payload: dict) -> list[float]:
    """Samples of a query_range response with one series (several are concatenated)."""
    if payload.get("status") != "success":
        raise RuntimeError(f"Prometheus error: {payload.get('error', payload)}")
    vals: list[float] = []
    for series in payload["data"]["result"]:
        for _ts, v in series["values"]:
            f = float(v)
            if math.isfinite(f):
                vals.append(f)
    return vals


def stats(values: list[float]) -> dict:
    n = len(values)
    if n == 0:
        return {"n": 0, "mean": 0.0, "sigma": 0.0, "upper": 0.0}
    mean = sum(values) / n
    sigma = math.sqrt(sum((x - mean) ** 2 for x in values) / n)
    return {"n": n, "mean": mean, "sigma": sigma, "upper": mean + SIGMAS * sigma}


def analyse(samples: dict[str, dict[str, list[float]]], caps: dict[str, float]) -> dict:
    """samples[city][kind] -> values; returns stats per city and kind (+ cap flag)."""
    out: dict = {}
    for city, kinds in samples.items():
        out[city] = {k: stats(v) for k, v in kinds.items()}
        load = out[city]["load"]
        cap = CAP_FRACTION * caps[city]
        load["cap"] = cap
        load["capped"] = load["upper"] > cap
        if load["capped"]:
            load["upper_raw"] = load["upper"]
            load["upper"] = cap
    return out


def render(result: dict, start: datetime, end: datetime, window: str, caps: dict[str, float]) -> str:
    basis = f"{window}-{end.strftime('%Y%m%d')}"
    iso = lambda d: d.strftime("%Y-%m-%dT%H:%M:%SZ")
    lines = [
        "# Baseline thresholds computed by monitoring/baseline/compute.py (contract 14.7).",
        f"# window: {iso(start)} .. {iso(end)} ({window}), step {STEP_S}s, upper = mean + {SIGMAS:g} sigma",
        f"# basis=\"{basis}\"; load upper capped at {CAP_FRACTION:g} x installed capacity",
        "# city series        n        mean       sigma       upper  note",
    ]
    for city in sorted(result):
        r = result[city]
        for kind, label, fmt in (("load", "grid_w", "%.1f"), ("msgs", "msgs/s", "%.4f"), ("auth", "auth10m", "%.2f")):
            s = r[kind]
            note = ""
            if kind == "load":
                note = (f"capped from {s['upper_raw']:.1f} to {s['cap']:.1f}" if s["capped"]
                        else f"cap {s['cap']:.1f} not applied")
            lines.append("# %-6s %-9s %5d %11s %11s %11s  %s" % (
                city, label, s["n"], fmt % s["mean"], fmt % s["sigma"], fmt % s["upper"], note))
    lines += ["groups:", "  - name: baseline", "    rules:"]

    def rule(name: str, value: float, city: str) -> str:
        return (f'      - {{record: "{name}", expr: "vector({value:.6g})", '
                f'labels: {{city: {city}, basis: "{basis}"}}}}')

    for city in sorted(result):
        r = result[city]
        lines += [
            rule("baseline:city_grid_power_watts:mean", r["load"]["mean"], city),
            rule("baseline:city_grid_power_watts:upper", r["load"]["upper"], city),
            rule("baseline:city_msgs_rate:mean", r["msgs"]["mean"], city),
            rule("baseline:city_msgs_rate:upper", r["msgs"]["upper"], city),
            rule("baseline:city_auth_failures_10m:upper", r["auth"]["upper"], city),
        ]
    return "\n".join(lines) + "\n"


def fetch(client, base: str, query: str, start: datetime, end: datetime) -> list[float]:
    r = client.get(base.rstrip("/") + "/api/v1/query_range", params={
        "query": query, "start": start.timestamp(), "end": end.timestamp(), "step": STEP_S})
    r.raise_for_status()
    return extract_values(r.json())


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "baseline").splitlines()[0])
    ap.add_argument("--prometheus", required=True, help="base URL of the central Prometheus")
    ap.add_argument("--window", default="24h", help="24h, 6h, 90m, ... (default 24h)")
    ap.add_argument("--end", help="ISO timestamp of the window end (default now, UTC)")
    ap.add_argument("--roster", required=True, help="roster.yaml (cities and max_kw)")
    ap.add_argument("--min-samples", type=int, default=30, help="refuse windows with fewer samples")
    args = ap.parse_args(argv)

    import httpx

    with open(args.roster) as f:
        caps = capacities(yaml.safe_load(f))
    end = parse_end(args.end)
    start = end - parse_window(args.window)
    samples: dict[str, dict[str, list[float]]] = {}
    with httpx.Client(timeout=60) as client:
        for city in sorted(caps):
            samples[city] = {k: fetch(client, args.prometheus, q.format(city=city), start, end)
                             for k, q in SERIES.items()}
            for k, v in samples[city].items():
                if len(v) < args.min_samples:
                    print(f"error: {city}/{k} has only {len(v)} samples in the window", file=sys.stderr)
                    return 2
    sys.stdout.write(render(analyse(samples, caps), start, end, args.window, caps))
    return 0


if __name__ == "__main__":
    sys.exit(main())
