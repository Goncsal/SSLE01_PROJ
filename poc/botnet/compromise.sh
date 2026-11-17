#!/usr/bin/env bash
# Botnet PoC: enrols N stations across the given cities into the botnet by setting
# SIM_EXTRA_ARGS="--compromised --c2-url ... --beacon-interval ..." in their sim.env and
# restarting evsim (docs/interfaces.md v6 9.2/9.4, 13.3). DESIGN-botnet.md "Exploitation
# path". The compromise must outlive this script's own process (the C2 schedule drives
# flood/surge, detection and the bulk quarantine step afterwards) so it is NOT undone on
# normal exit; it is recorded in a state file and undone only by --restore (which the
# run script calls once the scenario is over) or, if this script itself is interrupted
# mid-loop, for the stations it had already touched this invocation.
#
#   compromise.sh [--stations N] [--cities porto,lisboa,aveiro] [--port 8099]
#                 [--beacon-interval 15] [--state-file PATH] [--restore]
#                 [--run] [--duration N] [--dry-run]
#
# Default: --dry-run. --run is refused unless EVCHARGE_BASELINE_DONE=1.
set -euo pipefail
# shellcheck source=./lib.sh
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

STATIONS_N=3
CITIES_CSV=""   # default filled per mode: "lisboa" to compromise, all cities to restore
PORT="${C2_PORT:-8099}"
BEACON=15
RESTORE=0
# A durable, repo-local record of what was touched. The real restore does not depend on it
# (it detects compromise on the instances directly), so a lost file never strands a station.
STATE_FILE="${EVCHARGE_BOTNET_STATE:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/.botnet-compromise.state}"

print_usage() {
  cat <<EOF
usage: $(basename "$0") [--stations N] [--cities lisboa] [--port N] \\
       [--beacon-interval N] [--state-file PATH] [--restore] \\
       [--run] [--duration SECONDS] [--dry-run]

Sets SIM_EXTRA_ARGS="--compromised --c2-url http://c2.incus:<port> --beacon-interval <N>"
on N stations spread round-robin across the given cities (3 Lisboa stations by default),
restarts evsim on each, and prints the per-station and cross-station detections expected.
Each touched station keeps a pre-compromise copy of its sim.env inside the instance, so
--restore works after a host reboot. --restore scans every station in --cities (all three
cities by default) and cleans any that still carry the compromise flag; no --stations
needed, and it is safe to run when nothing is compromised.
EOF
  usage_common
}

ARGS=()
while [ $# -gt 0 ]; do
  case "$1" in
    --stations) STATIONS_N="$2"; shift 2 ;;
    --cities) CITIES_CSV="$2"; shift 2 ;;
    --port) PORT="$2"; shift 2 ;;
    --beacon-interval) BEACON="$2"; shift 2 ;;
    --state-file) STATE_FILE="$2"; shift 2 ;;
    --restore) RESTORE=1; shift ;;
    *) ARGS+=("$1"); shift ;;
  esac
done
parse_common_args "${ARGS[@]}"

# restore cleans every city by default (so a blind --restore un-strands any station);
# compromise defaults to Lisboa.
if [ -z "$CITIES_CSV" ]; then
  [ "$RESTORE" = 1 ] && CITIES_CSV="porto,lisboa,aveiro" || CITIES_CSV="lisboa"
fi

C2_URL="$(c2_url "$PORT")"
EXTRA="--compromised --c2-url $C2_URL --beacon-interval $BEACON"

