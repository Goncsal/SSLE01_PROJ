#!/usr/bin/env bash
# Revoke the current certificate of <hier_id>, rebuild the CRL, push it to the edges.
set -euo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
[ $# -ge 1 ] && [ $# -le 2 ] || { echo "usage: revoke.sh <hier_id> [reason]" >&2; exit 2; }
id="$1"; reason="${2:-keyCompromise}"
[[ "$reason" =~ ^[A-Za-z]+$ ]] || { echo "bad reason" >&2; exit 2; }
need_ca
parse_id "$id"
ca test -f "$CA_DIR/certs/$id.crt" || { echo "no certificate for $id" >&2; exit 1; }
s="$(serial_of "$id")"
if [ "$(status_of "$s")" = Revoked ]; then
  echo "already revoked: $s" >&2
else
  ca sh -c "cd $CA_DIR && openssl ca -batch -config openssl.cnf -revoke certs/$id.crt -crl_reason '$reason' 2>/dev/null"
fi
"$PKI_DIR/crl.sh" >&2
push="$PKI_DIR/../gateway/push_crl.sh"
if [ -x "$push" ]; then "$push" >&2
else echo "gateway/push_crl.sh not available: update the CRL on the edges by hand" >&2; fi
echo "$s"
