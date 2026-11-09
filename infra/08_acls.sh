#!/usr/bin/env bash
# Incus network ACLs for the city-* networks (contract section 16, flows 14.8).
#
#   infra/08_acls.sh [status|apply|remove|check] [--run] [--dry-run] [--check]
#                    [--allow-updates] [city ...]
#
# Default action is status (read-only). apply and remove only PRINT the plan unless --run is
# given, and --run is refused unless EVCHARGE_BASELINE_DONE=1 (applying an ACL changes live
# connectivity, so it must not happen while the baseline is being recorded).
# Only networks named city-<city> for a city in CITY_NET are ever touched.
# Addresses are resolved from `incus list` and CITY_NET at run time; none are stored in the repo.
. "$(dirname "$0")/lib.sh"

ACTION=status; RUN=0; UPDATES=0; CITIES=()
while [ $# -gt 0 ]; do
  case "$1" in
    status|apply|remove|check) ACTION="$1" ;;
    --check) ACTION=check ;;
    --dry-run) RUN=0 ;;
    --run) RUN=1 ;;
    --allow-updates) UPDATES=1 ;;
    -h|--help) sed -n '2,12p' "$0"; exit 0 ;;
    -*) echo "unknown option $1" >&2; exit 2 ;;
    *) CITIES+=("$1") ;;
  esac
  shift
done
if [ "${#CITIES[@]}" -eq 0 ]; then mapfile -t CITIES < <(printf '%s\n' "${!CITY_NET[@]}" | sort); fi
for c in "${CITIES[@]}"; do
  [ -n "${CITY_NET[$c]:-}" ] || { echo "unknown city '$c' (known: ${!CITY_NET[*]})" >&2; exit 2; }
done

if [ "$RUN" = 1 ] && { [ "$ACTION" = apply ] || [ "$ACTION" = remove ]; } \
   && [ "${EVCHARGE_BASELINE_DONE:-}" != 1 ]; then
  echo "refusing '$ACTION --run': EVCHARGE_BASELINE_DONE=1 is not set (the baseline run must be finished first)" >&2
  exit 3
fi

# run an incus command, or only print it when this is a dry run
DRY=1; [ "$RUN" = 1 ] && DRY=0
doit() { if [ "$DRY" = 1 ]; then printf '  [dry-run] %s\n' "$*"; else "$@"; fi; }

net_of() { echo "city-$1"; }
acl_names() { echo "evcharge-$1-stations evcharge-$1-edge"; }

# ---- address resolution ----------------------------------------------------------------
declare -A ADDR
resolve_city() {
  local city="$1" a n list=""
  ADDR=()
  ADDR[GW]="${CITY_NET[$city]%/*}"
  ADDR[GWBC]="${ADDR[GW]},$(printf "%d.%d.%d.%d" 255 255 255 255)"  # gateway plus the DHCP broadcast
  ADDR[NAT]="0.0.0.0/0"
  ADDR[EDGE]="$(ipof "$city-edge" || true)"
  list=""
  while read -r n; do
    [ -n "$n" ] || continue
    a="$(ipof "$n" || true)"; [ -n "$a" ] && list+="${list:+,}$a"
  done < <(incus list "^$city-st-[0-9]+\$" -f csv -c n)
  ADDR[STATIONS]="$list"
  list=""
  for n in $K3S_NODES; do a="$(ipof "$n" || true)"; [ -n "$a" ] && list+="${list:+,}$a"; done
  ADDR[K3S]="$list"
  ADDR[WAZUH]="$(ipof "$WAZUH_HOST" || true)"
  local r
  for r in EDGE STATIONS K3S WAZUH; do
    if [ -z "${ADDR[$r]}" ]; then
      bad "$city: no address for $r"
      ADDR[$r]="<unresolved-$r>"; UNRESOLVED=1
    fi
  done
}

# ---- rule model ------------------------------------------------------------------------
# Rules are kept symbolically (role names) in TUPLES and rendered to YAML with real addresses.
# tuple: acl|direction|protocol|source role|destination role|ports
declare -A YAML
TUPLES=()
rule() { # acl dir proto srcrole dstrole ports description
  local acl="$1" dir="$2" proto="$3" src="$4" dst="$5" ports="$6" desc="$7"
  TUPLES+=("$acl|$dir|$proto|$src|$dst|$ports")
  local y="- action: allow
  state: enabled
  description: \"$desc\"
"
  [ "$proto" = any ] || y+="  protocol: $proto
"
  y+="  source: \"${ADDR[$src]}\"
  destination: \"${ADDR[$dst]}\"
"
  [ -z "$ports" ] || y+="  destination_port: \"$ports\"
"
  YAML["$acl|$dir"]+="$y"
}