# --- restore mode: detect compromise on the instances themselves and undo it. This does
# not read the host state file, so it still works after a reboot or if the file is gone,
# and it cleans stations stranded by an earlier run. ---
if [ "$RESTORE" = 1 ]; then
  echo "== botnet PoC: restore sim.env on compromised stations =="
  restored=0
  # read the whole station list up front: the incus calls below would otherwise drain a
  # streamed stdin and cut the loop short after the first station.
  mapfile -t ALL_STATIONS < <(all_stations_in "$CITIES_CSV")
  for s in "${ALL_STATIONS[@]}"; do
    [ -n "$s" ] || continue
    inst="$(station_instance "$s")"
    station_is_compromised "$inst" || continue
    echo "  restoring $inst"
    if [ "$DRY_RUN" = 1 ]; then
      would "restore $SIM_ENV_PATH on $inst from its in-instance backup (or strip SIM_EXTRA_ARGS)"
      would "incus exec $inst -- systemctl restart evsim"
    else
      restore_sim_env "$inst"
    fi
    restored=$((restored + 1))
  done
  [ "$DRY_RUN" = 1 ] || rm -f "$STATE_FILE"
  echo "restore: done ($restored station(s) cleaned)"
  exit 0
fi

# --- compromise mode ---
mapfile -t STATIONS < <(pick_stations "$STATIONS_N" "$CITIES_CSV")

echo "== botnet PoC: compromise ${#STATIONS[@]} station(s) =="
echo "  cities                   : $CITIES_CSV"
echo "  c2                       : $C2_URL"
echo "  beacon every             : ${BEACON}s"
echo "  stations                 :"
for s in "${STATIONS[@]}"; do echo "    - $s ($(station_instance "$s"))"; done
echo "  expected (per station)   : host.new_outbound to $C2_URL -> netwatch 110303 ->" \
     "quarantine-charger fires automatically on that station (station-level response)"
echo "  expected (cross-station) : 110304 (freq 3/300, same_field dstip, different_field" \
     "hier_id -- names the fleet beaconing to $C2_URL, the 'locate the fleet' signal)"
echo "  expected (once flood/surge land) : ocpp.rate_limited/conn.kicked -> 110300/110301," \
     "edge limit_req -> 110306/110307; session.start burst per city -> 110308, joined with" \
     "CityLoadSpike; the correlator folds these into one incident keyed on the city"
echo

# touched this invocation, for a partial undo only if we are interrupted mid-loop
TOUCHED_SOFAR=()
restore_touched_sofar() {
  local entry inst content
  for entry in "${TOUCHED_SOFAR[@]}"; do
    inst="${entry%%|*}"
    content="${entry#*|}"
    content="${content//$'\x01'/$'\n'}"
    write_sim_env "$inst" "$content" || true
    incus exec "$inst" -- rm -f "$SIM_ENV_BACKUP" 2>/dev/null || true
    restart_evsim "$inst" || true
  done
}
trap 'restore_touched_sofar; run_cleanup; exit 130' INT TERM

if [ "$DRY_RUN" = 0 ]; then : > "$STATE_FILE.tmp"; fi
for s in "${STATIONS[@]}"; do
  inst="$(station_instance "$s")"
  if [ "$DRY_RUN" = 1 ]; then
    would "copy sim.env to $SIM_ENV_BACKUP inside $inst (restore point)"
    would "incus exec $inst -- sh -c 'cat > /etc/evcharge/sim.env'  # SIM_EXTRA_ARGS=\"$EXTRA\""
    would "incus exec $inst -- systemctl restart evsim"
  else
    orig="$(read_sim_env "$inst")"
    backup_sim_env "$inst" "$orig"     # durable copy inside the instance (reboot-proof)
    TOUCHED_SOFAR+=("$inst|${orig//$'\n'/$'\x01'}")
    printf '%s|%s\n' "$inst" "${orig//$'\n'/$'\x01'}" >> "$STATE_FILE.tmp"
    new="$(set_extra_args "$orig" "$EXTRA")"
    write_sim_env "$inst" "$new"
    restart_evsim "$inst"
  fi
done
if [ "$DRY_RUN" = 0 ]; then
  cat "$STATE_FILE.tmp" >> "$STATE_FILE"
  rm -f "$STATE_FILE.tmp"
  echo "state saved to $STATE_FILE (undo with: $0 --restore)"
fi

echo
if [ "$DRY_RUN" = 1 ]; then
  echo "compromise: done (dry-run, nothing changed)"
else
  echo "compromise: done (${#STATIONS[@]} station(s) now compromised; left running for the" \
       "flood/surge/detection stages -- restore with: $0 --restore)"
fi
