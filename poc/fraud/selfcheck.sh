#!/usr/bin/env bash
# Runs every poc/fraud script on its --dry-run path and checks each prints a plan and
# exits 0. Changes nothing (no sim.env is read or written, no instance is touched). Also
# checks that --run is refused without EVCHARGE_BASELINE_DONE=1.
set -euo pipefail
FRAUD_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

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

check "clone_card.sh --dry-run" "$FRAUD_DIR/clone_card.sh" --dry-run
check "clone_card.sh --dry-run --phase after" "$FRAUD_DIR/clone_card.sh" --dry-run --phase after
check "tamper_meter.sh --dry-run (scale)" "$FRAUD_DIR/tamper_meter.sh" --dry-run --mode scale
check "tamper_meter.sh --dry-run (rollback)" "$FRAUD_DIR/tamper_meter.sh" --dry-run --mode rollback
check "tamper_meter.sh --dry-run (freeze)" "$FRAUD_DIR/tamper_meter.sh" --dry-run --mode freeze
check "run.sh --dry-run" "$FRAUD_DIR/run.sh" --dry-run

echo
refusal_out="$(EVCHARGE_BASELINE_DONE= "$FRAUD_DIR/run.sh" --run 2>&1 || true)"
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
