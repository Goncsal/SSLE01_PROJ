#!/usr/bin/env bash
# Botnet PoC: the collateral-damage measurement for the bulk-quarantine response
# (DESIGN-botnet.md "Response and justification"; docs/interfaces.md v6 7.2 sim/csms
# metrics, 7.3 city recording rules). Snapshots sum(evcharge_sim_sessions_total
# {result="interrupted"}) per city (city Prometheus, per_charger scope, 7.2) and the
# CSMS connected-chargers/stopped-session totals (central Prometheus) before and after
# the bulk quarantine, and reports the delta: sessions interrupted by the response and
# an estimate of the kWh not delivered because of it -- the honest cost side of the
# argument in DESIGN-botnet.md.
#
#   measure_collateral.sh --phase before|after [--cities porto,lisboa,aveiro]
#                          [--session-minutes 8] [--state-file PATH]
#                          [--run] [--duration N] [--dry-run]
#
# Read-only against the lab: issues Prometheus queries through `incus exec` (python3,
# standard library), changes no lab state either way. --dry-run prints what it would
# query; --run actually queries (still gated by EVCHARGE_BASELINE_DONE=1, because a live
# query during the recorded baseline window would itself be unwanted load/attention).
set -euo pipefail
# shellcheck source=./lib.sh
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

PHASE="before"
CITIES_CSV="porto,lisboa,aveiro"
SESSION_MINUTES=8
STATE_FILE="${EVCHARGE_BOTNET_COLLATERAL_STATE:-/tmp/evcharge-botnet-collateral-before.state}"

print_usage() {
  cat <<EOF
usage: $(basename "$0") --phase before|after [--cities porto,lisboa,aveiro] \\
       [--session-minutes N] [--state-file PATH] [--run] [--duration SECONDS] [--dry-run]

before: snapshots interrupted-session counts, average charging power, CSMS connected
chargers and stopped-session totals, and saves them to --state-file.
after: re-snapshots the same, reads --state-file, and prints the delta: sessions
interrupted by the response, chargers taken off line, and the kWh-not-delivered estimate
(interrupted sessions x average charging power x half the mean session length -- stated
method, see README.md).
EOF
  usage_common
}

ARGS=()
while [ $# -gt 0 ]; do
  case "$1" in
    --phase) PHASE="$2"; shift 2 ;;
    --cities) CITIES_CSV="$2"; shift 2 ;;
    --session-minutes) SESSION_MINUTES="$2"; shift 2 ;;
    --state-file) STATE_FILE="$2"; shift 2 ;;
    *) ARGS+=("$1"); shift ;;
  esac
done
parse_common_args "${ARGS[@]}"

case "$PHASE" in
  before|after) ;;
  *) echo "--phase must be before or after" >&2; exit 2 ;;
esac

IFS=',' read -r -a CITIES <<< "$CITIES_CSV"

echo "== botnet PoC: collateral-damage snapshot ($PHASE) =="
echo "  cities   : $CITIES_CSV"
echo "  queries  : sum(evcharge_sim_sessions_total{result=\"interrupted\"}) per city (city Prometheus)"
echo "             avg charging power (evcharge_sim_power_watts over evcharge_sim_charger_status==2)"
echo "             evcharge_csms_connected_chargers, evcharge_csms_sessions_stopped_total (central Prometheus)"
echo

