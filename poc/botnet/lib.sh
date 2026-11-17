#!/usr/bin/env bash
# Shared helpers for the botnet PoC scripts (Phase 8, threat 3: botnet). Design
# background: the Phase 8 botnet design note. Interfaces: docs/interfaces.md v6 9.4
# (compromised mode, C2 protocol), 13.3 (sim.env), 15.1 (netwatch), 16 (ACLs), 7.2/7.3
# (sim and csms metrics).
#
# Every script here defaults to --dry-run: it prints the commands and target names it
# would use and changes nothing. A live run needs --run *and* the environment variable
# EVCHARGE_BASELINE_DONE=1 (set in the environment before the live run, never by this script): see
# require_baseline() below. Only `incus` is needed on the host.
set -euo pipefail

BOTNET_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$BOTNET_DIR/../.." && pwd)"
# shellcheck source=../../infra/lib.sh
. "$ROOT/infra/lib.sh"

CITY_IMG="evcharge-city"
ROSTER="${ROSTER:-$ROOT/dev/roster.yaml}"
SIM_ENV_PATH="/etc/evcharge/sim.env"
# A pre-compromise copy kept inside each station instance, so a restore never depends on
# host-side state and survives a host reboot (the earlier /tmp state file did not).
SIM_ENV_BACKUP="${SIM_ENV_BACKUP:-/etc/evcharge/sim.env.pre-botnet}"
C2_NAME="c2"

# --- shared flag state; each script's own getopts loop sets these ---
DRY_RUN=1
RUN_REQUESTED=0
DURATION="${DURATION:-300}"
CITY="${CITY:-porto}"

