#!/usr/bin/env bash
# Install the project's Wazuh manager configuration on the existing manager instance:
# decoder, rules, CDB lists, active-response scripts, the integration stub, the
# credentials files and the marked block of ossec.conf.
#
# Idempotent: unchanged files are not pushed, and the manager is restarted only when
# something changed. The block is checked with wazuh-analysisd -t before the restart;
# on a failed check every file touched in this run is restored.
#
# Secrets come from secrets/platform.env and travel on stdin only.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../../infra/lib.sh
. "$HERE/../../infra/lib.sh"          # sets ROOT, CITY_NET, BRIDGE_CIDR, K3S_NODES, WAZUH_HOST
WAZUH="$ROOT/wazuh"
SECRETS="$ROOT/secrets/platform.env"
M="$WAZUH_HOST"
OSSEC=/var/ossec
EVC=$OSSEC/etc/evcharge
BK="$EVC/backup/$(date +%Y%m%d-%H%M%S)"
CHANGED=0
ROLLBACK=()          # "destination|backup" (backup empty when the file did not exist)

mx() { incus exec "$M" -- "$@"; }
warn() { printf '  \033[33mwarn\033[0m %s\n' "$*" >&2; }

# put DEST MODE OWNER:GROUP < content   (no-op when DEST already has this content)
put() {
  local dst=$1 mode=$2 own=$3 data bk=""
  data="$(cat; printf x)"; data="${data%x}"
  if printf '%s' "$data" | mx sh -c "cmp -s - '$dst'" 2>/dev/null; then return 0; fi
  if mx test -e "$dst"; then
    bk="$BK/$(printf '%s' "$dst" | tr / _)"
    mx sh -c "mkdir -p '$BK' && cp -a '$dst' '$bk'"
  fi
  printf '%s' "$data" | mx sh -c "umask 077; cat > '$dst.new' && chmod $mode '$dst.new' && chown $own '$dst.new' && mv '$dst.new' '$dst'"
  ROLLBACK+=("$dst|$bk")
  CHANGED=1
  ok "installed $dst"
}

rollback() {
  local e dst bk
  for e in "${ROLLBACK[@]}"; do
    dst="${e%%|*}"; bk="${e#*|}"
    if [ -n "$bk" ]; then mx cp -a "$bk" "$dst"; else mx rm -f "$dst"; fi
  done
}

secret() { [ -f "$SECRETS" ] && grep -E "^$1=" "$SECRETS" | head -1 | cut -d= -f2- || true; }

# addresses of an instance on its lab interfaces (not docker0, cni0, flannel)
addrs_of() { incus list "^$1\$" -f csv -c 4 | grep -oE '[0-9.]+ \((eth[0-9]+|enp[0-9a-z]+)\)' | cut -d' ' -f1 || true; }

exists "$M" || { echo "manager instance $M not found" >&2; exit 1; }
running "$M" || { echo "manager instance $M not running" >&2; exit 1; }

# ---- enrolment password: append to platform.env if missing -------------------------
if [ -f "$SECRETS" ] && [ -z "$(secret WAZUH_ENROLL_PASSWORD)" ]; then
  pw="$(LC_ALL=C tr -dc 'A-Za-z0-9' </dev/urandom | head -c 32 || true)"
  printf 'WAZUH_ENROLL_PASSWORD=%s\n' "$pw" >>"$SECRETS"
  ok "WAZUH_ENROLL_PASSWORD added to secrets/platform.env"
fi

log "files on $M"
mx mkdir -p "$EVC" && mx chown root:wazuh "$EVC" && mx chmod 750 "$EVC"

put "$OSSEC/etc/decoders/evcharge_decoders.xml" 660 root:wazuh <"$WAZUH/decoders/evcharge_decoders.xml"
for f in "$WAZUH"/rules/evcharge_*.xml; do
  put "$OSSEC/etc/rules/$(basename "$f")" 660 root:wazuh <"$f"
done
for f in evcharge-plain-cities evcharge-ip-exceptions evcharge-card-allowlist; do
  put "$OSSEC/etc/lists/$f" 660 wazuh:wazuh <"$WAZUH/lists/$f"
done

# text export of the card allow-list for the active response (keys of the CDB list)
put "$EVC/card-allowlist" 640 root:wazuh < <({ grep -vE '^\s*(#|$)' "$WAZUH/lists/evcharge-card-allowlist" || true; } | cut -d: -f1)

# active responses and the integration stub
for s in quarantine-charger block-card evcharge-edge-drop; do
  if [ -f "$WAZUH/active-response/$s" ]; then
    put "$OSSEC/active-response/bin/$s" 750 root:wazuh <"$WAZUH/active-response/$s"
  else
    warn "wazuh/active-response/$s missing, command $s will not run"
  fi
done
put "$OSSEC/integrations/custom-correlator" 750 root:wazuh <"$WAZUH/integrations/custom-correlator"

