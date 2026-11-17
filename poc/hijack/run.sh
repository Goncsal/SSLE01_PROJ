#!/usr/bin/env bash
# Runs the identity-hijacking PoC: one scenario, a rogue in Porto with a stolen
# certificate connecting from a foreign address against station-IP enforcement. Prints a
# banner (what, expected detection, expected response) and relies on the sub-script's own
# cleanup. See README.md for the precondition (CSMS_ENFORCE_STATION_IP=true) and the ACL
# concession the live run needs.
#
#   run.sh [--run] [--duration N]
#
# Default: --dry-run (runs the sub-script with --dry-run too). --run is refused unless
# EVCHARGE_BASELINE_DONE=1 (checked here and again by the sub-script).
set -euo pipefail
# shellcheck source=./lib.sh
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

print_usage() {
  cat <<EOF
usage: $(basename "$0") [--run] [--duration SECONDS] [--dry-run]

Runs rogue_dupe.sh (Porto): a rogue presents a stolen certificate from a foreign address
while the genuine station stays up. This script only prints the banner and sequences it.
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

banner "rogue with a stolen certificate (Porto, TLS city, station-IP enforcement on)" \
       "ident.ip_mismatch (110104) per refusal; 3 refusals same srcip / 60 s -> 110132" \
       "evcharge-edge-drop blocks the rogue address at the Porto edge; genuine charger stays up"
"$HIJACK_DIR/rogue_dupe.sh" --duration "$DURATION" "$MODE_FLAG"

echo
echo "run.sh: scenario done; the rogue was deleted by the sub-script's own cleanup"
