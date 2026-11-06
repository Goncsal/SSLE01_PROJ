#!/usr/bin/env bash
# Replay the test cases through wazuh-logtest on the manager and compare rule ids.
# usage: run_logtest.sh [block ...]      (default: base hijack fraud botnet infra)
# The rules must be installed first (wazuh/manager/deploy_manager.sh).
# Exit status 1 when any case differs. Nothing is left on the manager afterwards.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../../infra/lib.sh
. "$HERE/../../infra/lib.sh"
M="$WAZUH_HOST"
DIR=/tmp/evcharge-tests

blocks=("$@")
[ "${#blocks[@]}" -gt 0 ] || blocks=(base hijack fraud botnet infra)

exists "$M" && running "$M" || { echo "manager instance $M not running" >&2; exit 1; }

incus exec "$M" -- rm -rf "$DIR"
incus exec "$M" -- mkdir -p "$DIR"
trap 'incus exec "$M" -- rm -rf "$DIR" >/dev/null 2>&1 || true' EXIT
incus file push "$HERE/logtest_driver.py" "$M$DIR/logtest_driver.py"
for b in "${blocks[@]}"; do
  incus file push "$HERE/$b.jsonl" "$HERE/$b.expect" "$M$DIR/"
done

status=0
incus exec "$M" -- /var/ossec/framework/python/bin/python3 "$DIR/logtest_driver.py" "$DIR" "${blocks[@]}" || status=$?
exit "$status"
