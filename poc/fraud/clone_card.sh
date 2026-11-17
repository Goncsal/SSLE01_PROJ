#!/usr/bin/env bash
# Fraud PoC 1: a cloned RFID card authorises sessions in two cities within minutes
# (impossible travel), driven through the simulator's existing --clone-card flag -- no
# new attacker containers (docs/interfaces.md v6 9.2/9.4). See DESIGN-fraud.md "Cloned
# card" and 4.4 (impossible_travel rule, block_card action).
#
#   clone_card.sh [--home porto] [--used-in aveiro,lisboa] [--phase before|after]
#                 [--card CARD] [--run] [--duration N]
#
# Picks a non-blocked card whose home_city is --home from dev/roster.yaml, then on one
# station in each --used-in city sets SIM_EXTRA_ARGS="--clone-card <card> --clone-sessions
# 2" in /etc/evcharge/sim.env (saving the previous content first), restarts evsim there,
# waits for the sessions, then restores every sim.env to what it held before (on exit and
# on signal -- see save_sim_env/restore_sim_env in lib.sh).
#
# --phase only changes the printed expectation; this script never starts or stops the
# fraud service itself (the operator does that between --phase before and --phase after
# calls, per DESIGN-fraud.md "Before / after").
#
# Default: --dry-run (prints the plan, changes nothing). --run is refused unless
# EVCHARGE_BASELINE_DONE=1 (set in the environment before the live run).
set -euo pipefail
# shellcheck source=./lib.sh
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

HOME_CITY=porto
USED_IN=aveiro,lisboa
PHASE=after
CLONE_SESSIONS=2
CARD=""

print_usage() {
  cat <<EOF
usage: $(basename "$0") [--home CITY] [--used-in CITY,CITY,...] [--phase before|after] [--card CARD] [--run] [--duration SECONDS] [--dry-run]

Picks a non-blocked card whose home_city is --home (dev/roster.yaml), then on one station
in each --used-in city sets SIM_EXTRA_ARGS="--clone-card <card> --clone-sessions $CLONE_SESSIONS"
and restarts evsim. --phase before/after only changes the printed expectation.
EOF
  usage_common
}

ARGS=()
while [ $# -gt 0 ]; do
  case "$1" in
    --home) HOME_CITY="$2"; shift 2 ;;
    --used-in) USED_IN="$2"; shift 2 ;;
    --phase) PHASE="$2"; shift 2 ;;
    --card) CARD="$2"; shift 2 ;;
    -h|--help) print_usage; exit 0 ;;
    *) ARGS+=("$1"); shift ;;
  esac
done
DURATION="${DURATION:-120}"
parse_global_flags "${ARGS[@]}"

case "$PHASE" in
  before|after) ;;
  *) echo "--phase must be before or after" >&2; exit 2 ;;
esac

[ -n "$CARD" ] || CARD="$(pick_card "$HOME_CITY")"
[ -n "$CARD" ] || { echo "no non-blocked card with home_city=$HOME_CITY in $ROSTER" >&2; exit 2; }

IFS=',' read -r -a CITIES <<< "$USED_IN"
[ "${#CITIES[@]}" -ge 1 ] || { echo "--used-in needs at least one city" >&2; exit 2; }

echo "== fraud PoC 1: cloned card ($CARD, home $HOME_CITY) used in: ${CITIES[*]} =="
echo "  clone-sessions each  : $CLONE_SESSIONS"
echo "  time-box             : ${DURATION}s"
if [ "$PHASE" = before ]; then
  echo "  before               : fraud service stopped, or FRAUD_DRY_RUN=true -- the cloned"
  echo "                         sessions complete unflagged (no block-card); this is the"
  echo "                         detector-off baseline for the before/after comparison"
else
  echo "  after (default)      : fraud service running, FRAUD_DRY_RUN=false (the lab"
  echo "                         default) -- expect fraud.alert impossible_travel, then"
  echo "                         fraud.action block_card"
fi
echo "  expected detections  : fraud impossible_travel (distance_km, speed_kmh >"
echo "                         FRAUD_MAX_KMH, city coordinates from the registry); Wazuh"
echo "                         110207 (impossible travel), 110208 (fraud service alert)"
echo "  expected response    : 110210 (high-confidence cloned card) -> block-card"
echo "                         (billing: card Blocked + the sessions flagged)"
echo

declare -a INSTANCES=()
for city in "${CITIES[@]}"; do
  st="$(first_station_for_city "$city")"
  [ -n "$st" ] || { echo "no station found for city $city in $ROSTER" >&2; exit 2; }
  hier="pt.$city.$st"
  inst="$(station_instance "$hier")"
  INSTANCES+=("$inst")
  echo "  target station       : $hier  ($inst)"
done
echo

for inst in "${INSTANCES[@]}"; do
  save_sim_env "$inst"
  add_cleanup "restore_sim_env '$inst'"
done

for inst in "${INSTANCES[@]}"; do
  set_sim_extra_args "$inst" "--clone-card $CARD --clone-sessions $CLONE_SESSIONS"
done

if [ "$DRY_RUN" = 1 ]; then
  would "wait ${DURATION}s for the cloned sessions to complete in both cities"
else
  echo "-- waiting ${DURATION}s for the cloned sessions --"
  sleep "$DURATION" & wait $!   # background + wait: INT/TERM run the cleanup at once
fi

echo
echo "clone_card: done (every sim.env touched above will be restored to its saved content" \
     "on exit)"
