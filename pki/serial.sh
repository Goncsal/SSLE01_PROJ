#!/usr/bin/env bash
# Print the serial of the current certificate of <hier_id>.
set -euo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
[ $# -eq 1 ] || { echo "usage: serial.sh <hier_id>" >&2; exit 2; }
need_ca
parse_id "$1"
ca test -f "$CA_DIR/certs/$1.crt" || { echo "no certificate for $1" >&2; exit 1; }
serial_of "$1"
