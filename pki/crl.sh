#!/usr/bin/env bash
# Regenerate crl.pem (valid 7 days).
set -euo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
need_ca
ca sh -c "cd $CA_DIR && openssl ca -batch -config openssl.cnf -gencrl -out crl.pem 2>/dev/null"
ok "CRL regenerated"
