#!/usr/bin/env bash
# Bandwidth of per-site monitoring (contract 14.6), measured from a throwaway pod:
#  - wire sizes: one /federate request per city and one request per direct target
#    (station exporters plus the edge exporters), each with and without gzip
#  - the 14.6 PromQL results for the federated and the direct jobs over a window
# usage: eval/bandwidth.sh [--window 30m] [--end <unix seconds>]
# The PromQL part needs data: A from the federate-* jobs, B only while the direct-* jobs ran
# (infra/06_monitoring.sh --direct-scrape on). Nothing is installed on the host.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
. "$ROOT/infra/lib.sh"

WINDOW=30m
END=""
while [ $# -gt 0 ]; do
  case "$1" in
    --window) WINDOW="$2"; shift 2 ;;
    --end) END="$2"; shift 2 ;;
    *) echo "usage: $0 [--window 30m] [--end <unix seconds>]" >&2; exit 2 ;;
  esac
done

IMAGE=docker.incus:5000/evcharge-sim:latest
CITIES="${!CITY_NET[@]}"

{
  printf 'WINDOW="%s"\nEND="%s"\nCITIES="%s"\n' "$WINDOW" "$END" "$CITIES"
  cat <<'PY'
import gzip, json, sys, time, urllib.parse, urllib.request

PROM = "http://prometheus.monitoring:9090"
REG = "http://registry.evcharge.svc:8001"
FED_INTERVAL, DIRECT_INTERVAL = 30, 5          # seconds, as in prometheus.yml.tmpl


def get(url):
    """Return (uncompressed bytes, bytes on the wire with gzip) of one GET."""
    req = urllib.request.Request(url, headers={"Accept-Encoding": "gzip"})
    with urllib.request.urlopen(req, timeout=10) as r:
        wire = r.read()
        raw = gzip.decompress(wire) if r.headers.get("Content-Encoding") == "gzip" else wire
    return len(raw), len(wire)


def prom(q):
    qs = {"query": q}
    if END:
        qs["time"] = END
    with urllib.request.urlopen(PROM + "/api/v1/query?" + urllib.parse.urlencode(qs), timeout=20) as r:
        return {x["metric"].get("city", ""): float(x["value"][1]) for x in json.load(r)["data"]["result"]}


print("== wire sizes, one scrape cycle (bytes: uncompressed / gzip) ==")
rows = []
for city in sorted(CITIES.split()):
    fed = get(f"http://edge.{city}.pt.lab:9090/federate?" +
              urllib.parse.urlencode({"match[]": '{__name__=~"city:.*"}'}))
    targets = [f"edge.{city}.pt.lab:9113", f"edge.{city}.pt.lab:9100"]
    with urllib.request.urlopen(f"{REG}/sd/prometheus?city={city}&kind=station", timeout=10) as r:
        targets += [t for g in json.load(r) for t in g["targets"]]
    dsum = [0, 0]
    for t in targets:
        raw, wire = get(f"http://{t}/metrics")
        dsum[0] += raw
        dsum[1] += wire
    rows.append((city, fed, len(targets), dsum))
    print(f"{city:8s} federate {fed[0]:7d} / {fed[1]:6d}   direct {len(targets)} targets {dsum[0]:8d} / {dsum[1]:7d}")

print("\n== bytes per minute from the sizes above (federate every %d s, direct every %d s) ==" %
      (FED_INTERVAL, DIRECT_INTERVAL))
print(f"{'city':8s} {'A raw':>9s} {'A gzip':>9s} {'B raw':>11s} {'B gzip':>10s} {'saving raw':>11s} {'saving gzip':>12s}")
tot = [0, 0, 0, 0]
for city, fed, n, d in rows:
    a = [fed[0] * 60 / FED_INTERVAL, fed[1] * 60 / FED_INTERVAL]
    b = [d[0] * 60 / DIRECT_INTERVAL, d[1] * 60 / DIRECT_INTERVAL]
    for i, v in enumerate(a + b):
        tot[i] += v
    print(f"{city:8s} {a[0]:9.0f} {a[1]:9.0f} {b[0]:11.0f} {b[1]:10.0f} {100*(1-a[0]/b[0]):10.1f}% {100*(1-a[1]/b[1]):11.1f}%")
print(f"{'total':8s} {tot[0]:9.0f} {tot[1]:9.0f} {tot[2]:11.0f} {tot[3]:10.0f} "
      f"{100*(1-tot[0]/tot[2]):10.1f}% {100*(1-tot[1]/tot[3]):11.1f}%")

print(f"\n== 14.6 PromQL over {WINDOW}{' ending at ' + END if END else ''} (uncompressed body, bytes/min) ==")
qa = f'sum by (city) (avg_over_time(scrape_body_size_bytes{{job=~"federate-.+"}}[{WINDOW}])) * 60 / {FED_INTERVAL}'
qb = f'sum by (city) (avg_over_time(scrape_body_size_bytes{{job=~"direct-.+"}}[{WINDOW}])) * 60 / {DIRECT_INTERVAL}'
sa = f'sum by (city) (avg_over_time(scrape_samples_scraped{{job=~"federate-.+"}}[{WINDOW}])) * 60 / {FED_INTERVAL}'
sb = f'sum by (city) (avg_over_time(scrape_samples_scraped{{job=~"direct-.+"}}[{WINDOW}])) * 60 / {DIRECT_INTERVAL}'
A, B, SA, SB = prom(qa), prom(qb), prom(sa), prom(sb)
print(f"{'city':8s} {'A B/min':>10s} {'B B/min':>11s} {'saving':>8s} {'A samples/min':>14s} {'B samples/min':>14s}")
for c in sorted(set(A) | set(B)):
    a, b = A.get(c), B.get(c)
    sv = f"{100*(1-a/b):6.1f}%" if a and b else "    n/a"
    print(f"{c:8s} {a or 0:10.0f} {b or 0:11.0f} {sv:>8s} {SA.get(c, 0):14.0f} {SB.get(c, 0):14.0f}")
ta, tb = sum(A.values()), sum(B.values())
print(f"{'total':8s} {ta:10.0f} {tb:11.0f} {(100*(1-ta/tb)) if tb else 0:6.1f}% {sum(SA.values()):14.0f} {sum(SB.values()):14.0f}")
PY
} | kc -n monitoring run "bandwidth-$$" --rm -i --restart=Never --quiet --image "$IMAGE" \
    --image-pull-policy=IfNotPresent -- python -