# --- cleanup: scripts push one undo command per resource they create; run in reverse
# order on exit, success or failure, and on INT/TERM ---
CLEANUP_CMDS=()
add_cleanup() { CLEANUP_CMDS+=("$1"); }
run_cleanup() {
  local i cmd
  for ((i = ${#CLEANUP_CMDS[@]} - 1; i >= 0; i--)); do
    cmd="${CLEANUP_CMDS[$i]}"
    log "cleanup: $cmd"
    eval "$cmd" || true
  done
  CLEANUP_CMDS=()
}
trap run_cleanup EXIT
trap 'run_cleanup; exit 130' INT TERM

# print a planned action without doing it
would() { printf '  would: %s\n' "$*"; }

# run_or_plan <argv...>: dry-run prints "would: argv"; a live run executes it as given
# (no shell re-parsing, so quoting the caller used is preserved exactly).
run_or_plan() {
  if [ "$DRY_RUN" = 1 ]; then
    would "$*"
  else
    log "$*"
    "$@"
  fi
}

# Refuses a live run unless the baseline-done flag is set. Call once --run has been parsed.
require_baseline() {
  if [ "${EVCHARGE_BASELINE_DONE:-}" != "1" ]; then
    echo "refusing --run: EVCHARGE_BASELINE_DONE is not set to 1." >&2
    echo "A live baseline recording must not be disturbed; set this in the environment" \
         "once the baseline is captured. Use --dry-run to check the script without it." >&2
    exit 1
  fi
}

# --- naming helpers (names only, never addresses: section 13.1 / 11.1 / 16) ---
# pt.porto.st-001 -> porto-st-001 (the Incus instance name, section 13.2)
station_instance() {
  local hier="$1"
  [[ "$hier" =~ ^pt\.([a-z][a-z0-9]*)\.(st-[0-9]{3})$ ]] || {
    echo "not a station hier-id: $hier" >&2; return 1
  }
  echo "${BASH_REMATCH[1]}-${BASH_REMATCH[2]}"
}

# The C2 belongs to this PoC (9.4): a lab-only DNS alias on the central bridge, the one
# host a compromised station is allowed to reach for the demo (16, stated concession).
c2_url() { echo "http://${C2_NAME}.incus:$1"; }

# pick_stations <N> <cities_csv> -> N station hier-ids, round-robin across the cities
# (pt.<city>.st-001, pt.<city>.st-002, ...), capped at STATIONS_PER_CITY per city.
pick_stations() {
  local n="$1" cities_csv="$2"
  local -a cities
  IFS=',' read -r -a cities <<< "$cities_csv"
  local total=$(( ${#cities[@]} * STATIONS_PER_CITY ))
  if [ "$n" -gt "$total" ]; then
    echo "pick_stations: --stations $n exceeds $total available across ${cities[*]}; capping" >&2
    n="$total"
  fi
  local i=0 idx=1 c
  while [ "$i" -lt "$n" ]; do
    for c in "${cities[@]}"; do
      [ "$i" -ge "$n" ] && break
      printf 'pt.%s.st-%03d\n' "$c" "$idx"
      i=$((i + 1))
    done
    idx=$((idx + 1))
  done
}

# --- sim.env (13.3): read/write/restart. Names only, no addresses written to disk here. ---
read_sim_env() { incus exec "$1" -- cat "$SIM_ENV_PATH"; }
write_sim_env() { printf '%s\n' "$2" | incus exec "$1" -- sh -c "cat > $SIM_ENV_PATH"; }
restart_evsim() { incus exec "$1" -- systemctl restart evsim; }

# set_extra_args <sim.env content> <extra args> -> content with SIM_EXTRA_ARGS replaced
# (or appended if absent); every other line (SIM_CSMS_URL, ...) is left untouched.
set_extra_args() {
  local content="$1" extra="$2"
  if printf '%s\n' "$content" | grep -q '^SIM_EXTRA_ARGS='; then
    printf '%s\n' "$content" | sed -E "s|^SIM_EXTRA_ARGS=.*|SIM_EXTRA_ARGS=\"$extra\"|"
  else
    printf '%s\nSIM_EXTRA_ARGS="%s"\n' "$content" "$extra"
  fi
}

# --- durable restore: the backup lives with the thing it backs up, inside the instance ---
# save a pre-compromise copy once (never overwrite an existing backup)
backup_sim_env() {
  local inst="$1" content="$2"
  incus exec "$inst" -- sh -c "test -f $SIM_ENV_BACKUP" 2>/dev/null && return 0
  printf '%s\n' "$content" | incus exec "$inst" -- sh -c "cat > $SIM_ENV_BACKUP"
}
# is the station still carrying the compromise flag?
station_is_compromised() {
  incus exec "$1" -- grep -q -- '--compromised' "$SIM_ENV_PATH" 2>/dev/null
}
# restore one station: prefer its in-instance backup, else strip SIM_EXTRA_ARGS to the
# honest baseline. Removes the backup and restarts evsim.
restore_sim_env() {
  local inst="$1" content
  if incus exec "$inst" -- test -f "$SIM_ENV_BACKUP" 2>/dev/null; then
    content="$(incus exec "$inst" -- cat "$SIM_ENV_BACKUP")"
    write_sim_env "$inst" "$content"
    incus exec "$inst" -- rm -f "$SIM_ENV_BACKUP"
  else
    content="$(read_sim_env "$inst")"
    write_sim_env "$inst" "$(set_extra_args "$content" "")"
  fi
  restart_evsim "$inst"
}
# every station hier-id in the given cities (all cities when none given)
all_stations_in() {
  local cities_csv="${1:-porto,lisboa,aveiro}"
  local -a cities; IFS=',' read -r -a cities <<< "$cities_csv"
  local c i
  for c in "${cities[@]}"; do
    for i in $(seq 1 "$STATIONS_PER_CITY"); do printf 'pt.%s.st-%03d\n' "$c" "$i"; done
  done
}

# --- Prometheus (7.2/7.3/14.1): read-only queries, run from inside the lab instance that
# already has network access, so the host needs only `incus exec` (course-way rule). ---
urlencode() {
  local s="$1" out="" c i hex
  for ((i = 0; i < ${#s}; i++)); do
    c="${s:i:1}"
    case "$c" in
      [a-zA-Z0-9.~_-]) out+="$c" ;;
      *) printf -v hex '%%%02X' "'$c"; out+="$hex" ;;
    esac
  done
  printf '%s' "$out"
}

# query_prom_value <instance> <base_url> <promql> -> bare number (0 if no series)
query_prom_value() {
  local inst="$1" base="$2" q="$3" url
  url="$base/api/v1/query?query=$(urlencode "$q")"
  incus exec "$inst" -- python3 - "$url" <<'PY'
import json
import sys
import urllib.request

with urllib.request.urlopen(sys.argv[1], timeout=10) as r:
    data = json.load(r)
result = data.get("data", {}).get("result", [])
print(sum(float(x["value"][1]) for x in result) if result else 0.0)
PY
}

# city Prometheus runs on the city's own edge (14.1); central Prometheus is reached from
# any K3s node on its NodePort (2.2) -- both read-only, no new traffic for a bot to hide in.
city_prom_query() { query_prom_value "$1-edge" "http://127.0.0.1:9090" "$2"; }
central_prom_query() { query_prom_value "k3s-master" "http://127.0.0.1:30090" "$1"; }

usage_common() {
  cat <<'EOF'

Common flags:
  --duration SECONDS hard time-box for the live scenario (default: 300)
  --dry-run          print what would happen, change nothing (default)
  --run              actually perform the scenario; refused unless EVCHARGE_BASELINE_DONE=1
  -h, --help         this help
EOF
}

# Parses the flags common to the scenario scripts. Unrecognised flags are left to the
# caller, which collects them into ARGS before calling this.
parse_common_args() {
  while [ $# -gt 0 ]; do
    case "$1" in
      --duration) DURATION="$2"; shift 2 ;;
      --run) RUN_REQUESTED=1; DRY_RUN=0; shift ;;
      --dry-run) RUN_REQUESTED=0; DRY_RUN=1; shift ;;
      --city) CITY="$2"; shift 2 ;;
      -h|--help) print_usage; exit 0 ;;
      *) echo "unknown argument: $1" >&2; print_usage; exit 2 ;;
    esac
  done
  if [ "$RUN_REQUESTED" = 1 ]; then require_baseline; fi
  return 0
}
