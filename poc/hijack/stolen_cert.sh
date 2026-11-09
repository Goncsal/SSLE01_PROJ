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
STATION_INST="$(station_instance "$STATION")"
PKI_STOLEN=/etc/evcharge/pki-stolen

echo "== hijacking PoC 2: stolen certificate from another host (CSMS_ENFORCE_STATION_IP=$PHASE) =="
echo "  charger id    : $CHARGER"
echo "  edge url      : $EDGE_URL"
echo "  rogue         : $ROGUE  (city network: city-$CITY)"
echo "  real station  : $STATION_INST (its evsim is stopped first: this PoC isolates check 9"
echo "                   (ip_mismatch), it is not the duplicate-id case -- that is rogue_dupe.sh,"
echo "                   which leaves the real station running on purpose)"
if [ "$PHASE" = before ]; then
  echo "  expected      : real station evsim stopped first (no duplicate-id collision); rogue" \
       "accepted, ident.ip_mismatch (110104) logged only (check 9, CSMS_ENFORCE_STATION_IP=false)"
else
  echo "  expected      : refused, 403 ident.ip_mismatch (check 9, CSMS_ENFORCE_STATION_IP=true)"
fi
echo "  time-box      : ${DURATION}s"
echo

if [ "$DRY_RUN" = 1 ]; then
  add_cleanup "incus delete -f '$ROGUE' >/dev/null 2>&1 || true"
  would "incus launch $CITY_IMG $ROGUE --network city-$CITY"
  would "wait_ready $ROGUE"
  would "incus file push $ROSTER $ROGUE/etc/evcharge/roster.yaml"
  add_cleanup "incus exec '$STATION_INST' -- systemctl start evsim >/dev/null 2>&1 || true"
  would "incus exec -n -T $STATION_INST -- systemctl stop evsim" \
        "(isolates check 9: no live connection left for the rogue's claim to collide with)"
  would "settle 5s so the real station's connections row is released before the rogue connects"
  would "$ROOT/pki/export.sh $CHARGER $ROGUE --dir $PKI_STOLEN"
  would "incus exec -n -T $ROGUE -- python -m evsim --roster /etc/evcharge/roster.yaml" \
        "--station $STATION --chargers cp-1 --csms-url $EDGE_URL" \
        "--pki-dir $PKI_STOLEN --metrics-port 0 --duration $DURATION"
else
  incus launch "$CITY_IMG" "$ROGUE" --network "city-$CITY"
  # Register the delete only now that the launch actually succeeded.
  add_cleanup "incus delete -f '$ROGUE' >/dev/null 2>&1 || true"
  wait_ready "$ROGUE"
  incus file push -q "$ROSTER" "$ROGUE/etc/evcharge/roster.yaml"
  # This PoC isolates check 9 (ip_mismatch); it is NOT the duplicate-id case (that is
  # rogue_dupe.sh, which leaves the real station running on purpose). With the real
  # station still connected, check 10 (charger.duplicate_id) fires regardless of
  # CSMS_ENFORCE_STATION_IP and masks the ip_mismatch-only story this script is meant to
  # show -- so stop the real station's evsim first, and only restart it once this
  # script's own cleanup runs (registered immediately after the stop succeeds, so the
  # EXIT trap always restarts it, dry-run or not, success or failure).
  incus exec -n -T "$STATION_INST" -- systemctl stop evsim
  add_cleanup "incus exec '$STATION_INST' -- systemctl start evsim >/dev/null 2>&1 || true"
  sleep 5
  "$ROOT/pki/export.sh" "$CHARGER" "$ROGUE" --dir "$PKI_STOLEN"
  # ident.ip_mismatch (before) / a 403 refusal (after, CSMS_ENFORCE_STATION_IP=true) are
  # both expected outcomes, not script failures -- see run_evsim in lib.sh. -n -T: evsim
  # reads no stdin and needs no tty; without them, running under `timeout` inside the
  # operator's own interactive-stdin pipeline can set up an exec control channel that
  # drops (incusd: "close 1006 (abnormal closure)"), killing evsim outright.
  run_evsim timeout "$((DURATION + 10))" incus exec -n -T "$ROGUE" -- \
    /opt/evcharge/venv/bin/python -m evsim \
    --roster /etc/evcharge/roster.yaml --station "$STATION" --chargers cp-1 \
    --csms-url "$EDGE_URL" --pki-dir "$PKI_STOLEN" --metrics-port 0 --duration "$DURATION"
fi

echo
echo "stolen_cert: done (rogue will be deleted on exit)"