# files that must exist but are never overwritten
mx sh -c "[ -e '$EVC/quarantine-allowlist' ] || { : > '$EVC/quarantine-allowlist'; chown root:wazuh '$EVC/quarantine-allowlist'; chmod 640 '$EVC/quarantine-allowlist'; }"
mx mkdir -p "$OSSEC/var/run"      # rate-guard state of the active responses
for f in evcharge-ar.json evcharge-revocation-requests.json evcharge-correlator-outbox.json; do
  mx sh -c "[ -e '$OSSEC/logs/$f' ] || { : > '$OSSEC/logs/$f'; chown root:wazuh '$OSSEC/logs/$f'; chmod 660 '$OSSEC/logs/$f'; }"
done

# ---- credentials (stdin only, never printed) ---------------------------------------
log "credentials"
enroll="$(secret WAZUH_ENROLL_PASSWORD)"
if [ -n "$enroll" ]; then
  put "$OSSEC/etc/authd.pass" 640 root:wazuh <<<"$enroll"
else
  warn "WAZUH_ENROLL_PASSWORD missing, authd.pass not written (enrolment would fail)"
fi
unset enroll

put "$EVC/ar.env" 640 root:wazuh < <(
  echo "REGISTRY_URL=http://k3s-master.incus:30801"
  echo "REGISTRY_URL_FALLBACK=http://k3s-node1.incus:30801"
  echo "CSMS_API_URL=http://k3s-master.incus:30800"
  echo "CSMS_API_URL_FALLBACK=http://k3s-node1.incus:30800"
  echo "BILLING_URL=http://k3s-master.incus:30802"
  echo "BILLING_URL_FALLBACK=http://k3s-node1.incus:30802"
  for k in REGISTRY_RESPONDER_TOKEN CSMS_RESPONDER_TOKEN BILLING_RESPONDER_TOKEN CORRELATOR_API_TOKEN; do
    v="$(secret "$k")"
    if [ -n "$v" ]; then echo "$k=$v"; else warn "$k missing in secrets/platform.env, left out of ar.env" >&2; fi
  done
)

# ---- ossec.conf block --------------------------------------------------------------
log "ossec.conf block"
wl=()
for city in "${!CITY_NET[@]}"; do
  wl+=("${CITY_NET[$city]%/*}")
  mapfile -t a < <(addrs_of "$city-edge"); wl+=("${a[@]}")
done
wl+=("${BRIDGE_CIDR%/*}")
for n in $K3S_NODES "$M" lab-ca; do
  mapfile -t a < <(addrs_of "$n"); wl+=("${a[@]}")
done
white=""
while read -r ip; do
  [ -n "$ip" ] && white+="    <white_list>$ip</white_list>"$'\n'
done < <(printf '%s\n' "${wl[@]}" | sort -u -V)
white="${white%$'\n'}"

block="$(awk -v w="$white" '/^@WHITE_LIST@$/ { print w; next } { print }' "$HERE/ossec-evcharge.xml")"
cur="$(mx cat "$OSSEC/etc/ossec.conf")"
stripped="$(printf '%s\n' "$cur" | sed '/<!-- evcharge begin -->/,/<!-- evcharge end -->/d')"
# A second <ruleset> element replaces the lists of the first one (the course-guide rules
# lose theirs), so the ruleset additions (ruleset-lines.xml: the nginx decoder exclusion
# and the three CDB lists) go into the existing <ruleset>, one tagged line each.
stripped="$(printf '%s\n' "$stripped" | { grep -vF '<!-- evcharge -->' || true; })"
tagged="$(sed 's|^|    |; s|$| <!-- evcharge -->|' "$HERE/ruleset-lines.xml")"
stripped="$(printf '%s\n' "$stripped" | awk -v l="$tagged" '{ print } !done && /<decoder_dir>etc\/decoders<\/decoder_dir>/ { print l; done = 1 }')"
# one <auth> only: switch the existing use_password to yes (a second <auth> block works but is confusing)
stripped="$(printf '%s\n' "$stripped" | sed 's|<use_password>no</use_password>|<use_password>yes</use_password> <!-- evcharge-auth -->|')"
new="$stripped"$'\n\n'"$block"
if [ "$new" != "$cur" ]; then
  put "$OSSEC/etc/ossec.conf" 660 root:wazuh <<<"$new"
fi

# ---- check, then restart only if the check passes ----------------------------------
if [ "$CHANGED" -eq 0 ]; then
  ok "nothing changed, manager not restarted"
  exit 0
fi

log "wazuh-analysisd -t"
if ! check_out="$(mx "$OSSEC/bin/wazuh-analysisd" -t 2>&1)"; then
  bad "configuration test failed, restoring the previous files"
  printf '%s\n' "$check_out" | tail -n 8 >&2
  rollback
  exit 1
fi
ok "configuration test passed"

log "restarting wazuh-manager"
mx systemctl restart wazuh-manager
for _ in $(seq 1 30); do
  mx "$OSSEC/bin/wazuh-control" status 2>/dev/null | grep -q '^wazuh-analysisd is running' && break
  sleep 2
done
mx "$OSSEC/bin/wazuh-control" status | grep -E 'analysisd|remoted|execd|integratord|authd'
ok "done; previous versions of changed files are in $BK"
