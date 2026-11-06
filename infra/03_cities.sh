#!/usr/bin/env bash
# City sites: networks, edge gateways and station containers (contract section 13).
#
#   infra/03_cities.sh [step] [city ...]
#   steps: all image update-sim networks instances certs edges stations register csms monitoring status
#
# `all` runs image, networks, instances, certs, edges, stations, register, csms, monitoring in that order.
# Every step can be repeated. REBUILD=1 rebuilds the image. MIN_AVAIL_MB=2500 stops the
# script when the host's available memory drops below that value (0 = no check).
# Only `incus` and bash are used on the host. Secrets go through stdin, never argv.
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

CITY_IMG=evcharge-city
BUILD_INST=evcharge-city-build
ROSTER="${ROSTER:-$ROOT/dev/roster.yaml}"
SECRETS="${SECRETS:-$ROOT/secrets/platform.env}"
CITYDIR="$ROOT/infra/city"
VENV=/opt/evcharge/venv
PY="$VENV/bin/python"
GID_EV=10001
ST_MEM="${ST_MEM:-224MiB}"
EDGE_MEM="${EDGE_MEM:-384MiB}"
MIN_AVAIL_MB="${MIN_AVAIL_MB:-0}"
REG_PORT=30801
API_PORT=30800

# ---- helpers ------------------------------------------------------------------------
guard_mem() {
  [ "$MIN_AVAIL_MB" -gt 0 ] || return 0
  local kb; kb=$(awk '/^MemAvailable:/ {print $2}' /proc/meminfo)
  if [ "$((kb / 1024))" -lt "$MIN_AVAIL_MB" ]; then
    echo "host available memory $((kb / 1024)) MiB < MIN_AVAIL_MB=$MIN_AVAIL_MB, stopping" >&2
    exit 3
  fi
}

# first/second part of the city's gateway address, host .2 for the edge (derived, not typed)
edge_addr() { local gw="${CITY_NET[$1]%/*}"; echo "${gw%.*}.2"; }

stations_of() { local i; for i in $(seq 1 "$STATIONS_PER_CITY"); do printf 'st-%03d\n' "$i"; done; }

# secret value from the secrets file (not sourced: it may hold characters the shell would expand)
secret() { grep -m1 "^$1=" "$SECRETS" | cut -d= -f2-; }

# run a command inside an instance
ix() { local i="$1"; shift; incus exec "$i" -- "$@"; }

# roster query inside the city's edge (the image venv has evcommon)
rq() { ix "$1-edge" "$PY" /opt/evcharge/register_city.py list --roster /etc/evcharge/roster.yaml --city "$1"; }

push_tools() { # roster and registration helper into an instance
  incus file push -q "$ROSTER" "$1/etc/evcharge/roster.yaml"
  incus file push -q "$CITYDIR/register_city.py" "$1/opt/evcharge/register_city.py"
}

# push stdin to a file only when the content differs; returns 0 if it changed
put_if_changed() { # <instance> <path> <mode>
  local inst="$1" path="$2" mode="$3" tmp new old
  tmp="$(mktemp)"; cat > "$tmp"
  new=$(sha256sum < "$tmp")
  old=$(ix "$inst" sh -c "[ -f '$path' ] && sha256sum < '$path' || true")
  if [ "$new" != "$old" ]; then
    incus file push -q --mode "$mode" "$tmp" "$inst$path"; rm -f "$tmp"; return 0
  fi
  rm -f "$tmp"; return 1
}

# ---- image --------------------------------------------------------------------------
src_tar() { tar -C "$ROOT" --exclude='__pycache__' --exclude='*.egg-info' --exclude=tests \
              --exclude='.pytest_cache' --exclude='*.pyc' -cf - common sim constraints.txt; }

install_sim() { # <instance> [pip extra args]: install the sim and common code from the repo tree
  local inst="$1"; shift
  ix "$inst" rm -rf /tmp/src
  ix "$inst" mkdir -p /tmp/src
  src_tar | ix "$inst" tar -x -C /tmp/src
  ix "$inst" sh -c "cd /tmp/src && $VENV/bin/pip install -q --no-cache-dir -c constraints.txt $* ./common ./sim"
  ix "$inst" rm -rf /tmp/src
}

