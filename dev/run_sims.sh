#!/usr/bin/env bash
# Start one evsim process per roster station against a remote CSMS (e.g. on K3s).
#
#   dev/run_sims.sh --csms-url URL[,URL...] [--roster PATH] [--sim-args "..."]
#                   [--metrics-host HOST] [--fresh]
#   dev/run_sims.sh --stop
#
#   --csms-url       OCPP base URL(s), e.g. ws://HOST:30900/ocpp; stations are assigned
#                    round-robin over the list
#   --roster PATH    roster file (default dev/roster.yaml)
#   --sim-args "..." extra flags for every evsim process
#   --metrics-host   bind address of the station metrics (default 0.0.0.0, so Prometheus
#                    can scrape them from another host)
#   --fresh          delete logs/sim-* first
#   --stop           stop the simulators started here (TERM, KILL after 10 s)
#
# Metrics ports start at 19301 (dev/run_local.sh uses 9301+). Pids: logs/run_sims.pids.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="$ROOT/.venv"
LOG_DIR="$ROOT/logs"
PIDFILE="$LOG_DIR/run_sims.pids"
BASE_PORT=19301
ROSTER_ARG="dev/roster.yaml"
SIM_ARGS_STR=""
CSMS_URLS=""
METRICS_HOST="0.0.0.0"
FRESH=0
STOP=0

die() { echo "error: $*" >&2; exit 1; }
usage() { sed -n '2,16p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --csms-url) [[ $# -ge 2 ]] || die "--csms-url needs a value"; CSMS_URLS="$2"; shift 2 ;;
    --roster) [[ $# -ge 2 ]] || die "--roster needs a path"; ROSTER_ARG="$2"; shift 2 ;;
    --sim-args) [[ $# -ge 2 ]] || die "--sim-args needs a string"; SIM_ARGS_STR="$2"; shift 2 ;;
    --metrics-host) [[ $# -ge 2 ]] || die "--metrics-host needs a value"; METRICS_HOST="$2"; shift 2 ;;
    --fresh) FRESH=1; shift ;;
    --stop) STOP=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) die "unknown option: $1 (see --help)" ;;
  esac
done

any_alive() {
  local pid name
  while read -r pid name; do
    if [[ -n "${pid:-}" ]] && kill -0 "$pid" 2>/dev/null; then return 0; fi
  done < "$PIDFILE"
  return 1
}

if (( STOP )); then
  if [[ ! -f "$PIDFILE" ]]; then echo "nothing to stop (no $PIDFILE)"; exit 0; fi
  while read -r pid name; do
    [[ -n "${pid:-}" ]] || continue
    if kill -0 "$pid" 2>/dev/null; then kill -TERM "$pid" 2>/dev/null || true; echo "TERM $name (pid $pid)"; fi
  done < "$PIDFILE"
  deadline=$((SECONDS + 10))
  while any_alive && (( SECONDS < deadline )); do sleep 0.2; done
  while read -r pid name; do
    [[ -n "${pid:-}" ]] || continue
    if kill -0 "$pid" 2>/dev/null; then
      kill -KILL "$pid" 2>/dev/null || true
      echo "KILL $name (pid $pid): did not stop within 10 s"
    fi
  done < "$PIDFILE"
  rm -f "$PIDFILE"
  echo "simulators stopped"
  exit 0
fi

[[ -n "$CSMS_URLS" ]] || die "--csms-url is required (see --help)"
case "$ROSTER_ARG" in /*) ROSTER="$ROSTER_ARG" ;; *) ROSTER="$PWD/$ROSTER_ARG" ;; esac
[[ -f "$ROSTER" ]] || die "roster not found: $ROSTER"
[[ -x "$VENV/bin/python" ]] || die "$VENV not found (run dev/run_local.sh once, or create the venv and install common and sim)"
cd "$ROOT"

if [[ -s "$PIDFILE" ]]; then
  while read -r pid name; do
    if [[ -n "${pid:-}" ]] && kill -0 "$pid" 2>/dev/null; then
      die "simulators are already running ($name, pid $pid); stop them with dev/run_sims.sh --stop"
    fi
  done < "$PIDFILE"
fi

IFS=',' read -r -a URLS <<<"$CSMS_URLS"
STATIONS="$("$VENV/bin/python" - "$ROSTER" <<'PY'
import sys
from evcommon.roster import load_roster

for sid, _, _ in load_roster(sys.argv[1]).iter_stations():
    print(sid)
PY
)"

mkdir -p "$LOG_DIR"
if (( FRESH )); then rm -f "$LOG_DIR"/sim-*; fi
: > "$PIDFILE"
SIM_EXTRA=()
read -r -a SIM_EXTRA <<<"$SIM_ARGS_STR"

i=0
while read -r sid; do
  [[ -n "$sid" ]] || continue
  url="${URLS[$((i % ${#URLS[@]}))]}"
  port=$((BASE_PORT + i))
  nohup env LOG_DIR="$LOG_DIR" LOG_STDOUT=false "$VENV/bin/python" -m evsim \
    --roster "$ROSTER" --station "$sid" --csms-url "$url" \
    --metrics-host "$METRICS_HOST" --metrics-port "$port" --log-dir "$LOG_DIR" \
    ${SIM_EXTRA[@]+"${SIM_EXTRA[@]}"} </dev/null >"$LOG_DIR/sim-$sid.out" 2>&1 &
  printf '%s %s\n' "$!" "sim-$sid" >> "$PIDFILE"
  i=$((i + 1))
done <<< "$STATIONS"
echo "started $i simulators over ${#URLS[@]} CSMS URL(s); metrics ports $BASE_PORT..$((BASE_PORT + i - 1))"

sleep 2
dead=""
while read -r pid name; do
  if ! kill -0 "$pid" 2>/dev/null; then dead="$dead $name"; fi
done < "$PIDFILE"
[[ -z "$dead" ]] || die "exited right after start:$dead (see $LOG_DIR/<name>.out); stop the rest with --stop"
echo "logs: $LOG_DIR/sim-<station>.{out,json}; stop with dev/run_sims.sh --stop"
