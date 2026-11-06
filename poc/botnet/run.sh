#!/usr/bin/env bash
# Orchestrates the botnet PoC end to end: deploy C2 -> compromise stations -> the C2
# schedule drives flood+surge -> (detection) -> bulk-quarantine step (the W8-run card
# performs the actual quarantine-charger calls, or confirms the active response fired;
# this script only waits and measures) -> measure collateral damage -> restore sim.env +
# delete C2. See README.md for the before/after mitigation steps (ACL concession,
# rate-limit toggle) the RUN card performs around these calls.
#
#   run.sh [--run] [--stations N] [--cities porto,lisboa,aveiro] [--duration N] [--dry-run]
#
# Default: --dry-run (runs every sub-script with --dry-run too). --run is refused unless
# EVCHARGE_BASELINE_DONE=1 (checked here and again by each sub-script).
set -euo pipefail
# shellcheck source=./lib.sh
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

STATIONS_N=6
CITIES_CSV="porto,lisboa,aveiro"

print_usage() {
  cat <<EOF
usage: $(basename "$0") [--run] [--stations N] [--cities porto,lisboa,aveiro] \\
       [--duration SECONDS] [--dry-run]

Runs, in order: deploy_c2.sh (backgrounded so its schedule can run alongside the other
stages), compromise.sh, a before-snapshot, a wait while the C2 schedule drives
idle -> flood -> surge -> stop and (outside this script) the bulk quarantine fires, an
after-snapshot, then compromise.sh --restore and the C2 is stopped.
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

banner "2/5 compromise $STATIONS_N station(s) across $CITIES_CSV" \
       "host.new_outbound -> 110303 per station; 110304 cross-station (same C2, names the fleet)" \
       "none yet (detection only)"
"$BOTNET_DIR/compromise.sh" --stations "$STATIONS_N" --cities "$CITIES_CSV" \
  --duration "$DURATION" "$MODE_FLAG"

banner "3/5 before-snapshot" "n/a" "n/a"
"$BOTNET_DIR/measure_collateral.sh" --phase before --cities "$CITIES_CSV" "$MODE_FLAG"

banner "4/5 flood + surge (driven by the C2 schedule) -> bulk quarantine" \
       "ocpp.rate_limited/conn.kicked -> 110300/110301/110307; session.start burst -> 110308 + CityLoadSpike" \
       "bulk quarantine (quarantine-charger per station) -- performed by the W8-run card, not this script"
if [ "$DRY_RUN" = 1 ]; then
  would "wait for the C2 schedule to reach flood/surge, then for the W8-run card's" \
        "quarantine-charger calls (or confirm the AR fired from evcharge-ar.json)"
else
  echo "waiting up to ${DURATION}s for the C2 schedule and the quarantine step" \
       "(performed outside this script)..."
  sleep "$DURATION"
fi

banner "5/5 after-snapshot, restore, cleanup" "n/a" "n/a"
"$BOTNET_DIR/measure_collateral.sh" --phase after --cities "$CITIES_CSV" "$MODE_FLAG"

echo
echo "run.sh: scenario done; restoring sim.env and stopping the C2 (cleanup trap)"