step_image() {
  if incus image info "$CITY_IMG" >/dev/null 2>&1 && [ "${REBUILD:-0}" != 1 ]; then
    ok "image $CITY_IMG exists (REBUILD=1 rebuilds)"; return
  fi
  log "building image $CITY_IMG"
  incus image alias delete "$CITY_IMG" 2>/dev/null || true
  incus delete -f "$BUILD_INST" 2>/dev/null || true
  guard_mem
  incus launch "$IMAGE" "$BUILD_INST" -p default
  wait_ready "$BUILD_INST"
  ix "$BUILD_INST" sh -c 'export DEBIAN_FRONTEND=noninteractive
    apt-get update -qq
    apt-get install -y -qq --no-install-recommends python3 python3-venv ca-certificates \
      curl nginx prometheus-nginx-exporter prometheus-node-exporter >/dev/null
    systemctl disable --now nginx prometheus-nginx-exporter prometheus-node-exporter >/dev/null 2>&1 || true
    python3 -m venv '"$VENV"'
    groupadd -g '"$GID_EV"' evcharge
    useradd -r -u '"$GID_EV"' -g evcharge -M -d /nonexistent -s /usr/sbin/nologin evcharge
    install -d -m 0755 -o root -g root /etc/evcharge /opt/evcharge
    install -d -m 0750 -o root -g evcharge /etc/evcharge/pki
    install -d -m 0755 -o evcharge -g evcharge /var/log/evcharge'
  install_sim "$BUILD_INST"
  ix "$BUILD_INST" "$PY" -c 'import evsim, evcommon; print("evsim import ok")'
  ix "$BUILD_INST" sh -c 'apt-get clean; rm -rf /var/lib/apt/lists/* /root/.cache'
  incus stop "$BUILD_INST"
  incus publish "$BUILD_INST" --alias "$CITY_IMG" >/dev/null
  incus delete "$BUILD_INST"
  ok "image $CITY_IMG published"
}

# push new sim/common code into the running stations and restart evsim
step_update_sim() {
  local city st inst
  for city in "$@"; do
    for st in $(stations_of "$city"); do
      inst="$city-$st"; running "$inst" || continue
      log "update-sim $inst"
      install_sim "$inst" --force-reinstall --no-deps
      ix "$inst" systemctl restart evsim
    done
  done
}

# ---- networks ------------------------------------------------------------------------
step_networks() { "$ROOT/infra/02_networks.sh"; }

# ---- instances -----------------------------------------------------------------------
launch_one() { # <name> <network> <mem> [static address]
  local name="$1" net="$2" mem="$3" addr="${4:-}"
  if ! exists "$name"; then
    guard_mem
    log "creating $name ($mem)"
    incus init "$CITY_IMG" "$name" -p default -n "$net" -c limits.memory="$mem" -c limits.cpu=1
    if [ -n "$addr" ]; then
      incus config device set "$name" eth0 ipv4.address="$addr" >/dev/null
    fi
  fi
  running "$name" || incus start "$name"
  wait_ready "$name"
}

step_instances() {
  local city st lat lon edge kind net a b
  for city in "$@"; do
    net="city-$city"
    incus network show "$net" >/dev/null 2>&1 || { echo "network $net missing, run: networks" >&2; return 1; }
    edge="$city-edge"
    launch_one "$edge" "$net" "$EDGE_MEM" "$(edge_addr "$city")"
    push_tools "$edge"
    incus config set "$edge" user.hier-id="pt.$city.edge" user.role=edge
    while IFS=$'\t' read -r kind a b _ _ <&3; do
      if [ "$kind" = edge ]; then
        incus config set "$edge" user.location.lat="$a" user.location.lon="$b"
      fi
    done 3< <(rq "$city")
    while IFS=$'\t' read -r kind st lat lon _ <&3; do
      [ "$kind" = station ] || continue
      # the sites hold the stations the networks step has aliases for
      launch_one "$city-$st" "$net" "$ST_MEM"
      incus config set "$city-$st" user.hier-id="pt.$city.$st" user.role=station \
        user.location.lat="$lat" user.location.lon="$lon" user.location.city="$city"
      push_tools "$city-$st"
    done 3< <(rq "$city")
    ok "$city: edge $(ipof "$edge") and stations up"
  done
}

