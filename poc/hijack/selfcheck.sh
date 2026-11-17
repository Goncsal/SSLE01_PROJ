#!/usr/bin/env bash
# Runs every poc/hijack script on its --dry-run path and checks each prints a plan and
# exits 0. Changes nothing (no instance, network or file is created). Also checks that
# --run is refused without EVCHARGE_BASELINE_DONE=1.
set -euo pipefail
HIJACK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

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

check "rogue_dupe.sh --dry-run" "$HIJACK_DIR/rogue_dupe.sh" --dry-run
check "run.sh --dry-run" "$HIJACK_DIR/run.sh" --dry-run

echo
refusal_out="$(EVCHARGE_BASELINE_DONE= "$HIJACK_DIR/rogue_dupe.sh" --run 2>&1 || true)"
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
