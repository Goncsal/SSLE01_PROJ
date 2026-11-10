#!/usr/bin/env bash
# Fraud PoC 2: a charger tampers with its own meter readings (under-report, rollback or
# freeze), driven through the simulator's existing --tamper flag (docs/interfaces.md v6
# 9.2/9.4, 7.2 feeder meter). See DESIGN-fraud.md "Meter tamper" and 4.4 (meter_backwards,
# grid_gap rules, quarantine_charger action).
#
#   tamper_meter.sh [--city lisboa] [--station pt.lisboa.st-003]
#                   [--mode scale|rollback|freeze] [--factor 0.5] [--extra-args "FLAGS"]
#                   [--run] [--duration N]
#
# --extra-args appends simulator flags (e.g. "--session-minutes 3 --arrival-rate 30": short,
# frequent sessions, so power_gap has enough charging samples inside the PoC window).
#
# Sets SIM_EXTRA_ARGS on one station's sim.env to the chosen tamper mode (saving the
# previous content first), restarts evsim, waits, then restores the saved sim.env (on
# exit and on signal -- see save_sim_env/restore_sim_env in lib.sh). --tamper-chargers is
# left at its default (every charger at the station) to keep the PoC simple.
#
# Default: --dry-run (prints the plan, changes nothing). --run is refused unless
# EVCHARGE_BASELINE_DONE=1 (set by the matching W7-run card).
set -euo pipefail
# shellcheck source=./lib.sh
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

CITY=lisboa
STATION=""
MODE=scale
FACTOR=0.5
EXTRA=""

print_usage() {
  cat <<EOF
usage: $(basename "$0") [--city CITY] [--station HIER_ID] [--mode scale|rollback|freeze] [--factor F] [--extra-args "SIM FLAGS"] [--run] [--duration SECONDS] [--dry-run]

scale/freeze take --factor (0 < F < 1, default 0.5, the under-report ratio: 9.2);
rollback ignores --factor (the register jumps back a fixed 500 Wh once per session).
EOF
  usage_common
}

ARGS=()
while [ $# -gt 0 ]; do
  case "$1" in
    --city) CITY="$2"; shift 2 ;;
    --station) STATION="$2"; shift 2 ;;
    --mode) MODE="$2"; shift 2 ;;
    --factor) FACTOR="$2"; shift 2 ;;
    --extra-args) EXTRA="$2"; shift 2 ;;
    -h|--help) print_usage; exit 0 ;;
    *) ARGS+=("$1"); shift ;;
  esac
done
DURATION="${DURATION:-180}"
parse_global_flags "${ARGS[@]}"

case "$MODE" in
  scale|rollback|freeze) ;;
  *) echo "--mode must be scale, rollback or freeze" >&2; exit 2 ;;
esac
[ -n "$STATION" ] || STATION="pt.$CITY.st-003"

INST="$(station_instance "$STATION")"

TAMPER_ARGS="--tamper $FACTOR --tamper-mode $MODE"
if [ "$MODE" = rollback ]; then
  # --factor is meaningless for rollback (fixed 500 Wh jump, section 9.2); keep a value
  # evsim accepts without claiming --factor did anything here.
  TAMPER_ARGS="--tamper 0.5 --tamper-mode rollback"
fi

[ -z "$EXTRA" ] || TAMPER_ARGS="$TAMPER_ARGS $EXTRA"

echo "== fraud PoC 2: meter tamper ($MODE) on $STATION ($INST) =="
echo "  tamper args          : $TAMPER_ARGS"
echo "  time-box             : ${DURATION}s"
case "$MODE" in
  scale)
    echo "  expected detections : invisible to the CSMS anomaly check (the readings stay"
    echo "                        internally consistent) -- caught by the feeder-meter"
    echo "                        power_gap fraud rule and Prometheus MeterUnderReporting"
    echo "                        (city:station_power_gap_watts)"
    echo "  expected response    : quarantine-charger"
    ;;
  rollback)
    echo "  expected detections : CSMS meter.anomaly (reason=backwards); fraud"
    echo "                        meter_backwards; Wazuh 110204/110205 (meter.anomaly"
    echo "                        frequency per charger)"
    echo "  expected response    : quarantine-charger"
    ;;
  freeze)
    echo "  expected detections : the register stops advancing mid-session -- fraud"
    echo "                        peer_deviation (station well below its city peers) and"
    echo "                        the grid_gap rule against the feeder meter"
    echo "  expected response    : quarantine-charger"
    ;;
esac
echo "  note                 : the station's feeder-meter metric (7.2) keeps the true"
echo "                        power throughout -- the gap against it is what catches"
echo "                        under-reporting that the CSMS cannot see on its own"
echo

save_sim_env "$INST"
add_cleanup "restore_sim_env '$INST'"
set_sim_extra_args "$INST" "$TAMPER_ARGS"

if [ "$DRY_RUN" = 1 ]; then
  would "wait ${DURATION}s while the station reports tampered meter values"
else
  echo "-- waiting ${DURATION}s --"
  sleep "$DURATION" & wait $!   # background + wait: INT/TERM run the cleanup at once
fi

echo
echo "tamper_meter: done (sim.env will be restored to its saved content on exit)"