build_city() {
  local city="$1" port=443 st ed
  case " $PLAIN_WS_CITIES " in *" $city "*) port=80 ;; esac
  st="evcharge-$city-stations"; ed="evcharge-$city-edge"
  TUPLES=(); YAML=()
  # stations
  rule "$st" egress tcp STATIONS EDGE "$port" "station to its own edge"
  rule "$st" egress tcp STATIONS WAZUH "1514,1515" "station to the Wazuh manager"
  rule "$st" egress udp STATIONS GWBC "53,67" "DNS and DHCP to the gateway"
  rule "$st" egress tcp STATIONS GW "53" "DNS over tcp to the gateway"
  rule "$st" egress icmp4 STATIONS GW "" "ICMP to the gateway"
  rule "$st" ingress any GW STATIONS "" "traffic from the gateway"
  # edge
  rule "$ed" egress tcp EDGE K3S "30900,30801" "edge to the CSMS and registry NodePorts"
  rule "$ed" egress tcp EDGE WAZUH "1514,1515" "edge to the Wazuh manager"
  rule "$ed" egress udp EDGE GWBC "53,67" "DNS and DHCP to the gateway"
  rule "$ed" egress tcp EDGE GW "53" "DNS over tcp to the gateway"
  rule "$ed" egress icmp4 EDGE GW "" "ICMP to the gateway"
  rule "$ed" ingress tcp STATIONS EDGE "$port" "stations to the edge"
  rule "$ed" ingress tcp K3S EDGE "9090,9100,9113" "central Prometheus federation and exporters"
  rule "$ed" ingress any GW EDGE "" "traffic from the gateway"
  if [ "$UPDATES" = 1 ]; then
    rule "$st" egress tcp STATIONS NAT "80,443" "updates (NAT), --allow-updates only"
    rule "$ed" egress tcp EDGE NAT "80,443" "updates (NAT), --allow-updates only"
  fi
}

acl_yaml() { # acl
  # No "name:" field: the ACL's name is already given positionally to both `incus network
  # acl create <name>` and `... edit <name>`, and on incus 7.5.1 a "name:" field in the
  # piped body makes `create` fail ("yaml: construct errors: ... field name not found in
  # type api.NetworkACLPut") even though `edit` silently tolerates it -- confirmed by
  # reproducing both ways against a throwaway test ACL, read-only to anything evcharge
  # uses. Previously this made every first-time `apply` fail outright (the ACL objects were
  # never created) while still setting the network's default-drop actions -- effectively a
  # believed-applied-but-actually-absent ACL with the drop actions (harmlessly) orphaned,
  # since incus does not enforce default actions without at least one ACL attached.
  local acl="$1" e="${YAML["$1|egress"]:-}" i="${YAML["$1|ingress"]:-}"
  printf 'description: "evcharge city ACL (managed by infra/08_acls.sh)"\n'
  if [ -n "$e" ]; then printf 'egress:\n%s' "$e"; else printf 'egress: []\n'; fi
  if [ -n "$i" ]; then printf 'ingress:\n%s' "$i"; else printf 'ingress: []\n'; fi
}

# ---- check: the computed rules equal the allow list of section 16, nothing more --------
check_city() {
  local city="$1" port=443 st ed fail=0 want got
  case " $PLAIN_WS_CITIES " in *" $city "*) port=80 ;; esac
  st="evcharge-$city-stations"; ed="evcharge-$city-edge"
  want="$(cat <<W | sort
$st|egress|tcp|STATIONS|EDGE|$port
$st|egress|tcp|STATIONS|WAZUH|1514,1515
$st|egress|udp|STATIONS|GWBC|53,67
$st|egress|tcp|STATIONS|GW|53
$st|egress|icmp4|STATIONS|GW|
$st|ingress|any|GW|STATIONS|
$ed|egress|tcp|EDGE|K3S|30900,30801
$ed|egress|tcp|EDGE|WAZUH|1514,1515
$ed|egress|udp|EDGE|GWBC|53,67
$ed|egress|tcp|EDGE|GW|53
$ed|egress|icmp4|EDGE|GW|
$ed|ingress|tcp|STATIONS|EDGE|$port
$ed|ingress|tcp|K3S|EDGE|9090,9100,9113
$ed|ingress|any|GW|EDGE|
W
)"
  if [ "$UPDATES" = 1 ]; then
    want="$(printf '%s\n%s\n%s\n' "$want" "$st|egress|tcp|STATIONS|NAT|80,443" "$ed|egress|tcp|EDGE|NAT|80,443" | sort)"
  fi
  got="$(printf '%s\n' "${TUPLES[@]}" | sort)"
  if [ "$want" = "$got" ]; then ok "$city: ${#TUPLES[@]} rules equal the allow list (default action drop, both directions)"
  else bad "$city: rules differ from the allow list"; diff <(echo "$want") <(echo "$got") || true; fail=1; fi
  # no rule may allow a station to reach a NodePort, a peer station, or another city
  if printf '%s\n' "${TUPLES[@]}" | grep -E '\|STATIONS\|(K3S|STATIONS)\|' | grep -q egress; then
    bad "$city: a station rule reaches the K3s nodes or a peer"; fail=1
  fi
  if [ "${ADDR[EDGE]#<}" != "${ADDR[EDGE]}" ] || [ "${ADDR[K3S]#<}" != "${ADDR[K3S]}" ]; then
    bad "$city: some addresses are unresolved (are the instances running?)"; fail=1
  fi
  return "$fail"
}

