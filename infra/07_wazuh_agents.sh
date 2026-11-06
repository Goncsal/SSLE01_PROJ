#!/usr/bin/env bash
# Wazuh agents on the whole platform (contract 15.1, 15.7).
#
#   infra/07_wazuh_agents.sh [all|groups|central|edges|stations] [city ...]
#
# all       groups, station limits, ONE station (memory checked), the other stations one by one,
#           the edges, then the DaemonSet on the K3s nodes; prints `agent_control -l`
# groups    create the agent groups on the manager and push wazuh/groups/*/agent.conf
# stations  raise the station memory limit and install the agents (first station gates the rest)
# edges     agents and iptables on the city edges
# central   agent image (built when missing), namespace, Secret wazuh-enroll, DaemonSet
#
# Environment: ONE_PER_CITY=1 installs only st-001 of each city (memory fallback),
# STATION_MEM_STOP_MIB=180 stops the roll-out when the first station's working set exceeds it,
# MIN_AVAIL_MB=1500 stops when the host's available memory is lower.
# The enrolment password (WAZUH_ENROLL_PASSWORD in secrets/platform.env) is optional until the
# manager requires it and travels on stdin only. Idempotent; never restarts evsim, nginx or the CSMS.
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

SECRETS="${SECRETS:-$ROOT/secrets/platform.env}"
ST_MEM_LIMIT="${ST_MEM:-224MiB}"
STATION_MEM_STOP_MIB="${STATION_MEM_STOP_MIB:-180}"
MIN_AVAIL_MB="${MIN_AVAIL_MB:-1500}"
ONE_PER_CITY="${ONE_PER_CITY:-0}"
STATIONS_PER_CITY="${STATIONS_PER_CITY:-5}"
AGENT_IMAGE_REPO="evcharge-wazuh-agent"
AGENT_IMAGE_TAG="4.14.9"
OSSEC=/var/ossec
INSTALL="$ROOT/wazuh/agents/install_native.sh"

mx() { incus exec "$WAZUH_HOST" -- "$@"; }
secret() { grep -m1 "^$1=" "$SECRETS" 2>/dev/null | cut -d= -f2- || true; }
# password on stdin of the installer; an empty line while the manager does not require one
# (authd rejects a request that carries a password it does not expect)
enrol_pw() {
  if mx grep -q '<use_password>yes</use_password>' $OSSEC/etc/ossec.conf; then
    printf '%s\n' "$(secret WAZUH_ENROLL_PASSWORD)"
  else
    printf '\n'
  fi
}

guard_mem() {
  local kb; kb=$(awk '/^MemAvailable:/ {print $2}' /proc/meminfo)
  if [ "$((kb / 1024))" -lt "$MIN_AVAIL_MB" ]; then
    echo "host available memory $((kb / 1024)) MiB < MIN_AVAIL_MB=$MIN_AVAIL_MB, stopping" >&2; exit 3
  fi
}
host_avail() { awk '/^MemAvailable:/ {printf "%d MiB", $2/1024}' /proc/meminfo; }

# working set of an instance in MiB (cgroup usage minus reclaimable file cache)
ws_mib() {
  incus exec "$1" -- sh -c 'cur=$(cat /sys/fs/cgroup/memory.current)
    inact=$(awk "/^inactive_file /{print \$2}" /sys/fs/cgroup/memory.stat)
    echo $(( (cur - inact) / 1048576 ))'
}
usage_mib() { incus exec "$1" -- sh -c 'echo $(( $(cat /sys/fs/cgroup/memory.current) / 1048576 ))'; }

wait_manager() {
  local i
  for i in $(seq 1 30); do mx $OSSEC/bin/agent_groups -l >/dev/null 2>&1 && return 0; sleep 5; done
  echo "Wazuh manager does not answer" >&2; return 1
}

stations_of() { local i n="$STATIONS_PER_CITY"; [ "$ONE_PER_CITY" = 1 ] && n=1
  for i in $(seq 1 "$n"); do printf 'st-%03d\n' "$i"; done; }

chargers_connected() { # read-only count through the CSMS API
  local tok; tok=$(secret CSMS_API_TOKEN)
  printf 'Authorization: Bearer %s\n' "$tok" \
    | incus exec k3s-master -- curl -sS -m 10 -H @- "http://127.0.0.1:30800/connections" 2>/dev/null \
    | grep -o '"count": *[0-9]*' | head -1 | grep -o '[0-9]*$' || true
}

# ---- groups -------------------------------------------------------------------------------
step_groups() {
  local g have tmp
  wait_manager
  for g in central edge station $(for c in "$@"; do echo "city-$c"; done); do
    if ! mx $OSSEC/bin/agent_groups -l | grep -qE "^ +$g \("; then
      mx $OSSEC/bin/agent_groups -q -a -g "$g" >/dev/null
      ok "group $g created"
    fi
  done
  for g in central edge station; do
    tmp="$(mktemp)"; cat "$ROOT/wazuh/groups/$g/agent.conf" > "$tmp"
    have=$(mx sh -c "sha256sum < $OSSEC/etc/shared/$g/agent.conf 2>/dev/null" | cut -d' ' -f1 || true)
    if [ "$have" != "$(sha256sum < "$tmp" | cut -d' ' -f1)" ]; then
      incus file push -q "$tmp" "$WAZUH_HOST$OSSEC/etc/shared/$g/agent.conf"
      mx chown wazuh:wazuh "$OSSEC/etc/shared/$g/agent.conf"; mx chmod 660 "$OSSEC/etc/shared/$g/agent.conf"
      mx $OSSEC/bin/verify-agent-conf -f "$OSSEC/etc/shared/$g/agent.conf" >/dev/null
      ok "agent.conf of $g pushed and verified"
    else
      ok "agent.conf of $g unchanged"
    fi
    rm -f "$tmp"
  done
}

