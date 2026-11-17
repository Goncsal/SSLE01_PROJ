#!/usr/bin/env bash
# Fraud PoC 1: a cloned RFID card authorises sessions in two cities within minutes
# (impossible travel), driven through the simulator's existing --clone-card flag -- no
# new attacker containers (docs/interfaces.md v6 9.2/9.4). See DESIGN-fraud.md "Cloned
# card" and 4.4 (impossible_travel rule, block_card action).
#
#   clone_card.sh [--home porto] [--used-in aveiro,lisboa] [--phase before|after]
#                 [--card CARD] [--run] [--duration N]
#
# Picks a non-blocked card whose home_city is --home from dev/roster.yaml, then uses it
# in each --used-in city one after the other: on one station of the city sets
# SIM_EXTRA_ARGS="--clone-card <card> --clone-sessions 1 ..." in /etc/evcharge/sim.env
# (saving the previous content first), restarts evsim there, waits until the CSMS logs
# the cloned session's session.start and session.stop, restores that station's sim.env,
# and moves on to the next city. In the last city it stops at session.start (the block
# happens at the second start) and the exit trap restores. Every sim.env is also restored on exit and on signal
# (see save_sim_env/restore_sim_env in lib.sh; the restore is idempotent).
#
# --phase only changes the printed expectation; this script never starts or stops the
# fraud service itself (the operator does that between --phase before and --phase after
# calls, per DESIGN-fraud.md "Before / after").
#
# Default: --dry-run (prints the plan, changes nothing). --run is refused unless
# EVCHARGE_BASELINE_DONE=1 (set in the environment before the live run).
set -euo pipefail
# shellcheck source=./lib.sh
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

HOME_CITY=porto
USED_IN=aveiro,lisboa
PHASE=after
CLONE_SESSIONS=1
CARD=""
# The cities are used sequentially, each with one short session. The CSMS refuses a second
# concurrent transaction for one card (auth.reject result=ConcurrentTx), so cloned sessions
# that overlap in two cities are refused rather than detected: that guard prevents the
# overlap but neither raises an alert nor blocks the card, and no second-city session.start
# ever reaches the fraud service. A sequential clone does: the first session stops, the
# second city's session.start follows a couple of minutes later, and the fraud service's
# impossible-travel rule scores the two starts. Over ~218 km (Aveiro -> Lisboa) a gap of a
# couple of minutes means a speed far above the 400 km/h block threshold. A fast arrival
# rate and a one-minute session keep each step short. Override with --arrival-rate /
# --session-minutes if needed.
CLONE_ARRIVAL=90
CLONE_SESSION_MIN=1
CITY_TIMEOUT=240   # per-city wait for the cloned session, also capped by --duration

print_usage() {
  cat <<EOF
usage: $(basename "$0") [--home CITY] [--used-in CITY,CITY,...] [--phase before|after] [--card CARD] [--run] [--duration SECONDS] [--dry-run]

Picks a non-blocked card whose home_city is --home (dev/roster.yaml), then on one station
in each --used-in city, one city after the other, sets SIM_EXTRA_ARGS="--clone-card <card>
--clone-sessions $CLONE_SESSIONS" (one short session), restarts evsim, waits for the session to
start and stop in the CSMS log, then restores that station. --duration is the overall time-box. --phase before/after only changes the printed expectation.
EOF
  usage_common
}

ARGS=()
while [ $# -gt 0 ]; do
  case "$1" in
    --home) HOME_CITY="$2"; shift 2 ;;
    --used-in) USED_IN="$2"; shift 2 ;;
    --phase) PHASE="$2"; shift 2 ;;
    --card) CARD="$2"; shift 2 ;;
    --arrival-rate) CLONE_ARRIVAL="$2"; shift 2 ;;
    --session-minutes) CLONE_SESSION_MIN="$2"; shift 2 ;;
    -h|--help) print_usage; exit 0 ;;
    *) ARGS+=("$1"); shift ;;
  esac
