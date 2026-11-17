#!/usr/bin/env bash
# Read-only evidence check for the hijacking PoC (rogue with a stolen certificate from a
# foreign address). Takes the UTC window the operator captured around a live
# run.sh/rogue_dupe.sh --run (via two `date -u +%Y-%m-%dT%H:%M:%SZ` fences) and checks the
# real signals instead of trusting "the script finished" -- see poc/hijack/README.md and
# docs/interfaces.md v6 5.2, 15.2.
#
# Sources checked:
#   - edge access log (the edge instance's own JSON log): did the rogue even reach it?
#   - CSMS logs, BOTH replicas (app.kubernetes.io/name=csms): ident.ip_mismatch for the
#     charger (403 refusals, the 110104 source)
#   - the evcharge-edge-drop response: a DROP rule for the rogue's address at the city edge
#     (the 110132 active response), plus the genuine station still connected
#   - Wazuh alerts for rule ids 110104/110132 (best-effort; prints "not checked" instead
#     of guessing if wazuh-container isn't reachable)
#
# Prints one of three verdicts: "expected detection observed", "unexpected result", or
# "setup failed" -- distinguishing "never connected" from "connected and refused" from
# "connected but NOT refused". Nothing is created, changed or deleted.
#
#   verify.sh <start_iso> <end_iso> [--charger HIER_ID] [--city CITY]
#   start_iso/end_iso: UTC, e.g. 2026-11-07T09:40:00Z (date -u +%Y-%m-%dT%H:%M:%SZ)
set -euo pipefail
HIJACK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=./lib.sh
. "$HIJACK_DIR/lib.sh"

usage() {
  echo "usage: $(basename "$0") <start_iso> <end_iso> [--charger HIER_ID] [--city CITY]" >&2
  echo "  start_iso/end_iso: UTC, e.g. 2026-11-07T09:40:00Z (date -u +%Y-%m-%dT%H:%M:%SZ)" >&2
  exit 2
}
[ $# -ge 2 ] || usage
START="$1"; END="$2"; shift 2
CHARGER="pt.porto.st-001.cp-1"
CITY_ARG="porto"
while [ $# -gt 0 ]; do
  case "$1" in
    --charger) CHARGER="$2"; shift 2 ;;
    --city) CITY_ARG="$2"; shift 2 ;;
    *) usage ;;
  esac
done
EDGE_INST="${CITY_ARG}-edge"
# Only the first 19 chars (YYYY-MM-DDTHH:MM:SS) are compared, since the edge/CSMS log
# timestamps carry a "+00:00" offset instead of "Z" -- this is still a correct
# lexicographic range check for same-length, zero-padded ISO-ish prefixes.
START19="${START:0:19}"
END19="${END:0:19}"

echo "== hijacking PoC 1 evidence check =="
echo "  window  : $START -> $END"
echo "  charger : $CHARGER"
echo "  edge    : $EDGE_INST"
echo

# --- 1. edge access log: did the rogue even reach the edge? ---
echo "-- edge log ($EDGE_INST) --"
EDGE_RAW="$(incus exec "$EDGE_INST" -- \
  grep -F "$CHARGER" "/var/log/evcharge/edge-pt.${CITY_ARG}.edge.json" 2>/dev/null)" || EDGE_RAW=""
EDGE_LINES="$(printf '%s\n' "$EDGE_RAW" | awk -v s="$START19" -v e="$END19" -F'"ts":"' '
  NF > 1 { split($2, a, "\""); t = substr(a[1], 1, 19); if (t >= s && t <= e) print }
