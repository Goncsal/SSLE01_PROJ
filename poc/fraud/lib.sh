#!/usr/bin/env bash
# Shared helpers for the fraud PoC scripts (Phase 7: cloned cards, meter tampering).
# Design background: DESIGN-fraud.md. Interfaces: docs/interfaces.md v6 4.4 (fraud rules),
# 6 (event vocabulary), 7.2 (feeder meter), 9.2/9.4 (sim flags), 13.3 (sim.env, how a
# station restarts evsim).
#
# Every script here defaults to --dry-run: it prints the sim.env it would write and the
# commands it would run, and changes nothing. A live run needs --run *and* the environment
# variable EVCHARGE_BASELINE_DONE=1 (set by the matching W7-run card, never by this one):
# see require_baseline() below. Only incus is needed on the host, and a dry run never
# calls it.
set -euo pipefail

FRAUD_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$FRAUD_DIR/../.." && pwd)"
# shellcheck source=../../infra/lib.sh
. "$ROOT/infra/lib.sh"

ROSTER="${ROSTER:-$ROOT/dev/roster.yaml}"
SIM_ENV_PATH=/etc/evcharge/sim.env

# --- shared flag state; each script's own arg loop sets these before calling
# parse_global_flags ---
DRY_RUN=1
RUN_REQUESTED=0
DURATION="${DURATION:-180}"

# --- cleanup: scripts push one undo command per sim.env they touch; run in reverse order
# on exit, success or failure, and on INT/TERM. This is how an edited sim.env always gets
# restored, even if the script is killed mid-run. ---
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

# Local copies of whatever sim.env held before this script touched it, so it can be put
# back exactly, even across process kill (restore_sim_env is registered as a cleanup
# right after save_sim_env, below).
SIM_ENV_SAVE_DIR="$(mktemp -d "${TMPDIR:-/tmp}/evcharge-fraud-poc.XXXXXX")"
add_cleanup "rm -rf '$SIM_ENV_SAVE_DIR'"

# print a planned action without doing it
would() { printf '  would: %s\n' "$*"; }

# run_or_plan <argv...>: dry-run prints "would: argv"; a live run executes it as given.
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
    echo "A live baseline recording must not be disturbed; the W7-run card sets this" \
         "once the baseline is captured. Use --dry-run to check the script without it." >&2
    exit 1
  fi
}

usage_common() {
  cat <<'EOF'

Common flags:
  --duration SECONDS hard time-box for the live scenario (default varies per script)
  --dry-run          print what would happen, change nothing (default)
  --run              actually perform the scenario; refused unless EVCHARGE_BASELINE_DONE=1
  -h, --help         this help
EOF
}

# Parses the flags common to every fraud PoC script (--duration/--run/--dry-run/-h).
# Callers strip their own extra flags into an array first and pass the remainder here.
parse_global_flags() {
  while [ $# -gt 0 ]; do
    case "$1" in
      --duration) DURATION="$2"; shift 2 ;;
      --run) RUN_REQUESTED=1; DRY_RUN=0; shift ;;
      --dry-run) RUN_REQUESTED=0; DRY_RUN=1; shift ;;
      -h|--help) print_usage; exit 0 ;;
      *) echo "unknown argument: $1" >&2; print_usage; exit 2 ;;
    esac
  done
  if [ "$RUN_REQUESTED" = 1 ]; then require_baseline; fi
}

# pt.porto.st-001 -> porto-st-001 (the Incus instance name, section 13.2)
station_instance() {
  local hier="$1"
  [[ "$hier" =~ ^pt\.([a-z][a-z0-9]*)\.(st-[0-9]{3})$ ]] || {
    echo "not a station hier-id: $hier" >&2; return 1
  }
  echo "${BASH_REMATCH[1]}-${BASH_REMATCH[2]}"
}

# --- roster helpers (dev/roster.yaml, section 9.1). The roster's grammar is regular
# enough for awk, so picking a card or a station does not need a YAML library / venv. ---

# first non-blocked card whose home_city is $1
pick_card() {
  local home="$1"
  awk -v home="$home" '
    /^- card:/ {
      if (card != "" && city == home && blocked == "false") { print card; found=1; exit }
      card=$3; city=""; blocked=""
    }
    /home_city:/ { city=$2 }
    /blocked:/   { blocked=$2 }
    END { if (!found && card != "" && city == home && blocked == "false") print card }
  ' "$ROSTER"
}

# first station (e.g. st-001) listed under city $1
first_station_for_city() {
  local want="$1"
  awk -v want="$want" '
    /^- city:/      { city=$3 }
    /^  - station:/ { if (city == want) { print $3; exit } }
  ' "$ROSTER"
}

# --- sim.env save/restore (section 13.3): save before the first edit, restore on exit. A
# station must never be left tampering or cloning once this script ends, whatever the
# reason it ended for. ---

# Reads the station's current sim.env into the save directory. Call once per instance,
# before the first edit, and register restore_sim_env as a cleanup right after.
save_sim_env() {
  local inst="$1"
  local save="$SIM_ENV_SAVE_DIR/$inst.sim.env"
  if [ "$DRY_RUN" = 1 ]; then
    would "incus exec $inst -- cat $SIM_ENV_PATH > <saved copy>  # save before editing"
    return 0
  fi
  incus exec "$inst" -- cat "$SIM_ENV_PATH" > "$save"
}

# Puts the saved sim.env back and restarts evsim so it picks it up. A no-op (with a
# warning) if save_sim_env was never called for this instance: never overwrite a station's
# sim.env with nothing to restore to.
restore_sim_env() {
  local inst="$1"
  local save="$SIM_ENV_SAVE_DIR/$inst.sim.env"
  if [ "$DRY_RUN" = 1 ]; then
    would "incus file push <saved copy> $inst$SIM_ENV_PATH;" \
          "incus exec $inst -- systemctl restart evsim"
    return 0
  fi
  if [ -f "$save" ]; then
    incus file push -q "$save" "$inst$SIM_ENV_PATH"
    incus exec "$inst" -- systemctl restart evsim
  else
    echo "warning: no saved sim.env for $inst (save_sim_env was never called); not touching it" >&2
  fi
}

# Rewrites SIM_EXTRA_ARGS in the station's sim.env (every other key kept from the saved
# copy) and restarts evsim. save_sim_env must have been called for this instance first.
set_sim_extra_args() {
  local inst="$1" extra="$2"
  local save="$SIM_ENV_SAVE_DIR/$inst.sim.env" new
  if [ "$DRY_RUN" = 1 ]; then
    would "write $SIM_ENV_PATH on $inst with SIM_EXTRA_ARGS=\"$extra\" (other keys kept" \
          "from the saved copy); incus exec $inst -- systemctl restart evsim"
    return 0
  fi
  [ -f "$save" ] || { echo "set_sim_extra_args: no saved sim.env for $inst" >&2; return 1; }
  new="$(mktemp "$SIM_ENV_SAVE_DIR/$inst.new.XXXXXX")"
  grep -v '^SIM_EXTRA_ARGS=' "$save" > "$new" || true
  printf 'SIM_EXTRA_ARGS=%s\n' "$extra" >> "$new"
  incus file push -q "$new" "$inst$SIM_ENV_PATH"
  incus exec "$inst" -- systemctl restart evsim
}