# ---- stations -----------------------------------------------------------------------------
install_station() { # <city> <st>
  local inst="$1-$2"
  guard_mem
  if [ "$(incus config get "$inst" limits.memory)" != "$ST_MEM_LIMIT" ]; then
    incus config set "$inst" "limits.memory=$ST_MEM_LIMIT"
    ok "$inst: memory limit $ST_MEM_LIMIT (live)"
  fi
  enrol_pw | "$INSTALL" "$inst" "pt.$1.$2" "city-$1,station"
}

step_stations() {
  local city st first=1 before after n0 n1 ev
  wait_manager
  n0=$(chargers_connected)
  log "stations: host available $(host_avail), chargers connected before: ${n0:-?}"
  for city in "$@"; do
    for st in $(stations_of); do
      if [ "$first" = 1 ]; then
        before=$(usage_mib "$city-$st")
        install_station "$city" "$st"
        sleep 45   # let the agent finish its first scans before measuring
        after=$(ws_mib "$city-$st")
        ev=$(incus exec "$city-$st" -- systemctl is-active evsim || true)
        log "first station $city-$st: memory $before MiB -> usage $(usage_mib "$city-$st") MiB (working set $after MiB), evsim $ev, host available $(host_avail)"
        if [ "$after" -gt "$STATION_MEM_STOP_MIB" ]; then
          bad "working set $after MiB exceeds $STATION_MEM_STOP_MIB MiB, stopping (use ONE_PER_CITY=1 for the fallback)"; exit 4
        fi
        [ "$ev" = active ] || { bad "evsim is $ev on $city-$st, stopping"; exit 4; }
        n1=$(chargers_connected)
        if [ -n "$n0" ] && [ "${n1:-0}" -lt "$n0" ]; then
          bad "chargers connected dropped from $n0 to ${n1:-0} after $city-$st, stopping"; exit 4
        fi
        first=0
      else
        install_station "$city" "$st"
        [ "$(incus exec "$city-$st" -- systemctl is-active evsim || true)" = active ] \
          || { bad "evsim is not active on $city-$st, stopping"; exit 4; }
      fi
    done
  done
  n1=$(chargers_connected)
  log "stations done: host available $(host_avail), chargers connected after: ${n1:-?}"
}

# ---- edges ----------------------------------------------------------------------------------
step_edges() {
  local city
  wait_manager
  for city in "$@"; do
    guard_mem
    enrol_pw | "$INSTALL" "$city-edge" "pt.$city.edge" "city-$city,edge"
  done
}

# ---- central (K3s nodes) ------------------------------------------------------------------------
agent_image_present() {
  incus exec k3s-master -- curl -fsS -m 10 "http://$REGISTRY/v2/$AGENT_IMAGE_REPO/tags/list" 2>/dev/null \
    | grep -q "\"$AGENT_IMAGE_TAG\""
}

step_central() {
  local pw
  wait_manager
  if ! agent_image_present; then
    log "agent image missing in the registry, building"
    "$ROOT/infra/04_build_push.sh" wazuh-agent
  fi
  kc apply -f - < "$ROOT/k8s/wazuh/namespace.yaml" >/dev/null
  pw=$(secret WAZUH_ENROLL_PASSWORD)
  if [ -n "$pw" ]; then
    # the value goes through stdin only
    printf '%s' "$pw" | incus exec k3s-master -- sh -c \
      'kubectl -n wazuh create secret generic wazuh-enroll --from-file=password=/dev/stdin --dry-run=client -o yaml | kubectl apply -f -' >/dev/null
    ok "Secret wazuh-enroll applied"
  else
    ok "no WAZUH_ENROLL_PASSWORD in the secrets file, Secret wazuh-enroll not created (the manager must not require one yet)"
  fi
  kc apply -f - < "$ROOT/k8s/wazuh/daemonset.yaml" >/dev/null
  kc -n wazuh rollout status daemonset/wazuh-agent --timeout=240s
  kc -n wazuh get pods -o wide
}

# ---- main -------------------------------------------------------------------------------------------
step="${1:-all}"; [ $# -gt 0 ] && shift
if [ $# -gt 0 ]; then
  cities=("$@")
  for c in "${cities[@]}"; do [ -n "${CITY_NET[$c]:-}" ] || { echo "unknown city: $c" >&2; exit 2; }; done
else
  mapfile -t cities < <(printf '%s\n' "${!CITY_NET[@]}" | sort)
fi

summary() {
  log "agents"
  mx $OSSEC/bin/agent_control -l | grep -E 'ID:' || true
}

case "$step" in
  groups) step_groups "${cities[@]}" ;;
  stations) step_groups "${cities[@]}"; step_stations "${cities[@]}"; summary ;;
  edges) step_groups "${cities[@]}"; step_edges "${cities[@]}"; summary ;;
  central) step_groups "${cities[@]}"; step_central; summary ;;
  all) step_groups "${cities[@]}"; step_stations "${cities[@]}"; step_edges "${cities[@]}"
       step_central; summary ;;
  -h|--help|help) sed -n '2,19p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//' ;;
  *) echo "unknown step: $step" >&2; exit 2 ;;
esac
