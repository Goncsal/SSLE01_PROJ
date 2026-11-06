#!/usr/bin/env bash
# Orchestrates both fraud PoC scenarios in order, with a banner before each (what, expected
# detection, expected response), then a final check that every sim.env this run touched is
# back to the content it held before. See README.md for the before/after steps the RUN
# card performs around these calls.
#
#   run.sh [--run] [--duration N]
#
# Default: --dry-run (runs every sub-script with --dry-run too). --run is refused unless
# EVCHARGE_BASELINE_DONE=1 (checked here and again by each sub-script).
set -euo pipefail
# shellcheck source=./lib.sh
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

print_usage() {
  cat <<EOF
usage: $(basename "$0") [--run] [--duration SECONDS] [--dry-run]

Runs, in order: clone_card.sh --phase after (the detector-acting case), then
tamper_meter.sh --mode scale. Each prints its own banner and detail; this script only
sequences them and checks every sim.env this run touched comes back unchanged.
EOF
  usage_common
}

ARGS=()
while [ $# -gt 0 ]; do
  case "$1" in
    -h|--help) print_usage; exit 0 ;;
    *) ARGS+=("$1"); shift ;;
  esac
done
DURATION="${DURATION:-180}"
parse_global_flags "${ARGS[@]}"

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

# Stations this run touches, tracking clone_card.sh's and tamper_meter.sh's own defaults
# (home porto, used-in aveiro,lisboa; and lisboa st-003) -- update here if those defaults
# change.
CHECK_STATIONS=(pt.aveiro.st-001 pt.lisboa.st-001 pt.lisboa.st-003)

declare -A BASELINE=()
if [ "$DRY_RUN" = 1 ]; then
  would "snapshot sim.env on ${CHECK_STATIONS[*]} before running (verification baseline)"
else
  echo "-- snapshotting sim.env before running (verification baseline) --"
  for hier in "${CHECK_STATIONS[@]}"; do
    inst="$(station_instance "$hier")"
    BASELINE["$inst"]="$(incus exec "$inst" -- cat "$SIM_ENV_PATH")"
  done
fi

banner "1/2 cloned card (impossible travel)" \
       "fraud impossible_travel; Wazuh 110203/110206" \
       "block-card"
"$FRAUD_DIR/clone_card.sh" --phase after --duration "$DURATION" "$MODE_FLAG"

banner "2/2 meter tamper (scale, under-report)" \
       "invisible to the CSMS; Prometheus MeterUnderReporting; fraud grid_gap" \
       "quarantine-charger"
"$FRAUD_DIR/tamper_meter.sh" --mode scale --duration "$DURATION" "$MODE_FLAG"

echo
if [ "$DRY_RUN" = 1 ]; then
  would "verify sim.env on ${CHECK_STATIONS[*]} matches the pre-run baseline"
else
  echo "-- verifying every sim.env is back to its pre-run content --"
  fail=0
  for hier in "${CHECK_STATIONS[@]}"; do
    inst="$(station_instance "$hier")"
    now="$(incus exec "$inst" -- cat "$SIM_ENV_PATH")"
    if [ "$now" = "${BASELINE[$inst]}" ]; then
      ok "sim.env unchanged on $inst"
    else
      bad "sim.env on $inst does not match its pre-run content"
      fail=1
    fi
  done
  [ "$fail" -eq 0 ] || { echo "run.sh: sim.env verification FAILED" >&2; exit 1; }
fi

echo
echo "run.sh: both scenarios done; every touched sim.env restored and verified"
