#!/usr/bin/env bash
# Orchestrates the botnet PoC end to end: deploy C2 -> baseline snapshot -> compromise 3
# Lisboa stations -> each beacon raises 110303 and the quarantine-charger active response
# fires automatically per station (110304 names the fleet) while the C2 schedule drives
# flood+surge -> measure the collateral of that automatic quarantine -> restore sim.env +
# delete C2. See README.md for the ACL concession and the rate-limit before/after the RUN
# card performs around these calls.
#
#   run.sh [--run] [--stations N] [--cities lisboa] [--duration N] [--dry-run]
#
# Default: --dry-run (runs every sub-script with --dry-run too). --run is refused unless
# EVCHARGE_BASELINE_DONE=1 (checked here and again by each sub-script).
set -euo pipefail
# shellcheck source=./lib.sh
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

STATIONS_N=3
CITIES_CSV="lisboa"

print_usage() {
  cat <<EOF
usage: $(basename "$0") [--run] [--stations N] [--cities lisboa] \\
       [--duration SECONDS] [--dry-run]

Runs, in order: deploy_c2.sh (backgrounded so its schedule can run alongside the other
stages), a baseline snapshot, compromise.sh (3 Lisboa stations by default), a wait while
the C2 schedule drives idle -> flood -> surge -> stop and the quarantine-charger active
response fires automatically on each station's beacon (110303), an after-snapshot of the
collateral, then compromise.sh --restore and the C2 is stopped.
EOF
  usage_common
}

ARGS=()
while [ $# -gt 0 ]; do
  case "$1" in
    --stations) STATIONS_N="$2"; shift 2 ;;
    --cities) CITIES_CSV="$2"; shift 2 ;;
    *) ARGS+=("$1"); shift ;;
  esac
done
parse_common_args "${ARGS[@]}"
MODE_FLAG="--dry-run"
[ "$DRY_RUN" = 1 ] || MODE_FLAG="--run"

banner() {
  echo
  echo "################################################################"
  printf '# %s\n' "$1"
  printf '#   expected detection : %s\n' "$2"
  printf '#   expected response  : %s\n' "$3"
  echo "################################################################"
}

C2_PID=""
stop_c2() {
  # Forward the signal/exit to the backgrounded deploy_c2.sh; its own trap deletes the
  # container. No separate incus call here -- one owner, one cleanup.
  if [ -n "$C2_PID" ] && kill -0 "$C2_PID" 2>/dev/null; then
    kill "$C2_PID" 2>/dev/null || true
    wait "$C2_PID" 2>/dev/null || true
  fi
}
add_cleanup "stop_c2"
add_cleanup "'$BOTNET_DIR/compromise.sh' --restore --cities '$CITIES_CSV' $MODE_FLAG"

banner "1/5 deploy C2" "n/a (the C2 itself is not instrumented)" "n/a"
if [ "$DRY_RUN" = 1 ]; then
  "$BOTNET_DIR/deploy_c2.sh" --duration "$DURATION" "$MODE_FLAG"
else
  "$BOTNET_DIR/deploy_c2.sh" --duration "$DURATION" "$MODE_FLAG" &
  C2_PID=$!
  sleep 5
fi

# Baseline BEFORE the compromise: the automatic quarantine fires on the first beacon
# (within a beacon interval of compromise), so a snapshot taken after compromise would
# race it. The healthy, pre-attack interrupted-session count is the right baseline for the
# collateral delta.
banner "2/5 baseline snapshot (healthy, pre-compromise)" "n/a" "n/a"
"$BOTNET_DIR/measure_collateral.sh" --phase before --cities "$CITIES_CSV" "$MODE_FLAG"

banner "3/5 compromise $STATIONS_N Lisboa station(s) ($CITIES_CSV)" \
       "host.new_outbound -> 110303 per station; 110304 names the fleet (3 stations -> C2 / 5 min)" \
       "110303 -> quarantine-charger per station (automatic active response)"
"$BOTNET_DIR/compromise.sh" --stations "$STATIONS_N" --cities "$CITIES_CSV" \
  --duration "$DURATION" "$MODE_FLAG"

banner "4/5 flood + surge (C2 schedule) while the automatic quarantine takes effect" \
       "ocpp.rate_limited/conn.kicked -> 110300/110301; edge limit_req -> 110306/110307; session.start burst -> 110308 + CityLoadSpike" \
       "quarantine-charger already firing automatically on 110303; one correlator incident (city=lisboa)"
if [ "$DRY_RUN" = 1 ]; then
  would "wait for the C2 schedule to reach flood/surge while quarantine-charger fires" \
        "automatically on each station's 110303 (confirm from evcharge-ar.json)"
else
  echo "waiting up to ${DURATION}s for the C2 schedule and the automatic quarantine..."
  sleep "$DURATION"
fi

banner "5/5 after-snapshot, restore, cleanup" "n/a" "n/a"
"$BOTNET_DIR/measure_collateral.sh" --phase after --cities "$CITIES_CSV" "$MODE_FLAG"

echo
echo "run.sh: scenario done; restoring sim.env and stopping the C2 (cleanup trap)"