# ---- certs ---------------------------------------------------------------------------
step_certs() {
  local city st cp
  "$ROOT/pki/ca.sh" >/dev/null
  "$ROOT/pki/issue.sh" --roster "$ROSTER" >/dev/null
  for city in "$@"; do
    while IFS=$'\t' read -r kind st _ _ cps <&3; do
      [ "$kind" = station ] || continue
      for cp in ${cps//,/ }; do
        "$ROOT/pki/export.sh" "pt.$city.$st.$cp" "$city-$st" --group "$GID_EV" >/dev/null
      done
    done 3< <(rq "$city")
    ok "$city: charger certificates exported"
  done
}

# ---- edges ---------------------------------------------------------------------------
is_plain() { local c; for c in $PLAIN_WS_CITIES; do [ "$c" = "$1" ] && return 0; done; return 1; }

step_edges() {
  local city mode
  for city in "$@"; do
    mode=--tls; is_plain "$city" && mode=--plain
    "$ROOT/gateway/deploy_edge.sh" "$city" "$mode"
  done
}

# ---- stations ------------------------------------------------------------------------
step_stations() {
  local city st inst url changed
  for city in "$@"; do
    if is_plain "$city"; then url="ws://edge.$city.pt.lab/ocpp"; else url="wss://edge.$city.pt.lab/ocpp"; fi
    for st in $(stations_of "$city"); do
      inst="$city-$st"
      push_tools "$inst"
      changed=0
      # sim.env is created once; later edits (SIM_EXTRA_ARGS, URL) are the operator's
      if ! ix "$inst" test -f /etc/evcharge/sim.env; then
        sed "s|@URL@|$url|" "$CITYDIR/sim.env.tmpl" | put_if_changed "$inst" /etc/evcharge/sim.env 0644 || true
        changed=1
      fi
      sed "s|@STATION@|pt.$city.$st|" "$CITYDIR/evsim.service" \
        | put_if_changed "$inst" /etc/systemd/system/evsim.service 0644 && changed=1
      if [ "$changed" = 1 ]; then ix "$inst" systemctl daemon-reload; fi
      ix "$inst" systemctl enable evsim >/dev/null 2>&1
      if [ "$changed" = 1 ] || ! ix "$inst" systemctl is-active --quiet evsim; then
        ix "$inst" systemctl restart evsim
      fi
    done
    ok "$city: evsim enabled and running on $(stations_of "$city" | wc -l) stations ($url)"
  done
}

# ---- register ------------------------------------------------------------------------
step_register() {
  local city st inst admin tok stations serials edge_serial payload cp kind cps
  admin=$(secret REGISTRY_ADMIN_TOKEN)
  [ -n "$admin" ] || { echo "REGISTRY_ADMIN_TOKEN missing in $SECRETS" >&2; return 1; }
  for city in "$@"; do
    tok=$(secret "REGISTRY_TOKEN_$(echo "$city" | tr '[:lower:]' '[:upper:]')")
    [ -n "$tok" ] || { echo "REGISTRY_TOKEN_${city^^} missing in $SECRETS" >&2; return 1; }
    # NOC creates the city node (admin token on stdin to curl -H @-)
    printf 'Authorization: Bearer %s\n' "$admin" \
      | ix k3s-master curl -sS -o /dev/null -w "pt.$city: HTTP %{http_code}\n" -X PUT -H @- \
          -H 'content-type: application/json' -d "{\"name\":\"${city^}\"}" \
          "http://127.0.0.1:$REG_PORT/nodes/pt.$city"
    stations=""; serials=""
    while IFS=$'\t' read -r kind st _ _ cps <&3; do
      [ "$kind" = station ] || continue
      inst="$city-$st"
      stations+="${stations:+,}\"$st\":\"$(ipof "$inst")\""
      for cp in ${cps//,/ }; do
        serials+="${serials:+,}\"pt.$city.$st.$cp\":\"$("$ROOT/pki/serial.sh" "pt.$city.$st.$cp")\""
      done
    done 3< <(rq "$city")
    edge_serial=$("$ROOT/pki/serial.sh" "pt.$city.edge")
    payload="{\"token\":\"$tok\",\"edge\":{\"ip\":\"$(ipof "$city-edge")\",\"cert_serial\":\"$edge_serial\"},\"stations\":{$stations},\"serials\":{$serials}}"
    # the city subtree is written from the edge with the city's own token (delegation)
    printf '%s' "$payload" | ix "$city-edge" "$PY" /opt/evcharge/register_city.py register \
      --roster /etc/evcharge/roster.yaml --city "$city" \
      $(for n in $K3S_NODES; do printf -- '--registry http://%s.incus:%s ' "$n" "$REG_PORT"; done)
  done
}

# ---- csms ----------------------------------------------------------------------------
step_csms() {
  local city proxies="" cur want="false"
  for city in $(printf '%s\n' "${!CITY_NET[@]}" | sort); do
    proxies+="${proxies:+,}$(ipof "$city-edge")"
  done
  cur=$(kc -n evcharge get configmap csms-edge -o jsonpath='{.data.CSMS_TRUSTED_PROXIES}|{.data.CSMS_ALLOW_DIRECT}' 2>/dev/null || true)
  if [ "$cur" = "$proxies|$want" ]; then
    ok "csms-edge already set (proxies=$proxies, allow_direct=$want)"; return
  fi
  kc -n evcharge create configmap csms-edge --from-literal="CSMS_TRUSTED_PROXIES=$proxies" \
    --from-literal="CSMS_ALLOW_DIRECT=$want" --dry-run=client -o yaml | kc apply -f - >/dev/null
  kc -n evcharge rollout restart deployment/csms >/dev/null
  kc -n evcharge rollout status deployment/csms --timeout=180s
  ok "csms-edge written (proxies=$proxies, allow_direct=$want), csms restarted"
}

# ---- monitoring -----------------------------------------------------------------------
step_monitoring() {
  local city
  for city in "$@"; do "$ROOT/monitoring/city/deploy_city_prom.sh" "$city"; done
}

# ---- status --------------------------------------------------------------------------
step_status() {
  local city st inst api n
  api=$(secret CSMS_API_TOKEN)
  for city in "$@"; do
    log "$city"
    for inst in "$city-edge" $(for st in $(stations_of "$city"); do echo "$city-$st"; done); do
      if ! exists "$inst"; then bad "$inst missing"; continue; fi
      printf '  %-14s %-8s %-16s mem %s' "$inst" "$(incus list "^$inst\$" -f csv -c s)" "$(ipof "$inst")" \
        "$(incus list "^$inst\$" -f csv -c m)"
      case "$inst" in
        *-edge) printf '  nginx -t: %s\n' "$(ix "$inst" nginx -t 2>&1 | tail -1 | sed 's/^nginx: //')" ;;
        *) printf '  evsim: %s\n' "$(ix "$inst" systemctl is-active evsim 2>&1 || true)" ;;
      esac
    done
    n=$(printf 'Authorization: Bearer %s\n' "$api" \
        | ix k3s-master curl -sS -H @- "http://127.0.0.1:$API_PORT/connections?prefix=pt.$city" 2>/dev/null \
        | grep -o '"count": *[0-9]*' | head -1 | grep -o '[0-9]*$' || true)
    echo "  connected chargers: ${n:-?}"
  done
}

