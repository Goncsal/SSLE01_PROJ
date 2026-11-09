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
EDGE_HOST="$(edge_host "$CITY")"
if is_plain_city "$CITY"; then EDGE_PORT=80; TLS_FLAG=0; else EDGE_PORT=443; TLS_FLAG=1; fi

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

if [ "$DRY_RUN" = 1 ]; then
  add_cleanup "incus delete -f '$ROGUE' >/dev/null 2>&1 || true"
  would "incus launch $CITY_IMG $ROGUE --network city-$CITY"
  would "wait_ready $ROGUE"
  would "incus file push $ROSTER $ROGUE/etc/evcharge/roster.yaml"
else
  incus launch "$CITY_IMG" "$ROGUE" --network "city-$CITY"
  # Only undo what this run actually created: register the delete now that the launch
  # has succeeded, not before (a launch that fails must never trigger a delete of an
  # instance that was never created, and a dry run must never reach this branch at all).
  add_cleanup "incus delete -f '$ROGUE' >/dev/null 2>&1 || true"
  wait_ready "$ROGUE"
  incus file push -q "$ROSTER" "$ROGUE/etc/evcharge/roster.yaml"
fi

echo "-- connectivity probe: rogue vs. the real station, read-only, before any evsim call --"
if [ "$DRY_RUN" = 1 ]; then
  would "probe_connectivity $ROGUE $EDGE_HOST $EDGE_PORT rogue $TLS_FLAG"
  would "probe_connectivity $STATION_INST $EDGE_HOST $EDGE_PORT $STATION_INST-baseline $TLS_FLAG"
else
  probe_connectivity "$ROGUE" "$EDGE_HOST" "$EDGE_PORT" "rogue" "$TLS_FLAG"
  probe_connectivity "$STATION_INST" "$EDGE_HOST" "$EDGE_PORT" "$STATION_INST baseline" "$TLS_FLAG"
fi
echo

echo "-- (a) no certificate --"
if [ "$DRY_RUN" = 1 ]; then
  would "copy ca.crt only from lab-ca into $ROGUE:$PKI_NOCERT (no client cert presented)"
  would "incus exec -n -T $ROGUE -- python -m evsim --roster /etc/evcharge/roster.yaml" \
        "--station $STATION --chargers cp-1 --csms-url $EDGE_URL" \
        "--pki-dir $PKI_NOCERT --metrics-port 0 --duration 20"
else
  incus exec "$ROGUE" -- mkdir -p "$PKI_NOCERT"
  incus exec lab-ca -- cat /root/ca/ca.crt \
    | incus exec "$ROGUE" -- sh -c "umask 077; cat > $PKI_NOCERT/ca.crt"
  # A refusal (ident.no_cert) is the expected outcome, so a non-zero evsim exit here must
  # not abort the script under set -e -- see run_evsim in lib.sh. time-boxed defensively
  # even though --duration already bounds it, same as part (b) below. -n -T: evsim reads
  # no stdin and needs no tty; without them, running under `timeout` inside the
  # operator's own interactive-stdin pipeline sets up an exec control channel that can
  # drop (incusd: "close 1006 (abnormal closure)"), which kills evsim outright -- not a
  # detection, not evsim's own fault, a pure transport artifact of the invocation shape.
  run_evsim timeout 30 incus exec -n -T "$ROGUE" -- /opt/evcharge/venv/bin/python -m evsim \
    --roster /etc/evcharge/roster.yaml --station "$STATION" --chargers cp-1 \
    --csms-url "$EDGE_URL" --pki-dir "$PKI_NOCERT" --metrics-port 0 --duration 20
fi

echo "-- (b) stolen certificate, real station left running --"
if [ "$DRY_RUN" = 1 ]; then
  would "$ROOT/pki/export.sh $CHARGER $ROGUE --dir $PKI_STOLEN  # stolen cert, straight from lab-ca"
  would "incus exec -n -T $ROGUE -- python -m evsim --roster /etc/evcharge/roster.yaml" \
        "--station $STATION --chargers cp-1 --csms-url $EDGE_URL" \
        "--pki-dir $PKI_STOLEN --metrics-port 0 --duration $DURATION"
else
  "$ROOT/pki/export.sh" "$CHARGER" "$ROGUE" --dir "$PKI_STOLEN"
  # ident.ip_mismatch / charger.duplicate_id (or the connection simply being refused/
  # dropped) are expected outcomes here, not script failures -- see run_evsim in lib.sh.
  # This is the call whose non-zero exit used to abort run.sh before scenarios 2/3 ran.
  # -n -T: see the comment on part (a)'s evsim call above -- same control-channel risk.
  run_evsim timeout "$((DURATION + 10))" incus exec -n -T "$ROGUE" -- \
    /opt/evcharge/venv/bin/python -m evsim \
    --roster /etc/evcharge/roster.yaml --station "$STATION" --chargers cp-1 \
    --csms-url "$EDGE_URL" --pki-dir "$PKI_STOLEN" --metrics-port 0 --duration "$DURATION"
fi

echo
echo "rogue_dupe: done (rogue will be deleted on exit)"