')"
# The charger id alone doesn't distinguish the rogue from the real station's own normal
# traffic (both use the same hier_id) -- more than one distinct srcip for the same
# hier_id in the window is what actually shows a second, foreign connection attempt.
EDGE_SRCIPS=""
if [ -n "$EDGE_LINES" ]; then
  echo "$EDGE_LINES"
  EDGE_SRCIPS="$(printf '%s\n' "$EDGE_LINES" | grep -oE '"srcip":"[^"]*"' | sort -u)"
  echo "  distinct srcips seen: $(printf '%s\n' "$EDGE_SRCIPS" | wc -l)"
  echo "$EDGE_SRCIPS" | sed 's/^/    /'
else
  echo "  (no edge.access line for $CHARGER in the window)"
fi
echo

# --- 2. CSMS logs, both replicas ---
echo "-- CSMS logs (both replicas, app.kubernetes.io/name=csms) --"
PODS="$(incus exec k3s-master -- kubectl -n evcharge get pods -l app.kubernetes.io/name=csms -o name 2>/dev/null)" || PODS=""
CSMS_LINES=""
if [ -n "$PODS" ]; then
  # A `while read` loop here would be wrong: `incus exec` below reads its own stdin by
  # default and would silently consume the rest of the pod list meant for the next
  # `read`, after only the first pod. mapfile + for avoids sharing stdin with the loop.
  mapfile -t POD_ARR <<<"$PODS"
  for pod in "${POD_ARR[@]}"; do
    [ -n "$pod" ] || continue
    echo "  pod: $pod"
    out="$(incus exec k3s-master -- kubectl -n evcharge logs "$pod" --since=30m </dev/null 2>/dev/null \
      | grep -F "$CHARGER")" || out=""
    if [ -n "$out" ]; then
      echo "$out" | sed 's/^/    /'
      CSMS_LINES="${CSMS_LINES}${out}
"
    else
      echo "    (no lines for $CHARGER)"
    fi
  done
else
  echo "  (could not list CSMS pods -- is k3s-master reachable?)"
fi
echo

# --- 3. evcharge-edge-drop response: a DROP rule for the rogue's address at the edge ---
echo "-- edge-drop response ($EDGE_INST iptables) --"
DROP_OUT=""
if [ -n "$EDGE_SRCIPS" ]; then
  while IFS= read -r line; do
    ip="$(printf '%s' "$line" | grep -oE '[0-9]+(\.[0-9]+){3}')" || ip=""
    [ -n "$ip" ] || continue
    found="$(incus exec "$EDGE_INST" -- sh -c "iptables -L -n 2>/dev/null | grep -F '$ip'")" || found=""
    if [ -n "$found" ]; then
      echo "  DROP rule present for rogue address $ip:"
      echo "$found" | sed 's/^/    /'
      DROP_OUT="$found"
    fi
  done <<<"$EDGE_SRCIPS"
fi
[ -n "$DROP_OUT" ] || echo "  (no DROP rule found for any rogue address seen above; the" \
  "edge-drop may have expired, or 110132 did not reach 3 refusals in 60 s)"
echo

# --- 4. Wazuh alerts, best-effort ---
echo "-- Wazuh alerts (rule ids 110104/110132) --"
if incus info wazuh-container >/dev/null 2>&1; then
  WZ="$(incus exec wazuh-container -- \
    grep -E '"id":"(110104|110132)"' /var/ossec/logs/alerts/alerts.json 2>/dev/null \
    | grep -F "$CHARGER")" || WZ=""
  if [ -n "$WZ" ]; then
    echo "$WZ" | sed 's/^/  /'
  else
    echo "  (no matching alert lines found for $CHARGER; check timestamps against the window by hand)"
  fi
else
  echo "  not checked (wazuh-container not reachable from here)"
fi
echo

# --- verdict: distinguish never-connected / connected-and-refused / connected-not-refused ---
SRCIP_COUNT=0
[ -z "$EDGE_SRCIPS" ] || SRCIP_COUNT="$(printf '%s\n' "$EDGE_SRCIPS" | sed '/^$/d' | wc -l)"
DETECTED="$(printf '%s\n' "$CSMS_LINES" | grep -cE '"event":"ident\.ip_mismatch"')" || DETECTED=0

echo "== verdict =="
if [ -z "$EDGE_LINES" ]; then
  echo "setup failed -- the rogue never reached the edge (nothing in edge.access for" \
       "$CHARGER in the window at all, not even the real station). Check the connectivity" \
       "probe output from rogue_dupe.sh (DNS/TCP/TLS, rogue vs. baseline) for where it" \
       "stopped; with the city ACLs applied the rogue's address is dropped before the edge."
elif [ "$SRCIP_COUNT" -le 1 ] && [ "$DETECTED" -eq 0 ]; then
  echo "setup failed -- only one srcip reached the edge for $CHARGER in the window (the" \
       "real station's own normal traffic); the rogue itself never got this far. Check the" \
       "connectivity probe output from rogue_dupe.sh for where it stopped."
elif [ "$DETECTED" -gt 0 ]; then
  echo "expected detection observed -- CSMS logged $DETECTED ident.ip_mismatch refusal(s)" \
       "for $CHARGER in the window (see the CSMS lines above).$([ -n "$DROP_OUT" ] && \
       printf '%s' ' The edge-drop response is present (110132 fired).')"
else
  echo "unexpected result -- more than one srcip reached the edge for $CHARGER in the" \
       "window (see the distinct srcips above), so the rogue did connect, but no" \
       "ident.ip_mismatch line was found in either CSMS replica's logs for it (is" \
       "CSMS_ENFORCE_STATION_IP=true?)."
fi
