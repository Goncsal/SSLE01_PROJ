#!/usr/bin/env bash
# Create the lab-ca container and the root CA. Idempotent; never replaces an existing CA key.
set -euo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

if ! exists "$CA_INST"; then
  log "creating $CA_INST"
  incus launch "$IMAGE" "$CA_INST" -n "$BRIDGE" \
    -c limits.memory=64MiB -c limits.cpu=1 -c user.hier-id=pt.central.lab-ca
fi
# default for $ENV::SAN so that every openssl command can parse the configuration
incus config set "$CA_INST" environment.SAN=DNS:unset.invalid
running "$CA_INST" || incus start "$CA_INST"
wait_ready "$CA_INST"

if ! ca sh -c 'command -v openssl >/dev/null && python3 -c "import yaml"' >/dev/null 2>&1; then
  log "installing openssl and python3-yaml (apt needs more than 64MiB; limit raised meanwhile)"
  incus config set "$CA_INST" limits.memory=256MiB
  ca sh -c 'DEBIAN_FRONTEND=noninteractive apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends openssl python3-yaml >/dev/null; apt-get clean'
  incus config set "$CA_INST" limits.memory=64MiB
fi

ca sh -c "mkdir -p $CA_DIR/certs $CA_DIR/keys $CA_DIR/newcerts && chmod 700 $CA_DIR $CA_DIR/keys"
incus file push -q "$PKI_DIR/openssl.cnf" "$CA_INST$CA_DIR/openssl.cnf"

if ca test -f "$CA_DIR/ca.key" && ca test -f "$CA_DIR/ca.crt"; then
  ok "CA already present"
else
  log "creating the root CA"
  ca sh -c "set -e; cd $CA_DIR; umask 077
    : > index.txt; echo 'unique_subject = no' > index.txt.attr
    echo 01 > crlnumber
    openssl genpkey -algorithm EC -pkeyopt ec_paramgen_curve:P-256 -out ca.key
    openssl req -new -x509 -key ca.key -sha256 -days 3650 -config openssl.cnf -extensions v3_ca \
      -subj '/O=$ORG/CN=EVCharge Lab Root CA' -out ca.crt
    chmod 600 ca.key; chmod 644 ca.crt"
  ok "CA created"
fi
ca sh -c "cd $CA_DIR; [ -f crl.pem ] || openssl ca -batch -config openssl.cnf -gencrl -out crl.pem 2>/dev/null"
ok "$CA_INST ready"
