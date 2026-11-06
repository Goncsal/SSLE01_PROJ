#!/usr/bin/env bash
# Runs every poc/botnet script on its --dry-run path and checks each prints a plan and
# exits 0. Changes nothing (no instance, no sim.env edit, no incus write call). Also
# checks that --run is refused without EVCHARGE_BASELINE_DONE=1.
set -euo pipefail
BOTNET_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

pass=0; fail=0
check() {
  local desc="$1"; shift
  echo "-- $desc --"
  if "$@"; then
    echo "   ok"
    pass=$((pass + 1))
  else
    echo "   FAIL: $desc" >&2
    fail=$((fail + 1))
  fi
}

check "deploy_c2.sh --dry-run" "$BOTNET_DIR/deploy_c2.sh" --dry-run
check "compromise.sh --dry-run" "$BOTNET_DIR/compromise.sh" --dry-run
check "compromise.sh --dry-run --restore" "$BOTNET_DIR/compromise.sh" --dry-run --restore
check "measure_collateral.sh --dry-run --phase before" \
      "$BOTNET_DIR/measure_collateral.sh" --dry-run --phase before
check "measure_collateral.sh --dry-run --phase after" \
      "$BOTNET_DIR/measure_collateral.sh" --dry-run --phase after
check "run.sh --dry-run" "$BOTNET_DIR/run.sh" --dry-run

echo
refusal_out="$(EVCHARGE_BASELINE_DONE= "$BOTNET_DIR/run.sh" --run 2>&1 || true)"
if echo "$refusal_out" | grep -qi baseline; then
  echo "-- --run refused without EVCHARGE_BASELINE_DONE=1 -- ok"
  pass=$((pass + 1))
else
  echo "-- FAIL: --run was not refused without the baseline flag" >&2
  fail=$((fail + 1))
fi

echo
echo "selfcheck: $pass ok, $fail failed"
[ "$fail" -eq 0 ]
