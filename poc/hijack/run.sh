#!/usr/bin/env bash
# Orchestrates the three identity-hijacking PoC scenarios in order, with a banner before
# each (what, expected detection, expected response) and a final cleanup. See README.md
# for the before/after mitigation steps the RUN card performs between calls.
#
#   run.sh [--run] [--duration N]
#
# Default: --dry-run (runs every sub-script with --dry-run too). --run is refused unless
# EVCHARGE_BASELINE_DONE=1 (checked here and again by each sub-script).
set -euo pipefail
# shellcheck source=./lib.sh
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

print_usage() {
  cat <<EOF
usage: $(basename "$0") [--run] [--duration SECONDS] [--dry-run]

Runs, in order: rogue_dupe.sh (Porto), stolen_cert.sh --phase before (Porto), mitm_aveiro.sh
(Aveiro). Each prints its own banner and detail; this script only sequences them.
EOF
  usage_common
}

parse_common_args "$@"
MODE_FLAG="--dry-run"
[ "$DRY_RUN" = 1 ] || MODE_FLAG="--run"

banner() {
  echo
  echo "################################################################"
  printf '# %s\n' "$1"
  printf '#   expected detection : %s\n' "$2"
  printf '#   expected response  : %s\n' "$3"
  echo "################################################################"
}

pause_between() {
  if [ "$DRY_RUN" = 1 ]; then
    would "pause 5s between scenarios"
  else
    sleep 5
  fi
}

banner "1/3 rogue duplicate id (Porto, TLS city)" \
       "ident.no_cert (110105); ident.ip_mismatch (110104); charger.duplicate_id (110100)" \
       "composite 110130/110131 -> quarantine-charger (+ edge IP drop only for 110130)"
"$HIJACK_DIR/rogue_dupe.sh" --duration "$DURATION" "$MODE_FLAG"
# Read-only: report (not yet auto-reset) whether scenario 1 left the target quarantined
# or edge-firewalled, so scenario 2's result stays interpretable.
report_target_state "pt.porto.st-001.cp-1" "porto"
pause_between

banner "2/3 stolen certificate from another host (Porto)" \
       "ident.ip_mismatch (110104), before/after CSMS_ENFORCE_STATION_IP" \
       "before: logged only; after: 403 refusal at the handshake"
"$HIJACK_DIR/stolen_cert.sh" --phase before --duration "$DURATION" "$MODE_FLAG"
pause_between

banner "3/3 routed MitM on the plain-ws Aveiro link" \
       "meter.anomaly (110116, plain city)" \
       "quarantine-charger on the affected id; before/after Aveiro -> mTLS"
"$HIJACK_DIR/mitm_aveiro.sh" --duration "$DURATION" "$MODE_FLAG"

echo
echo "run.sh: all three scenarios done; each sub-script cleaned up its own instances"
