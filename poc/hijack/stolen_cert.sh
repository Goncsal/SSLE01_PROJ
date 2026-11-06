#!/usr/bin/env bash
# Hijacking PoC 2: the focused stolen-certificate-from-another-host case, used for the
# CSMS_ENFORCE_STATION_IP before/after (docs/interfaces.md v6 5.2 check 9, 12.5). The
# RUN card toggles that setting between two runs of this script; this script only runs
# the PoC and labels the outcome it expects for the phase given.
#
#   stolen_cert.sh [--city porto] [--station pt.porto.st-001] [--phase before|after]
#                  [--run] [--duration N]
#
# Default: --dry-run. --run is refused unless EVCHARGE_BASELINE_DONE=1.
set -euo pipefail
# shellcheck source=./lib.sh
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

PHASE="before"

print_usage() {
  cat <<EOF
usage: $(basename "$0") [--city CITY] [--station HIER_ID] [--phase before|after] [--run] [--duration SECONDS] [--dry-run]

Copies the real station's certificate and key into a rogue container on the same city
network and connects with it. This script does not change CSMS_ENFORCE_STATION_IP
itself (ConfigMap csms-edge, section 5.2/12.5) -- the RUN card sets that before each
call and passes --phase to match, so the printed "expected" line is correct either way.
EOF
  usage_common
}

# consume --phase before the shared parser (parse_common_args would reject it)
ARGS=()
while [ $# -gt 0 ]; do
  case "$1" in
    --phase) PHASE="$2"; shift 2 ;;
    *) ARGS+=("$1"); shift ;;
  esac
done
parse_common_args "${ARGS[@]}"

case "$PHASE" in
  before|after) ;;
  *) echo "--phase must be before or after" >&2; exit 2 ;;
esac

CHARGER="$STATION.cp-1"
EDGE_URL="$(edge_url "$CITY")"
ROGUE="$(rogue_name "$CITY")"
PKI_STOLEN=/etc/evcharge/pki-stolen

echo "== hijacking PoC 2: stolen certificate from another host (CSMS_ENFORCE_STATION_IP=$PHASE) =="
echo "  charger id : $CHARGER"
echo "  edge url   : $EDGE_URL"
echo "  rogue      : $ROGUE  (city network: city-$CITY)"
if [ "$PHASE" = before ]; then
  echo "  expected   : accepted; ident.ip_mismatch (110104) logged only (check 9, CSMS_ENFORCE_STATION_IP=false)"
else
  echo "  expected   : refused, 403 ident.ip_mismatch (check 9, CSMS_ENFORCE_STATION_IP=true)"
fi
echo "  time-box   : ${DURATION}s"
echo

add_cleanup "incus delete -f '$ROGUE' >/dev/null 2>&1 || true"

run_or_plan incus launch "$CITY_IMG" "$ROGUE" --network "city-$CITY"
if [ "$DRY_RUN" = 1 ]; then
  would "wait_ready $ROGUE"
  would "incus file push $ROSTER $ROGUE/etc/evcharge/roster.yaml"
  would "$ROOT/pki/export.sh $CHARGER $ROGUE --dir $PKI_STOLEN"
  would "incus exec $ROGUE -- python -m evsim --roster /etc/evcharge/roster.yaml" \
        "--station $STATION --chargers cp-1 --csms-url $EDGE_URL" \
        "--pki-dir $PKI_STOLEN --metrics-port 0 --duration $DURATION"
else
  wait_ready "$ROGUE"
  incus file push -q "$ROSTER" "$ROGUE/etc/evcharge/roster.yaml"
  "$ROOT/pki/export.sh" "$CHARGER" "$ROGUE" --dir "$PKI_STOLEN"
  timeout "$((DURATION + 10))" incus exec "$ROGUE" -- \
    /opt/evcharge/venv/bin/python -m evsim \
    --roster /etc/evcharge/roster.yaml --station "$STATION" --chargers cp-1 \
    --csms-url "$EDGE_URL" --pki-dir "$PKI_STOLEN" --metrics-port 0 --duration "$DURATION"
fi

echo
echo "stolen_cert: done (rogue will be deleted on exit)"
