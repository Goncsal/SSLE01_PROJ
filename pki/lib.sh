# Shared helpers for the pki scripts. Only `incus` is needed on the host.
PKI_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../infra/lib.sh
. "$PKI_DIR/../infra/lib.sh"

CA_INST="lab-ca"
CA_DIR="/root/ca"
ORG="EVCharge Lab"

# run a command inside the CA container
ca() { incus exec "$CA_INST" -- "$@"; }

need_ca() {
  exists "$CA_INST" || { echo "lab-ca missing: run pki/ca.sh" >&2; return 1; }
  ca test -f "$CA_DIR/ca.crt" || { echo "CA not initialised: run pki/ca.sh" >&2; return 1; }
}

# parse_id <hier_id>: sets KIND (charger|edge), CITY, SAN
parse_id() {
  local id="$1"
  if [[ "$id" =~ ^pt\.([a-z][a-z0-9]*)\.(st-[0-9]{3})\.(cp-[0-9]+)$ ]]; then
    KIND=charger; CITY="${BASH_REMATCH[1]}"
    SAN="DNS:${BASH_REMATCH[3]}.${BASH_REMATCH[2]}.$CITY.pt.lab"
  elif [[ "$id" =~ ^pt\.([a-z][a-z0-9]*)\.edge$ ]]; then
    KIND=edge; CITY="${BASH_REMATCH[1]}"
    SAN="DNS:edge.$CITY.pt.lab,DNS:$CITY-edge.$CITY.pt.lab"
  else
    echo "not a charger or edge hier-id: $id" >&2; return 1
  fi
}

# normalised serial of the current certificate of <hier_id>
serial_of() {
  ca openssl x509 -noout -serial -in "$CA_DIR/certs/$1.crt" \
    | sed -e 's/^serial=//' -e 's/://g' | tr 'a-f' 'A-F' | sed -e 's/^0*//'
}

# status of a normalised serial in the CA database: Valid / Revoked / unknown
# (the database stores an even number of hex digits, so odd-length serials get a leading zero)
status_of() {
  local s="$1"
  [ $(( ${#s} % 2 )) -eq 0 ] || s="0$s"
  ca sh -c "cd $CA_DIR && openssl ca -config openssl.cnf -status '$s' 2>&1" \
    | grep -oE '(Valid|Revoked)' | head -1 || true
}