# Prints one line per city ("<city> interrupted=<n> power_avg_w=<n>") then one total line
# ("total connected=<n> stopped=<n>"). Dry-run prints the plan and zeroes instead.
snapshot() {
  local city interrupted power_avg connected stopped
  for city in "${CITIES[@]}"; do
    if [ "$DRY_RUN" = 1 ]; then
      would "query city Prometheus on $city-edge: sum(evcharge_sim_sessions_total{result=\"interrupted\"})"
      would "query city Prometheus on $city-edge: avg charging power_watts"
      interrupted=0; power_avg=0
    else
      interrupted="$(city_prom_query "$city" 'sum(evcharge_sim_sessions_total{result="interrupted"})')"
      power_avg="$(city_prom_query "$city" \
        'sum(evcharge_sim_power_watts) / clamp_min(sum(evcharge_sim_charger_status == bool 2), 1)')"
    fi
    printf '%s interrupted=%s power_avg_w=%s\n' "$city" "${interrupted:-0}" "${power_avg:-0}"
  done
  if [ "$DRY_RUN" = 1 ]; then
    would "query central Prometheus: sum(evcharge_csms_connected_chargers), sum(evcharge_csms_sessions_stopped_total)"
    connected=0; stopped=0
  else
    connected="$(central_prom_query 'sum(evcharge_csms_connected_chargers)')"
    stopped="$(central_prom_query 'sum(evcharge_csms_sessions_stopped_total)')"
  fi
  printf 'total connected=%s stopped=%s\n' "${connected:-0}" "${stopped:-0}"
}

if [ "$PHASE" = before ]; then
  if [ "$DRY_RUN" = 1 ]; then
    would "save this snapshot to $STATE_FILE"
    snapshot
  else
    snapshot | tee "$STATE_FILE"
    echo "saved to $STATE_FILE"
  fi
  echo
  echo "measure_collateral: before-snapshot recorded here (interrupted sessions baseline," \
       "taken before the bulk quarantine)"
  exit 0
fi

# --- phase = after ---
echo "-- after --"
if [ "$DRY_RUN" = 1 ]; then
  would "read before-snapshot from $STATE_FILE"
  snapshot
  echo
  echo "would: print the delta (interrupted sessions caused by the response, chargers" \
       "taken off line, kWh not delivered) -- interrupted sessions is the headline number"
  exit 0
fi

if [ ! -f "$STATE_FILE" ]; then
  echo "no before-snapshot at $STATE_FILE; run --phase before first" >&2
  exit 1
fi

AFTER_FILE="$(mktemp /tmp/evcharge-botnet-collateral-after.XXXXXX)"
snapshot > "$AFTER_FILE"
cat "$AFTER_FILE"
echo
awk -v session_min="$SESSION_MINUTES" '
  FNR == NR {
    if ($1 == "total") { for (i = 2; i <= NF; i++) { split($i, kv, "="); b_tot[kv[1]] = kv[2] } }
    else { split($2, kv, "="); b_int[$1] = kv[2]; split($3, kv2, "="); b_pow[$1] = kv2[2] }
    next
  }
  {
    if ($1 == "total") { for (i = 2; i <= NF; i++) { split($i, kv, "="); a_tot[kv[1]] = kv[2] } }
    else {
      split($2, kv, "="); a_int = kv[2]; split($3, kv2, "="); ap = kv2[2]
      city = $1
      b_i = (city in b_int) ? b_int[city] : 0
      d = a_int - b_i; if (d < 0) d = 0
      kwh = d * (ap / 1000.0) * (session_min / 2 / 60)
      tot_int += d; tot_kwh += kwh
      printf "%-10s interrupted+=%d  avg_power_w=%.0f  kwh_not_delivered=%.2f\n", city, d, ap, kwh
    }
  }
  END {
    printf "\ninterrupted sessions (the response'\''s doing): %d\n", tot_int
    printf "kWh not delivered (estimate, avg power x half the mean session length): %.1f\n", tot_kwh
    bc = ("connected" in b_tot) ? b_tot["connected"] : 0
    ac = ("connected" in a_tot) ? a_tot["connected"] : 0
    printf "chargers off line: %d -> %d (delta %d)\n", bc, ac, bc - ac
    bs = ("stopped" in b_tot) ? b_tot["stopped"] : 0
    as_ = ("stopped" in a_tot) ? a_tot["stopped"] : 0
    printf "CSMS stopped-session total: %.0f -> %.0f\n", bs, as_
  }
' "$STATE_FILE" "$AFTER_FILE"
rm -f "$AFTER_FILE"
