#!/usr/bin/env bash
# Scalability measurement (rubric I). Benign: a synthetic OCPP connection-load generator
# (eval/ocpp_load.py) is added to the five plain-ws Aveiro stations; nothing in the stations'
# own roster, sim.env or evsim service is changed, and no TLS cert is needed.
#
#   eval/scale.sh up <K> [hb-interval]   K extra synthetic chargers per Aveiro station
#                                        (registers them with the Aveiro token, starts the generator)
#   eval/scale.sh measure <label> [sec]  capture a measurement window into evidence/eval/raw/<label>/
#   eval/scale.sh down                   stop generators, delete the synthetic registry nodes
#   eval/scale.sh health                 connected / quarantine / pods / free RAM
# Safety: `up` refuses to run when available host RAM would fall below MIN_AVAIL_MB (1500).
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
. "$ROOT/infra/lib.sh"
SECRETS="$ROOT/secrets/platform.env"
CITY=aveiro
STATIONS="st-001 st-002 st-003 st-004 st-005"
URL="ws://edge.$CITY.pt.lab/ocpp"
MIN_AVAIL_MB="${MIN_AVAIL_MB:-1500}"
OUT="$ROOT/evidence/eval/raw"
PY=/opt/evcharge/venv/bin/python
secret() { grep -m1 "^$1=" "$SECRETS" | cut -d= -f2-; }
avail_mb() { awk '/^MemAvailable:/ {print int($2/1024)}' /proc/meminfo; }
ix() { local i="$1"; shift; incus exec "$i" -- "$@"; }
PROM=""
prom_ip() { [ -n "$PROM" ] || PROM=$(kc -n monitoring get svc prometheus -o jsonpath='{.spec.clusterIP}'); }
q() { prom_ip; incus exec k3s-master -- curl -s "http://$PROM:9090/api/v1/query" --data-urlencode "query=$1"; echo; }

api_get() { printf 'Authorization: Bearer %s\n' "$(secret CSMS_API_TOKEN)" |
  ix k3s-master curl -sS -H @- "http://127.0.0.1:30800$1"; }

health() {
  echo "available_mb=$(avail_mb)"
  echo "connected_all=$(api_get '/connections?prefix=pt' | grep -o '"count": *[0-9]*' | head -1 | grep -o '[0-9]*$')"
  echo "connected_aveiro=$(api_get '/connections?prefix=pt.aveiro' | grep -o '"count": *[0-9]*' | head -1 | grep -o '[0-9]*$')"
  echo "quarantined=$(ix k3s-master curl -sS http://127.0.0.1:30801/quarantine | grep -o '"hier_id"' | wc -l)"
  kc get pods -A --no-headers | awk '$4!="Running" && $4!="Completed" {print "NOT RUNNING: "$0}'
  echo "pods_running=$(kc get pods -A --no-headers | grep -c ' Running ')"
}

registry_nodes() { # <put|delete> <K>
  local op="$1" k="$2" st
  secret REGISTRY_TOKEN_AVEIRO | ix aveiro-edge "$PY" -c '
import sys, httpx
op, k = sys.argv[1], int(sys.argv[2])
tok = sys.stdin.read().strip()
h = {"Authorization": "Bearer " + tok}
base = None
for n in ("k3s-master", "k3s-node1"):
    try:
        httpx.get(f"http://{n}.incus:30801/whoami", headers=h, timeout=5).raise_for_status(); base = f"http://{n}.incus:30801"; break
    except httpx.HTTPError: pass
assert base, "registry unreachable"
done = 0
for s in range(1, 6):
    for i in range(3, 3 + k):
        hid = f"pt.aveiro.st-{s:03d}.cp-{i}"
        if op == "put":
            r = httpx.put(f"{base}/nodes/{hid}", headers=h, timeout=10, json={
                "name": f"load {hid}", "max_kw": 22.0, "connectors": 1, "vendor": "EVLab",
                "model": "EVL-AC22", "serial": f"EVLOAD-{i:04d}"})
            assert r.status_code in (200, 201), (hid, r.status_code, r.text)
        else:
            r = httpx.delete(f"{base}/nodes/{hid}", headers=h, timeout=10)
            assert r.status_code in (204, 404), (hid, r.status_code, r.text)
        done += 1
print(op, done, "nodes")
' "$op" "$k"
}

stop_gen() {
  for st in $STATIONS; do
    ix "$CITY-$st" sh -c 'systemctl stop evload.service 2>/dev/null; systemctl reset-failed evload.service 2>/dev/null; true'
  done
}

cmd_up() {
  local k="$1" hb="${2:-5}" st
  [ "$(avail_mb)" -ge "$MIN_AVAIL_MB" ] || { echo "available RAM $(avail_mb) MiB < $MIN_AVAIL_MB, refusing" >&2; exit 3; }
  stop_gen
  registry_nodes put "$k"
  for st in $STATIONS; do
    ix "$CITY-$st" mkdir -p /opt/evload
    incus file push -q "$ROOT/eval/ocpp_load.py" "$CITY-$st/opt/evload/ocpp_load.py"
    ix "$CITY-$st" systemd-run -q --unit=evload -p User=evcharge -p Restart=on-failure \
      "$PY" /opt/evload/ocpp_load.py --station "pt.$CITY.$st" --first 3 --count "$k" \
      --url "$URL" --hb-interval "$hb"
  done
  echo "started $k synthetic chargers per station (hb ${hb}s); available_mb=$(avail_mb)"
}