done
DURATION=480   # overall time-box (lib.sh preset its own default); --duration overrides
parse_global_flags "${ARGS[@]}"

case "$PHASE" in
  before|after) ;;
  *) echo "--phase must be before or after" >&2; exit 2 ;;
esac

[ -n "$CARD" ] || CARD="$(pick_card "$HOME_CITY")"
[ -n "$CARD" ] || { echo "no non-blocked card with home_city=$HOME_CITY in $ROSTER" >&2; exit 2; }

IFS=',' read -r -a CITIES <<< "$USED_IN"
[ "${#CITIES[@]}" -ge 1 ] || { echo "--used-in needs at least one city" >&2; exit 2; }

echo "== fraud PoC 1: cloned card ($CARD, home $HOME_CITY) used in: ${CITIES[*]} =="
echo "  clone-sessions each  : $CLONE_SESSIONS (arrival ${CLONE_ARRIVAL}/h, ${CLONE_SESSION_MIN} min each, to overlap)"
echo "  time-box             : ${DURATION}s"
if [ "$PHASE" = before ]; then
  echo "  before               : fraud service stopped, or FRAUD_DRY_RUN=true -- the cloned"
  echo "                         sessions complete unflagged (no block-card); this is the"
  echo "                         detector-off baseline for the before/after comparison"
else
  echo "  after (default)      : fraud service running, FRAUD_DRY_RUN=false (the lab"
  echo "                         default) -- expect fraud.alert impossible_travel, then"
  echo "                         fraud.action block_card"
fi
echo "  expected detections  : fraud impossible_travel (distance_km, speed_kmh >"
echo "                         FRAUD_MAX_KMH, city coordinates from the registry); Wazuh"
echo "                         110207 (impossible travel), 110208 (fraud service alert)"
echo "  expected response    : 110210 (high-confidence cloned card) -> block-card"
echo "                         (billing: card Blocked + the sessions flagged)"
echo

declare -a INSTANCES=()
for city in "${CITIES[@]}"; do
  st="$(first_station_for_city "$city")"
  [ -n "$st" ] || { echo "no station found for city $city in $ROSTER" >&2; exit 2; }
  hier="pt.$city.$st"
  inst="$(station_instance "$hier")"
  INSTANCES+=("$inst")
  echo "  target station       : $hier  ($inst)"
done
echo

for inst in "${INSTANCES[@]}"; do
  save_sim_env "$inst"
  add_cleanup "restore_sim_env '$inst'"
done

# csms_card_event EVENT CARD CITY SECONDS: succeeds if the CSMS log of the last SECONDS
# seconds holds a JSON line with that event for the card in that city. kubectl logs -l
# defaults to --tail=10, so --tail=-1 is needed.
csms_card_event() {
  kc -n evcharge logs -l app.kubernetes.io/name=csms -c csms --since="${4}s" --tail=-1 2>/dev/null \
    | grep -E "\"event\": ?\"$1\"" | grep -E "\"card\": ?\"$2\"" | grep -qE "\"city\": ?\"$3\""
}

# wait_city_session CITY T0 TIMEOUT NEED_STOP: poll until the cloned session started in CITY
# and, if NEED_STOP=1, stopped too (an earlier city must be done before the next one starts,
# else ConcurrentTx; for the last city the second session.start is all the fraud service needs)
wait_city_session() {
  local city="$1" t0="$2" limit="$3" need_stop="${4:-1}" now el started=0
  while :; do
    now=$(date +%s); el=$((now - t0))
    if [ "$started" = 0 ] && csms_card_event session.start "$CARD" "$city" $((el + 10)); then
      started=1; SESSION_START_EPOCH[$city]="$(date +%s)"
      echo "  [${el}s] $city: session.start seen for $CARD"
      [ "$need_stop" = 1 ] || return 0
    fi
    if [ "$started" = 1 ] && csms_card_event session.stop "$CARD" "$city" $((el + 10)); then
      echo "  [${el}s] $city: session.stop seen for $CARD"; return 0
    fi
    if [ "$el" -ge "$limit" ]; then
      echo "  [${el}s] $city: timed out waiting for the cloned session (started=$started)" >&2
      return 1
    fi
    echo "  [${el}s] $city: waiting (start seen: $started)"
    sleep 5 & wait $!
  done
}

