#!/usr/bin/env bash
# Read-only evidence check for hijacking PoC 1 (rogue duplicate id / stolen cert). Takes
# the UTC window the operator captured around a live run.sh/rogue_dupe.sh --run (via two
# `date -u +%Y-%m-%dT%H:%M:%SZ` fences) and checks the real signals instead of trusting
# "the script finished" -- see poc/hijack/README.md and docs/interfaces.md v6 5.2, 15.2.
#
# Sources checked:
#   - edge access log (the edge instance's own JSON log): did the rogue even reach it?
#   - CSMS logs, BOTH replicas (app.kubernetes.io/name=csms): ident.no_cert /
#     ident.ip_mismatch / charger.duplicate_id for the charger
#   - registry quarantine state (read-only, needs REGISTRY_ADMIN_TOKEN in the shell)
#   - Wazuh alerts for rule ids 110100/110104/110105/110130/110131 (best-effort; prints
#     "not checked" instead of guessing if wazuh-container isn't reachable)
#
# Prints one of three verdicts: "expected detection observed", "unexpected result", or
# "setup failed" -- distinguishing "never connected" from "connected and detected" from
# "connected but NOT detected". Nothing is created, changed or deleted.
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

# --- 3. registry quarantine state ---
echo "-- registry quarantine --"
if [ -n "${REGISTRY_ADMIN_TOKEN:-}" ]; then
  curl -s -H "Authorization: Bearer $REGISTRY_ADMIN_TOKEN" \
    "http://10.255.255.80:30801/quarantine?prefix=pt.${CITY_ARG}" | sed 's/^/  /'
else
  echo "  not checked (REGISTRY_ADMIN_TOKEN not in this shell; see secrets/platform.env)"
fi
echo

# --- 4. Wazuh alerts, best-effort ---
echo "-- Wazuh alerts (rule ids 110100/110104/110105/110130/110131) --"
if incus info wazuh-container >/dev/null 2>&1; then
  WZ="$(incus exec wazuh-container -- \
    grep -E '"id":"(110100|110104|110105|110130|110131)"' /var/ossec/logs/alerts/alerts.json 2>/dev/null \
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

# --- verdict: distinguish never-connected / connected-and-detected / connected-not-detected ---
SRCIP_COUNT=0
[ -z "$EDGE_SRCIPS" ] || SRCIP_COUNT="$(printf '%s\n' "$EDGE_SRCIPS" | sed '/^$/d' | wc -l)"
DETECTED="$(printf '%s\n' "$CSMS_LINES" | grep -cE '"event":"(ident\.(no_cert|ip_mismatch)|charger\.duplicate_id)"')" || DETECTED=0

echo "== verdict =="
if [ -z "$EDGE_LINES" ]; then
  echo "scenario 1: setup failed -- the rogue never reached the edge (nothing in" \
       "edge.access for $CHARGER in the window at all, not even the real station)." \
       "Check the connectivity probe output from rogue_dupe.sh (DNS/TCP/TLS, rogue vs." \
       "baseline) for where it stopped."
elif [ "$SRCIP_COUNT" -le 1 ] && [ "$DETECTED" -eq 0 ]; then
  echo "scenario 1: setup failed -- only one srcip reached the edge for $CHARGER in the" \
       "window (the real station's own normal traffic); the rogue itself never got this" \
       "far. Check the connectivity probe output from rogue_dupe.sh for where it stopped."
elif [ "$DETECTED" -gt 0 ]; then
  echo "scenario 1: expected detection observed -- CSMS logged an identity-check event" \
       "for $CHARGER in the window (see the CSMS lines above)."
else
  echo "scenario 1: unexpected result -- more than one srcip reached the edge for" \
       "$CHARGER in the window (see the distinct srcips above), so the rogue did" \
       "connect, but no ident.no_cert / ident.ip_mismatch / charger.duplicate_id line" \
       "was found in either CSMS replica's logs for it."
fi
