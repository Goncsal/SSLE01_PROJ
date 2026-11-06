#!/usr/bin/env bash
# One Incus bridge per city (a separate L2 site), routed to the central bridge.
#
# Findings this script relies on (tested 2026-11-05):
#  - Incus routes between its bridges and keeps the source address, so the
#    central side sees the real city IP (no NAT between bridges).
#  - Every bridge's dnsmasq defaults to dns.domain "incus" and answers that zone
#    locally, so central names were NXDOMAIN from a city. Each city therefore gets
#    its own zone (<city>.pt.lab, authoritative in that city's resolver: DNS
#    delegation per site) and forwards *.incus to the central bridge.
#
# DNS aliases (contract 13.1): edge.<city>.pt.lab and st-NNN.<city>.pt.lab are cnames for the
# instance names. The station ids are generated from STATIONS_PER_CITY (st-001 ...), the same
# rule as the roster generator (dev/gen_roster.py); no roster parsing is needed on the host.
# raw.dnsmasq of a city bridge is rewritten completely on every run, so the result is the same each
# time. The central bridge gets one `server=/<city>.pt.lab/<city gateway>` line per city (the K3s pods
# resolve *.pt.lab through the node's resolver, contract 14.2); its other lines are kept.
. "$(dirname "$0")/lib.sh"

central_dns="${BRIDGE_CIDR%/*}"
for city in "${!CITY_NET[@]}"; do
  net="city-$city"
  if incus network show "$net" >/dev/null 2>&1; then
    log "$net exists"
  else
    log "creating $net (${CITY_NET[$city]})"
    incus network create "$net" ipv4.address="${CITY_NET[$city]}" ipv4.nat=true ipv6.address=none
  fi
  raw="server=/incus/$central_dns"$'\n'"cname=edge.$city.pt.lab,$city-edge.$city.pt.lab"
  for i in $(seq 1 "$STATIONS_PER_CITY"); do
    st=$(printf 'st-%03d' "$i")
    raw+=$'\n'"cname=$st.$city.pt.lab,$city-$st.$city.pt.lab"
  done
  incus network set "$net" dns.domain="$city.pt.lab" raw.dnsmasq="$raw"
  incus network set "$net" user.hier-id="pt.$city"
done

# central bridge: forward each city zone to that city's resolver, keep everything else
have="$(incus network get "$BRIDGE" raw.dnsmasq || true)"
raw=""
while IFS= read -r line; do
  case "$line" in
    ''|server=/*.pt.lab/*) ;;
    *) raw+="$line"$'\n' ;;
  esac
done <<< "$have"
for city in $(printf '%s\n' "${!CITY_NET[@]}" | sort); do
  gw="${CITY_NET[$city]%/*}"
  raw+="server=/$city.pt.lab/$gw"$'\n'
done
raw="${raw%$'\n'}"
if [ "$raw" != "$have" ]; then
  log "$BRIDGE raw.dnsmasq: forward the city zones"
  printf 'before:\n%s\n' "${have:-(empty)}"
  incus network set "$BRIDGE" raw.dnsmasq="$raw"
  printf 'after:\n%s\n' "$raw"
else
  log "$BRIDGE raw.dnsmasq already forwards the city zones"
fi
incus network list -f compact | grep -E 'NAME|city-|'"$BRIDGE"
