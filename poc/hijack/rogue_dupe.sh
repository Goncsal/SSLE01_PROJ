#!/usr/bin/env bash
# Hijacking PoC 1: a rogue charge point reuses a real charger id against a TLS city
# edge, first with no client certificate, then with a certificate stolen (copied) from
# the real station while it is connected. See DESIGN-hijacking.md §"Duplicate id" and
# docs/interfaces.md v6 5.2 (identity checks 6-10), 12.4 (pki/export.sh), 9.2 (evsim).
#
#   rogue_dupe.sh [--city porto] [--station pt.porto.st-001] [--run] [--duration N]
#
# Default: --dry-run (prints the plan, changes nothing). --run is refused unless
# EVCHARGE_BASELINE_DONE=1 (set by the W6-run card).
set -euo pipefail
# shellcheck source=./lib.sh
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

print_usage() {
  cat <<EOF
usage: $(basename "$0") [--city CITY] [--station HIER_ID] [--run] [--duration SECONDS] [--dry-run]

(a) connects the rogue with only ca.crt (no client cert) -> expect refusal (ident.no_cert).
(b) copies the real station's certificate into the rogue (stolen cert) and connects
    while the real station stays up -> expect ident.ip_mismatch, and charger.duplicate_id
    for whichever of the two the CSMS sees connect second.
EOF
  usage_common
}

parse_common_args "$@"

CHARGER="$STATION.cp-1"
EDGE_URL="$(edge_url "$CITY")"
ROGUE="$(rogue_name "$CITY")"
STATION_INST="$(station_instance "$STATION")"
PKI_NOCERT=/etc/evcharge/pki-nocert
PKI_STOLEN=/etc/evcharge/pki-stolen

echo "== hijacking PoC 1: rogue duplicate id =="
echo "  charger id      : $CHARGER"
echo "  edge url        : $EDGE_URL"
echo "  rogue instance   : $ROGUE  (city network: city-$CITY)"
echo "  real station     : $STATION_INST"
echo "  expected (a)     : ident.no_cert (110105), refused before the OCPP upgrade"
echo "  expected (b)     : ident.ip_mismatch (110104); charger.duplicate_id (110100) for"
echo "                      whichever of the rogue/real connects second; composite"
echo "                      110130 (duplicate id + foreign srcip) or 110131 (reverse order)"
echo "                      if both land within 300 s"
echo "  time-box          : ${DURATION}s per evsim run"
echo

add_cleanup "incus delete -f '$ROGUE' >/dev/null 2>&1 || true"

run_or_plan incus launch "$CITY_IMG" "$ROGUE" --network "city-$CITY"
if [ "$DRY_RUN" = 1 ]; then
  would "wait_ready $ROGUE"
  would "incus file push $ROSTER $ROGUE/etc/evcharge/roster.yaml"
else
  wait_ready "$ROGUE"
  incus file push -q "$ROSTER" "$ROGUE/etc/evcharge/roster.yaml"
fi

echo "-- (a) no certificate --"
if [ "$DRY_RUN" = 1 ]; then
  would "copy ca.crt only from lab-ca into $ROGUE:$PKI_NOCERT (no client cert presented)"
  would "incus exec $ROGUE -- python -m evsim --roster /etc/evcharge/roster.yaml" \
        "--station $STATION --chargers cp-1 --csms-url $EDGE_URL" \
        "--pki-dir $PKI_NOCERT --metrics-port 0 --duration 20"
else
  incus exec "$ROGUE" -- mkdir -p "$PKI_NOCERT"
  incus exec lab-ca -- cat /root/ca/ca.crt \
    | incus exec "$ROGUE" -- sh -c "umask 077; cat > $PKI_NOCERT/ca.crt"
  incus exec "$ROGUE" -- /opt/evcharge/venv/bin/python -m evsim \
    --roster /etc/evcharge/roster.yaml --station "$STATION" --chargers cp-1 \
    --csms-url "$EDGE_URL" --pki-dir "$PKI_NOCERT" --metrics-port 0 --duration 20
fi

echo "-- (b) stolen certificate, real station left running --"
if [ "$DRY_RUN" = 1 ]; then
  would "$ROOT/pki/export.sh $CHARGER $ROGUE --dir $PKI_STOLEN  # stolen cert, straight from lab-ca"
  would "incus exec $ROGUE -- python -m evsim --roster /etc/evcharge/roster.yaml" \
        "--station $STATION --chargers cp-1 --csms-url $EDGE_URL" \
        "--pki-dir $PKI_STOLEN --metrics-port 0 --duration $DURATION"
else
  "$ROOT/pki/export.sh" "$CHARGER" "$ROGUE" --dir "$PKI_STOLEN"
  timeout "$((DURATION + 10))" incus exec "$ROGUE" -- \
    /opt/evcharge/venv/bin/python -m evsim \
    --roster /etc/evcharge/roster.yaml --station "$STATION" --chargers cp-1 \
    --csms-url "$EDGE_URL" --pki-dir "$PKI_STOLEN" --metrics-port 0 --duration "$DURATION"
fi

echo
echo "rogue_dupe: done (rogue will be deleted on exit)"