cmd_down() {
  stop_gen
  for st in $STATIONS; do ix "$CITY-$st" rm -rf /opt/evload; done
  # delete a generous range: 404 is accepted
  registry_nodes delete 30
  health
}

cmd_measure() {
  local label="$1" win="${2:-120}" d="$OUT/$1" st w0 w1 t0 t1
  mkdir -p "$d"
  exec 3>&1; exec >"$d/summary.txt"
  echo "label=$label window_s=$win start=$(date -u +%FT%TZ)"
  health
  w0=$(ix wazuh-container grep events_processed /var/ossec/var/run/wazuh-analysisd.state | grep -o "[0-9]*")
  t0=$(date +%s)
  for st in $STATIONS; do
    echo "cpu0 $st $(ix $CITY-$st sh -c 'grep usage_usec /sys/fs/cgroup/cpu.stat')"
  done > "$d/stations_cpu0.txt"
  sleep "$win"
  w1=$(ix wazuh-container grep events_processed /var/ossec/var/run/wazuh-analysisd.state | grep -o "[0-9]*")
  t1=$(date +%s)
  echo "wazuh_events_processed: $w0 -> $w1 in $((t1 - t0)) s => EPS=$(awk "BEGIN{printf \"%.2f\", ($w1-$w0)/($t1-$t0)}")"
  ix wazuh-container sh -c 'grep -E "events_received|events_dropped|events_processed|alerts_written" /var/ossec/var/run/wazuh-analysisd.state; ps -o rss=,pcpu=,comm= -C wazuh-analysisd -C wazuh-remoted -C wazuh-db' > "$d/wazuh.txt"
  local r="[${win}s]"
  {
    echo "## csms request latency (evcharge_csms_ocpp_handle_seconds, all actions), window $win s"
    for p in 0.5 0.95 0.99; do echo "p$p"; q "histogram_quantile($p,sum by (le)(rate(evcharge_csms_ocpp_handle_seconds_bucket$r)))"; done
    echo "p95 by action"; q "histogram_quantile(0.95,sum by (action,le)(rate(evcharge_csms_ocpp_handle_seconds_bucket$r)))"
    echo "## csms message rate (msg/s)"; q "sum(rate(evcharge_csms_ocpp_handle_seconds_count$r))"
    echo "## csms connected chargers"; q "sum(evcharge_csms_connected_chargers)"
    echo "## csms dependency p95 (registry/db/billing)"; q "histogram_quantile(0.95,sum by (dependency,le)(rate(evcharge_csms_dependency_seconds_bucket$r)))"
    echo "## csms edge rejections (rate)"; q "sum(rate(evcharge_csms_identity_rejections_total$r))"
    echo "## prometheus head series"; q "prometheus_tsdb_head_series"
    echo "## active series by job (count of all series per job)"; q "count by (job)({__name__=~'.+'})"
    echo "## series per city (federated, label city)"; q "count by (city)({city!=''})"
    echo "## scrape duration max/avg by job"; q "max by (job)(scrape_duration_seconds)"
    echo "## scrape samples scraped by job"; q "sum by (job)(scrape_samples_scraped)"
    echo "## prometheus rss / cpu"; q "process_resident_memory_bytes{job='prometheus'}"; q "rate(process_cpu_seconds_total{job='prometheus'}$r)"
    echo "## csms process rss/cpu per pod"; q "process_resident_memory_bytes{job=~'csms.*'}"; q "rate(process_cpu_seconds_total{job=~'csms.*'}$r)"
    echo "## csms replicas (HPA)"
  } > "$d/prom.txt" 2>&1
  { kc get hpa -n evcharge csms --no-headers; } >> "$d/prom.txt" 2>&1 || true
  { kc top pods -A --no-headers | sort -k3 -rn | head -14; echo; kc top nodes --no-headers; echo; free -m; echo; incus list -c n,m -f csv | grep -E "wazuh|k3s|docker|aveiro" ; } > "$d/resources.txt" 2>&1 || true
  for st in $STATIONS; do
    echo "cpu1 $st $(ix $CITY-$st sh -c 'grep usage_usec /sys/fs/cgroup/cpu.stat')"
    echo "mem $st $(ix $CITY-$st sh -c 'grep -E "^anon " /sys/fs/cgroup/memory.stat; cat /sys/fs/cgroup/memory.current')"
    ix $CITY-$st sh -c 'tail -n1 /dev/null; journalctl -u evload -n1 --no-pager -o cat 2>/dev/null' || true
  done > "$d/stations_cpu1.txt" 2>&1
  ix $CITY-edge sh -c 'tail -n 3000 /var/log/nginx/access.log 2>/dev/null | awk "{print \$9}" | sort | uniq -c' > "$d/edge_status_codes.txt" 2>&1 || true
  echo "end=$(date -u +%FT%TZ)"
  exec >&3
  cat "$d/summary.txt"; grep -E "^(p0|## csms request|## csms message)" -A1 "$d/prom.txt" | head -30
}

case "${1:-}" in
  up) cmd_up "${2:?K}" "${3:-5}" ;;
  down) cmd_down ;;
  measure) cmd_measure "${2:?label}" "${3:-120}" ;;
  health) health ;;
  *) sed -n '2,12p' "${BASH_SOURCE[0]}"; exit 2 ;;
esac