# ---- attach / detach -------------------------------------------------------------------
current_acls() { incus network get "$1" security.acls 2>/dev/null | tr -d ' ' || true; }

apply_city() {
  local city="$1" net acl have want
  net="$(net_of "$city")"
  incus network show "$net" >/dev/null 2>&1 || { bad "$net does not exist"; return 1; }
  log "$net: ACLs $([ "$DRY" = 1 ] && echo "(dry-run)")"
  for acl in $(acl_names "$city"); do
    printf -- '--- %s\n' "$acl"; acl_yaml "$acl" | sed 's/^/    /'
    if incus network acl show "$acl" >/dev/null 2>&1; then
      if [ "$DRY" = 1 ]; then echo "  [dry-run] incus network acl edit $acl   (rules replaced from the YAML above)"
      else acl_yaml "$acl" | incus network acl edit "$acl"; fi
    else
      if [ "$DRY" = 1 ]; then echo "  [dry-run] incus network acl create $acl   (rules from the YAML above)"
      else acl_yaml "$acl" | incus network acl create "$acl"; fi
    fi
  done
  have="$(current_acls "$net")"
  want="$have"
  for acl in $(acl_names "$city"); do
    case ",$want," in *",$acl,"*) ;; *) want+="${want:+,}$acl" ;; esac
  done
  doit incus network set "$net" security.acls="$want"
  doit incus network set "$net" security.acls.default.egress.action=drop
  doit incus network set "$net" security.acls.default.ingress.action=drop
}

remove_city() {
  local city="$1" net acl have keep="" a
  net="$(net_of "$city")"
  incus network show "$net" >/dev/null 2>&1 || { bad "$net does not exist"; return 1; }
  log "$net: removing the evcharge ACLs $([ "$DRY" = 1 ] && echo "(dry-run)")"
  have="$(current_acls "$net")"
  for a in ${have//,/ }; do
    case "$a" in "evcharge-$city-"*) ;; *) keep+="${keep:+,}$a" ;; esac
  done
  if [ -n "$keep" ]; then doit incus network set "$net" security.acls="$keep"
  else
    doit incus network unset "$net" security.acls
    doit incus network unset "$net" security.acls.default.egress.action
    doit incus network unset "$net" security.acls.default.ingress.action
  fi
  for acl in $(acl_names "$city"); do
    if incus network acl show "$acl" >/dev/null 2>&1; then doit incus network acl delete "$acl"
    else echo "  (acl $acl not present)"; fi
  done
}

status_city() {
  local city="$1" net acl
  net="$(net_of "$city")"
  incus network show "$net" >/dev/null 2>&1 || { bad "$net does not exist"; return 1; }
  log "$net"
  printf '  security.acls: %s\n  default egress: %s  ingress: %s\n' \
    "$(current_acls "$net" | sed 's/^$/(none: open)/')" \
    "$(incus network get "$net" security.acls.default.egress.action | sed 's/^$/(unset)/')" \
    "$(incus network get "$net" security.acls.default.ingress.action | sed 's/^$/(unset)/')"
  for acl in $(acl_names "$city"); do
    if incus network acl show "$acl" >/dev/null 2>&1; then incus network acl show "$acl" | sed 's/^/    /'
    else echo "  acl $acl: not present"; fi
  done
}

# ---- main ------------------------------------------------------------------------------
UNRESOLVED=0; RC=0
for city in "${CITIES[@]}"; do
  case "$ACTION" in
    status) status_city "$city" || RC=1 ;;
    remove) remove_city "$city" || RC=1 ;;
    apply|check)
      resolve_city "$city"; build_city "$city"
      if [ "$ACTION" = check ]; then check_city "$city" || RC=1
      else
        check_city "$city" || RC=1
        if [ "$UNRESOLVED" = 1 ] && [ "$DRY" = 0 ]; then bad "refusing to apply with unresolved addresses"; exit 1; fi
        apply_city "$city" || RC=1
      fi ;;
  esac
done
if [ "$ACTION" = apply ] || [ "$ACTION" = remove ]; then
  if [ "$DRY" = 1 ]; then log "dry-run only: nothing changed (use --run with EVCHARGE_BASELINE_DONE=1 to $ACTION)"
  else log "$ACTION done for: ${CITIES[*]}"; fi
fi
exit "$RC"
