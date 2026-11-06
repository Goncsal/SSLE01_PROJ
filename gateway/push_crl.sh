#!/usr/bin/env bash
# Copy the current CRL from the CA container to the edge gateways and reload nginx.
# Usage: gateway/push_crl.sh [--instance NAME] [city ...]     (default: every city whose edge exists)
# Plain-mode edges (no ssl_crl in their configuration) are skipped. Exit 1 if any edge fails.
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../infra/env.sh
. "$HERE/../infra/env.sh"

CA_INSTANCE="${CA_INSTANCE:-lab-ca}"
CONF=/etc/nginx/conf.d/evcharge.conf
PKI_DIR=/etc/evcharge/pki

inst_override=""; cities=()
while [ $# -gt 0 ]; do
  case "$1" in
    -h|--help) sed -n '2,4p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
    --instance) inst_override="${2:?--instance needs a value}"; shift ;;
    *) cities+=("$1") ;;
  esac
  shift
done
[ ${#cities[@]} -gt 0 ] || mapfile -t cities < <(printf '%s\n' "${!CITY_NET[@]}" | sort)

crl="$(incus exec "$CA_INSTANCE" -- cat /root/ca/crl.pem)" || { echo "cannot read CRL from $CA_INSTANCE" >&2; exit 1; }
[ -n "$crl" ] || { echo "empty CRL from $CA_INSTANCE" >&2; exit 1; }

rc=0
for city in "${cities[@]}"; do
  inst="${inst_override:-$city-edge}"
  if ! incus info "$inst" >/dev/null 2>&1; then
    if [ ${#cities[@]} -eq 1 ] || [ -n "$inst_override" ]; then echo "$city: instance $inst missing" >&2; rc=1
    else echo "$city: no edge instance, skipped"; fi
    continue
  fi
  if ! incus exec "$inst" -- grep -q '^[[:space:]]*ssl_crl' "$CONF" 2>/dev/null; then
    echo "$city: plain mode, skipped"; continue
  fi
  if printf '%s\n' "$crl" | incus exec "$inst" -- sh -c "cat > $PKI_DIR/crl.pem.new && mv $PKI_DIR/crl.pem.new $PKI_DIR/crl.pem" &&
     incus exec "$inst" -- sh -c 'nginx -t >/dev/null 2>&1 && systemctl reload nginx'; then
    echo "$city: crl pushed, nginx reloaded"
  else
    echo "$city: FAILED" >&2; rc=1
  fi
done
exit $rc
