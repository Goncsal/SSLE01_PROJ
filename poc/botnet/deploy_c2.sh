#!/usr/bin/env bash
# Botnet PoC: launches the throwaway `c2` container the compromised stations beacon to
# (docs/interfaces.md v6 9.4; DESIGN-botnet.md "Exploitation path"). Pushes a copy of
# sim/tests/stub_c2.py and this PoC's scheduler wrapper (c2_schedule.py) into it and runs
# the fixed task schedule idle -> flood -> surge -> stop (9.4). Reachable from a
# compromised station only because the ACL test allows a bot this one central-bridge
# host (section 16, the stated lab concession) -- see README.md.
#
#   deploy_c2.sh [--port 8099] [--idle-s 30] [--flood-s 60] [--flood-rate-hz 20]
#                [--surge-s 120] [--stop-s 30] [--run] [--duration N] [--dry-run]
#
# Default: --dry-run. --run is refused unless EVCHARGE_BASELINE_DONE=1.
set -euo pipefail
# shellcheck source=./lib.sh
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

PORT="${C2_PORT:-8099}"
IDLE_S=30
FLOOD_S=60
FLOOD_RATE_HZ=20
SURGE_S=120
STOP_S=30

print_usage() {
  cat <<EOF
usage: $(basename "$0") [--port N] [--idle-s N] [--flood-s N] [--flood-rate-hz N] \\
       [--surge-s N] [--stop-s N] [--run] [--duration SECONDS] [--dry-run]

Launches 'c2' on the central bridge (no --network: the lab's default profile network),
pushes stub_c2.py and c2_schedule.py into it, and runs the fixed schedule (idle, flood,
surge, stop), each phase timed as given. Prints the address bots use
(http://c2.incus:<port>) and deletes the container on exit and on INT/TERM.
EOF
  usage_common
}

ARGS=()
while [ $# -gt 0 ]; do
  case "$1" in
    --port) PORT="$2"; shift 2 ;;
    --idle-s) IDLE_S="$2"; shift 2 ;;
    --flood-s) FLOOD_S="$2"; shift 2 ;;
    --flood-rate-hz) FLOOD_RATE_HZ="$2"; shift 2 ;;
    --surge-s) SURGE_S="$2"; shift 2 ;;
    --stop-s) STOP_S="$2"; shift 2 ;;
    *) ARGS+=("$1"); shift ;;
  esac
done
parse_common_args "${ARGS[@]}"

C2_URL="$(c2_url "$PORT")"
TOTAL_S=$(( ${IDLE_S%.*} + ${FLOOD_S%.*} + ${SURGE_S%.*} + ${STOP_S%.*} ))

echo "== botnet PoC: deploy C2 =="
echo "  container  : $C2_NAME (central bridge)"
echo "  address    : $C2_URL"
echo "  schedule   : idle ${IDLE_S}s -> flood ${FLOOD_S}s (${FLOOD_RATE_HZ}Hz) -> surge ${SURGE_S}s -> stop ${STOP_S}s"
echo "  total      : ${TOTAL_S}s (hard-capped by --duration ${DURATION}s)"
echo "  concession : reachable from a compromised station only because the ACL test allows"
echo "               a bot this one central-bridge host (docs/interfaces.md v6 section 16)"
echo

add_cleanup "incus delete -f '$C2_NAME' >/dev/null 2>&1 || true"

run_or_plan incus launch "$CITY_IMG" "$C2_NAME"
if [ "$DRY_RUN" = 1 ]; then
  would "wait_ready $C2_NAME"
  would "incus file push $ROOT/sim/tests/stub_c2.py $C2_NAME/opt/evcharge/stub_c2.py"
  would "incus file push $BOTNET_DIR/c2_schedule.py $C2_NAME/opt/evcharge/c2_schedule.py"
  would "incus exec $C2_NAME -- python3 /opt/evcharge/c2_schedule.py --port $PORT" \
        "--idle-s $IDLE_S --flood-s $FLOOD_S --flood-rate-hz $FLOOD_RATE_HZ" \
        "--surge-s $SURGE_S --stop-s $STOP_S  # default --host binds every interface"
  would "print c2 address: $C2_URL"
else
  wait_ready "$C2_NAME"
  incus file push -q "$ROOT/sim/tests/stub_c2.py" "$C2_NAME/opt/evcharge/stub_c2.py"
  incus file push -q "$BOTNET_DIR/c2_schedule.py" "$C2_NAME/opt/evcharge/c2_schedule.py"
  echo "c2 address: $C2_URL"
  timeout "$DURATION" incus exec "$C2_NAME" -- \
    /opt/evcharge/venv/bin/python3 /opt/evcharge/c2_schedule.py --port "$PORT" \
    --idle-s "$IDLE_S" --flood-s "$FLOOD_S" --flood-rate-hz "$FLOOD_RATE_HZ" \
    --surge-s "$SURGE_S" --stop-s "$STOP_S"
fi

echo
echo "deploy_c2: done (c2 will be deleted on exit)"
