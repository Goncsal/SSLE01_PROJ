#!/usr/bin/env bash
# Hijacking PoC: a rogue charge point in Porto presents a certificate stolen (copied)
# from a real station and connects to the city edge from a foreign address, while the
# genuine station stays up. With station-IP enforcement on (CSMS_ENFORCE_STATION_IP=true,
# the lab default), the CSMS refuses every attempt at the handshake. See
# docs/interfaces.md v6 5.2 (identity checks 6-10), 12.4 (pki/export.sh), 9.2 (evsim).
#
#   rogue_dupe.sh [--city porto] [--station pt.porto.st-001] [--run] [--duration N]
#
# What it shows, end to end:
#   - mTLS proves key possession, not location: the stolen certificate is accepted at the
#     edge (it is validly signed and not yet revoked), so the rogue reaches the CSMS.
#   - the CSMS checks the source address against the registry and refuses: 403,
#     ident.ip_mismatch -> Wazuh 110104 per attempt.
#   - evsim retries, so three refusals from the same source address land inside 60 s ->
#     Wazuh 110132 -> the evcharge-edge-drop active response blocks the rogue's address at
#     the Porto edge. The genuine pt.porto.st-001.cp-1 keeps charging throughout.
#
# Default: --dry-run (prints the plan, changes nothing). --run is refused unless
# EVCHARGE_BASELINE_DONE=1 (set in the environment before the live run).
set -euo pipefail
# shellcheck source=./lib.sh
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

print_usage() {
  cat <<EOF
usage: $(basename "$0") [--city CITY] [--station HIER_ID] [--run] [--duration SECONDS] [--dry-run]

Copies the real station's certificate and key into a throwaway rogue container on the
same city network (straight from lab-ca, pki/export.sh) and connects with it while the
real station stays running. With CSMS_ENFORCE_STATION_IP=true the CSMS refuses each
attempt (403, ident.ip_mismatch); three refusals from the same address within 60 s raise
110132 and the edge drops that address.
EOF
  usage_common
}

parse_common_args "$@"

CHARGER="$STATION.cp-1"
EDGE_URL="$(edge_url "$CITY")"
ROGUE="$(rogue_name "$CITY")"
STATION_INST="$(station_instance "$STATION")"
PKI_STOLEN=/etc/evcharge/pki-stolen
EDGE_HOST="$(edge_host "$CITY")"
if is_plain_city "$CITY"; then EDGE_PORT=80; TLS_FLAG=0; else EDGE_PORT=443; TLS_FLAG=1; fi
# evsim's default --reject-backoff is 60 s, so a rogue refused with 403 would retry only
# once a minute and never put three refusals inside the 110132 window (freq 3 / 60 s). A
# genuinely stolen credential driven by a real attacker would reconnect aggressively, so
# the rogue uses a short backoff here: four refusals land within the first minute and
# 110132 fires -> evcharge-edge-drop. Kept comfortably above a per-request flood so this
# stays the stolen-credential story (110104/110132), not the handshake-flood one (110307).
REJECT_BACKOFF=15

echo "== hijacking PoC: rogue with a stolen certificate from a foreign address =="
echo "  charger id       : $CHARGER"
echo "  edge url         : $EDGE_URL"
echo "  rogue instance   : $ROGUE  (city network: city-$CITY)"
echo "  real station     : $STATION_INST (left running on purpose: the genuine charger"
echo "                      must stay up while the rogue is refused and edge-dropped)"
echo "  precondition     : CSMS_ENFORCE_STATION_IP=true (the lab default; set in the"
echo "                      csms-edge ConfigMap, not by this script)"
echo "  expected per try : ident.ip_mismatch (110104), refused 403 at the handshake"
echo "  expected (3/60s) : stolen credential in use from one address, 3 refusals within"
echo "                      60 s -> 110132 -> evcharge-edge-drop blocks the rogue address"
echo "                      at the $CITY edge (self-expiring edge rule, no session touched)"
echo "  time-box         : ${DURATION}s"
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

echo "-- stolen certificate, real station left running --"
if [ "$DRY_RUN" = 1 ]; then
  would "$ROOT/pki/export.sh $CHARGER $ROGUE --dir $PKI_STOLEN  # stolen cert, straight from lab-ca"
  would "incus exec -n -T $ROGUE -- python -m evsim --roster /etc/evcharge/roster.yaml" \
        "--station $STATION --chargers cp-1 --csms-url $EDGE_URL" \
        "--pki-dir $PKI_STOLEN --metrics-port 0 --reject-backoff $REJECT_BACKOFF" \
        "--duration $DURATION"
else
  "$ROOT/pki/export.sh" "$CHARGER" "$ROGUE" --dir "$PKI_STOLEN"
  # A 403 refusal (ident.ip_mismatch, CSMS_ENFORCE_STATION_IP=true) is the expected
  # outcome of every attempt, not a script failure -- evsim's own retry loop swallows the
  # refusals and still exits 0 once --duration elapses, so the detection outcome is read
  # from the evidence check (verify.sh), never from this exit code (see run_evsim in
  # lib.sh). The retries are what put three refusals from the same address inside 60 s and
  # raise 110132. -n -T: evsim reads no stdin and needs no tty; without them, running
  # under `timeout` inside the operator's own interactive-stdin pipeline sets up an exec
  # control channel that can drop (incusd: "close 1006 (abnormal closure)"), killing evsim
  # outright -- a transport artifact of the invocation shape, not a detection.
  run_evsim timeout "$((DURATION + 10))" incus exec -n -T "$ROGUE" -- \
    /opt/evcharge/venv/bin/python -m evsim \
    --roster /etc/evcharge/roster.yaml --station "$STATION" --chargers cp-1 \
    --csms-url "$EDGE_URL" --pki-dir "$PKI_STOLEN" --metrics-port 0 \
    --reject-backoff "$REJECT_BACKOFF" --duration "$DURATION"
fi

echo
echo "rogue_dupe: done (rogue will be deleted on exit; the genuine station was never touched)"