declare -A SESSION_START_EPOCH=()
EXTRA="--clone-card $CARD --clone-sessions $CLONE_SESSIONS --arrival-rate $CLONE_ARRIVAL --session-minutes $CLONE_SESSION_MIN"
T_ALL=$(date +%s)
for i in "${!CITIES[@]}"; do
  city="${CITIES[$i]}"; inst="${INSTANCES[$i]}"
  set_sim_extra_args "$inst" "$EXTRA"
  if [ "$DRY_RUN" = 1 ]; then
    if [ "$i" -eq $((${#CITIES[@]} - 1)) ] && [ "${#CITIES[@]}" -gt 1 ]; then
      would "poll the CSMS log until session.start for $CARD in $city (last city: no wait" \
            "for session.stop; timeout ${CITY_TIMEOUT}s, capped by --duration ${DURATION}s)"
      would "restore sim.env on $inst on exit"
    else
      would "poll the CSMS log until session.start and session.stop for $CARD in $city" \
            "(timeout ${CITY_TIMEOUT}s, capped by --duration ${DURATION}s)"
      would "restore sim.env on $inst right away (and again on exit, idempotent)"
    fi
    continue
  fi
  left=$((DURATION - ($(date +%s) - T_ALL)))
  [ "$left" -gt 0 ] || { echo "--duration ${DURATION}s used up before $city" >&2; break; }
  limit=$CITY_TIMEOUT; [ "$left" -lt "$limit" ] && limit=$left
  echo "-- $city ($inst): waiting up to ${limit}s for the cloned session --"
  if [ "$i" -eq $((${#CITIES[@]} - 1)) ] && [ "${#CITIES[@]}" -gt 1 ]; then
    # last city: the session.start is what gets scored; the exit trap restores sim.env
    wait_city_session "$city" "$(date +%s)" "$limit" 0 || true
  else
    wait_city_session "$city" "$(date +%s)" "$limit" 1 || true
    restore_sim_env "$inst"
  fi
done

if [ "$DRY_RUN" = 1 ]; then
  would "print the gap between the first and second session.start (the fraud service scores it)"
  would "read-only: GET /cards/$CARD from billing to report whether the card got blocked"
else
  echo
  first="${CITIES[0]}"; last="${CITIES[$((${#CITIES[@]} - 1))]}"
  if [ -n "${SESSION_START_EPOCH[$first]:-}" ] && [ -n "${SESSION_START_EPOCH[$last]:-}" ] \
     && [ "$first" != "$last" ]; then
    gap=$((SESSION_START_EPOCH[$last] - SESSION_START_EPOCH[$first]))
    echo "gap between session starts ($first -> $last): about ${gap}s (observed by polling," \
         "+-5s); speed = distance_km / gap, see the fraud.alert impossible_travel line"
  else
    echo "could not observe a session.start in every city; no gap to report" >&2
  fi
  # read-only check of the card state; skipped if the token or master address is missing
  token="$(grep -s '^BILLING_API_TOKEN=' "$ROOT/secrets/platform.env" | cut -d= -f2- || true)"
  master="$(ipof k3s-master 2>/dev/null || true)"
  if [ -n "$token" ] && [ -n "$master" ]; then
    echo "-- waiting 10s for the fraud service, then reading the card from billing --"
    sleep 10 & wait $!
    echo "billing card $CARD: $(curl -s -m 10 -H "Authorization: Bearer $token" \
      "http://$master:30802/cards/$CARD" || echo unreachable)"
  else
    echo "(billing token or master address not available; check the card state by hand)"
  fi
fi

echo
echo "clone_card: done (every sim.env touched above is restored; the exit trap restores" \
     "again if anything is left)"
