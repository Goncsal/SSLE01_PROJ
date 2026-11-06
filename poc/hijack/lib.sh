#!/usr/bin/env bash
# Shared helpers for the hijacking PoC scripts (Phase 6, signal/identity hijacking).
# Design background: the Phase 6 hijacking design note. Interfaces: docs/interfaces.md v6
# 5.2, 9.2, 11, 12, 16.
#
# Every script here defaults to --dry-run: it prints the commands and target names it
# would use and changes nothing. A live run needs --run *and* the environment variable
# EVCHARGE_BASELINE_DONE=1 (set by the matching RUN card, never by this one): see
# require_baseline() below. Only `incus` is needed on the host.
set -euo pipefail

HIJACK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HIJACK_DIR/../.." && pwd)"
# shellcheck source=../../infra/lib.sh
. "$ROOT/infra/lib.sh"

CITY_IMG="evcharge-city"
ROSTER="${ROSTER:-$ROOT/dev/roster.yaml}"

# --- shared flag state; each script's own getopts loop sets these ---
DRY_RUN=1
RUN_REQUESTED=0
DURATION="${DURATION:-120}"
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

# Refuses a live run unless the RUN card's flag is set. Call once --run has been parsed.
require_baseline() {
  if [ "${EVCHARGE_BASELINE_DONE:-}" != "1" ]; then
    echo "refusing --run: EVCHARGE_BASELINE_DONE is not set to 1." >&2
    echo "A live baseline recording must not be disturbed; the W6-run card sets this" \
         "once the baseline is captured. Use --dry-run to check the script without it." >&2
    exit 1
  fi
}

# --- naming helpers (names only, never addresses: section 13.1 / 11.1) ---
edge_host() { echo "edge.$1.pt.lab"; }

is_plain_city() {
  local c="$1" p
  for p in ${PLAIN_WS_CITIES//,/ }; do
    if [ "$p" = "$c" ]; then return 0; fi
  done
  return 1
}

edge_url() {
  local c="$1"
  if is_plain_city "$c"; then echo "ws://$(edge_host "$c")/ocpp"; else echo "wss://$(edge_host "$c")/ocpp"; fi
}

# pt.porto.st-001 -> porto-st-001 (the Incus instance name, section 13.2)
station_instance() {
  local hier="$1"
  [[ "$hier" =~ ^pt\.([a-z][a-z0-9]*)\.(st-[0-9]{3})$ ]] || {
    echo "not a station hier-id: $hier" >&2; return 1
  }
  echo "${BASH_REMATCH[1]}-${BASH_REMATCH[2]}"
}

rogue_name() { echo "${1}-rogue"; }

usage_common() {
  cat <<'EOF'

Common flags:
  --city NAME        city to target (default: porto)
  --station HIER_ID  station hier-id, e.g. pt.porto.st-001 (default: pt.<city>.st-001)
  --duration SECONDS hard time-box for the live scenario (default: 120)
  --dry-run          print what would happen, change nothing (default)
  --run              actually perform the scenario; refused unless EVCHARGE_BASELINE_DONE=1
  -h, --help         this help
EOF
}

# Parses the flags common to the scenario scripts. Unrecognised flags are left in
# REMAINING_ARGS for the caller to parse its own extras (currently none needed).
parse_common_args() {
  STATION=""
  while [ $# -gt 0 ]; do
    case "$1" in
      --city) CITY="$2"; shift 2 ;;
      --station) STATION="$2"; shift 2 ;;
      --duration) DURATION="$2"; shift 2 ;;
      --run) RUN_REQUESTED=1; DRY_RUN=0; shift ;;
      --dry-run) RUN_REQUESTED=0; DRY_RUN=1; shift ;;
      -h|--help) print_usage; exit 0 ;;
      *) echo "unknown argument: $1" >&2; print_usage; exit 2 ;;
    esac
  done
  [ -n "$STATION" ] || STATION="pt.$CITY.st-001"
  if [ "$RUN_REQUESTED" = 1 ]; then require_baseline; fi
  return 0
}