# ---- main ----------------------------------------------------------------------------
step="${1:-all}"; [ $# -gt 0 ] && shift
if [ $# -gt 0 ]; then
  cities=("$@")
  for c in "${cities[@]}"; do [ -n "${CITY_NET[$c]:-}" ] || { echo "unknown city: $c" >&2; exit 2; }; done
else
  mapfile -t cities < <(printf '%s\n' "${!CITY_NET[@]}" | sort)
fi

case "$step" in
  image) step_image ;;
  update-sim) step_update_sim "${cities[@]}" ;;
  networks) step_networks ;;
  instances) step_instances "${cities[@]}" ;;
  certs) step_certs "${cities[@]}" ;;
  edges) step_edges "${cities[@]}" ;;
  stations) step_stations "${cities[@]}" ;;
  register) step_register "${cities[@]}" ;;
  csms) step_csms ;;
  monitoring) step_monitoring "${cities[@]}" ;;
  status) step_status "${cities[@]}" ;;
  all)
    step_image; step_networks; step_instances "${cities[@]}"; step_certs "${cities[@]}"
    step_edges "${cities[@]}"; step_stations "${cities[@]}"; step_register "${cities[@]}"; step_csms
    step_monitoring "${cities[@]}"
    step_status "${cities[@]}" ;;
  -h|--help|help) sed -n '2,10p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//' ;;
  *) echo "unknown step: $step" >&2; exit 2 ;;
esac
