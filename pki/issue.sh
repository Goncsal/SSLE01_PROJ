#!/usr/bin/env bash
# Issue charger / edge certificates.
#   issue.sh <hier_id> [--force]
#   issue.sh --roster PATH [--city X] [--force]
# Prints "<hier_id> <SERIAL>" per line.
set -euo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

usage() { echo "usage: issue.sh <hier_id> [--force] | --roster PATH [--city X] [--force]" >&2; exit 2; }
force=0; roster=""; city=""; ids=()
while [ $# -gt 0 ]; do
  case "$1" in
    --force) force=1 ;;
    --roster) [ $# -ge 2 ] || usage; roster="$2"; shift ;;
    --city) [ $# -ge 2 ] || usage; city="$2"; shift ;;
    -*) usage ;;
    *) ids+=("$1") ;;
  esac
  shift
done
if [ -n "$roster" ]; then
  [ ${#ids[@]} -eq 0 ] && [ -f "$roster" ] || usage
else
  [ ${#ids[@]} -eq 1 ] && [ -z "$city" ] || usage
fi
need_ca

issue_one() {
  local id="$1" s
  parse_id "$id"
  if ca test -f "$CA_DIR/certs/$id.crt" && ca test -f "$CA_DIR/keys/$id.key"; then
    s="$(serial_of "$id")"
    if [ "$force" -eq 0 ] \
       && ca openssl x509 -noout -checkend 86400 -in "$CA_DIR/certs/$id.crt" >/dev/null \
       && [ "$(status_of "$s")" = Valid ]; then
      echo "$id $s"; return 0
    fi
    "$PKI_DIR/revoke.sh" "$id" superseded >&2
  fi
  incus exec --env "SAN=$SAN" --env "KIND=$KIND" --env "ID=$id" --env "CITY=$CITY" --env "ORG=$ORG" \
    "$CA_INST" -- sh -c '
      set -e; cd /root/ca; umask 077
      openssl genpkey -algorithm EC -pkeyopt ec_paramgen_curve:P-256 -out "keys/$ID.key"
      openssl req -new -key "keys/$ID.key" -config openssl.cnf -subj "/O=$ORG/OU=$CITY/CN=$ID" -out "newcerts/$ID.csr"
      openssl ca -batch -config openssl.cnf -extensions "$KIND" -days 397 -rand_serial -notext -preserveDN \
        -in "newcerts/$ID.csr" -out "certs/$ID.crt" 2>/dev/null
      rm -f "newcerts/$ID.csr"; chmod 644 "certs/$ID.crt"' >&2
  echo "$id $(serial_of "$id")"
}

if [ -n "$roster" ]; then
  mapfile -t ids < <(ca python3 -c '
import sys, yaml
r = yaml.safe_load(sys.stdin); c = sys.argv[1]; cc = r.get("country", "pt")
for ct in r["cities"]:
    if c and ct["city"] != c: continue
    p = cc + "." + ct["city"]
    for st in ct["stations"]:
        for ch in st["chargers"]:
            print(p + "." + st["station"] + "." + ch["charger"])
    print(p + ".edge")' "$city" < "$roster")
fi
for id in "${ids[@]}"; do issue_one "$id"; done
