#!/usr/bin/env bash
# Operator side of the quarantine response: certificate revocation is never automatic.
# Reads the pending requests that quarantine-charger appended on the Wazuh manager, shows
# them, asks for confirmation and runs pki/revoke.sh for each; processed request ids are
# kept in lab-ca:/root/ca/processed-requests, so the script is idempotent.
#   process_revocations.sh --list     only list the pending requests
#   process_revocations.sh [--yes]    revoke (asks first, unless --yes)
# REVOKE_CMD overrides the revoke script (tests).
set -euo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

WAZUH_INST="${WAZUH_INSTANCE:-wazuh-container}"
REQUESTS=/var/ossec/logs/evcharge-revocation-requests.json
PROCESSED="$CA_DIR/processed-requests"
REVOKE_CMD="${REVOKE_CMD:-$PKI_DIR/revoke.sh}"

usage() { echo "usage: process_revocations.sh [--list | --yes]" >&2; exit 2; }
mode=ask
case "${1:-}" in
  "") ;;
  --list) mode=list ;;
  --yes) mode=yes ;;
  *) usage ;;
esac
[ $# -le 1 ] || usage
need_ca
exists "$WAZUH_INST" || { echo "$WAZUH_INST missing" >&2; exit 1; }

# request_id TAB hier_id TAB ts TAB alert_id TAB rule_id TAB reason, one line per request
# (parsed inside the manager with its own interpreter; nothing is installed on the host)
read_requests() {
  incus exec "$WAZUH_INST" -- /var/ossec/framework/python/bin/python3 -c '
import json, re, sys
seen = set()
try:
    fh = open(sys.argv[1], encoding="utf-8")
except OSError:
    sys.exit(0)
def f(v):
    return re.sub(r"[^A-Za-z0-9._:@/ -]", "?", str(v))[:120]
for line in fh:
    try:
        r = json.loads(line)
        hid = r["hier_id"]
    except (ValueError, KeyError, TypeError):
        continue
    rid = "%s|%s" % (f(r.get("alert_id", "")), f(hid))
    if rid in seen:
        continue
    seen.add(rid)
    print("\t".join([rid, f(hid), f(r.get("ts", "")), f(r.get("alert_id", "")), f(r.get("rule_id", "")), f(r.get("reason", ""))]))
' "$REQUESTS"
}

mapfile -t all < <(read_requests)
done_ids="$(ca sh -c "cat '$PROCESSED' 2>/dev/null || true")"
pending=()
for row in "${all[@]}"; do
  [ -n "$row" ] || continue
  rid="${row%%$'\t'*}"
  grep -qxF -- "$rid" <<<"$done_ids" || pending+=("$row")
done

if [ ${#pending[@]} -eq 0 ]; then echo "no pending revocation requests"; exit 0; fi
echo "pending revocation requests (${#pending[@]}):"
for row in "${pending[@]}"; do
  IFS=$'\t' read -r rid hid ts aid rule reason <<<"$row"
  printf '  %-26s %-24s rule %-7s alert %s\n      %s\n' "$hid" "$ts" "$rule" "$aid" "$reason"
done
[ "$mode" != list ] || exit 0

if [ "$mode" = ask ]; then
  [ -t 0 ] || { echo "not a terminal: use --yes to confirm" >&2; exit 1; }
  read -r -p "revoke the current certificate of each charger above? [y/N] " answer
  [[ "$answer" =~ ^[Yy]$ ]] || { echo "aborted"; exit 1; }
fi

rc=0
for row in "${pending[@]}"; do
  IFS=$'\t' read -r rid hid _ <<<"$row"
  if ! parse_id "$hid" 2>/dev/null; then echo "skip: bad id $hid" >&2; rc=1; continue; fi
  if serial="$("$REVOKE_CMD" "$hid")"; then
    printf '%s\n' "$rid" | ca sh -c "cat >> '$PROCESSED'"
    ok "$hid revoked (serial $serial)"
  else
    bad "$hid: revoke failed; request stays pending"; rc=1
  fi
done
exit $rc
