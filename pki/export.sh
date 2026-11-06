#!/usr/bin/env bash
# Copy a leaf identity into an instance, straight from lab-ca (nothing touches the host disk).
#   export.sh <hier_id> <instance> [--dir D] [--group GID] [--crl]
#   export.sh --public        (ca.crt and crl.pem into pki/out/, public material only)
set -euo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

usage() { echo "usage: export.sh <hier_id> <instance> [--dir D] [--group GID] [--crl] | --public" >&2; exit 2; }
if [ "${1:-}" = "--public" ]; then
  need_ca
  mkdir -p "$PKI_DIR/out"
  ca cat "$CA_DIR/ca.crt" > "$PKI_DIR/out/ca.crt"
  ca cat "$CA_DIR/crl.pem" > "$PKI_DIR/out/crl.pem"
  ok "pki/out/ca.crt, pki/out/crl.pem"; exit 0
fi
[ $# -ge 2 ] || usage
id="$1"; inst="$2"; shift 2
dir=/etc/evcharge/pki; group=""; crl=0
while [ $# -gt 0 ]; do
  case "$1" in
    --dir) [ $# -ge 2 ] || usage; dir="$2"; shift ;;
    --group) [ $# -ge 2 ] || usage; group="$2"; shift ;;
    --crl) crl=1 ;;
    *) usage ;;
  esac
  shift
done
[[ "$group" =~ ^[0-9]*$ ]] || usage
[[ "$dir" =~ ^/[A-Za-z0-9._/-]+$ ]] || usage
need_ca
parse_id "$id"
ca test -f "$CA_DIR/keys/$id.key" || { echo "no certificate for $id" >&2; exit 1; }
exists "$inst" || { echo "no instance $inst" >&2; exit 1; }

incus exec "$inst" -- mkdir -p "$dir"
put() { # put <src in lab-ca> <name> <mode>
  ca cat "$1" | incus exec "$inst" -- sh -c 'umask 077; cat > "$1"; chmod "$2" "$1"; chown "0:${3:-0}" "$1"' _ "$dir/$2" "$3" "${group:-0}"
}
put "$CA_DIR/ca.crt" ca.crt 0644
put "$CA_DIR/certs/$id.crt" "$id.crt" 0644
put "$CA_DIR/keys/$id.key" "$id.key" "$([ -n "$group" ] && echo 0640 || echo 0600)"
[ "$crl" -eq 0 ] || put "$CA_DIR/crl.pem" crl.pem 0644
ok "exported $id to $inst:$dir"
