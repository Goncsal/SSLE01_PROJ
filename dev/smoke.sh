#!/usr/bin/env bash
# Check a running local stack (started by dev/run_local.sh). Prints PASS or FAIL per
# check and exits 1 if any check fails.
#
#   dev/smoke.sh [--sessions] [--roster PATH] [--wait S]
#
#   --sessions      also require a "session.stop" line in logs/csms-*.json
#                   (a session ends after about 8 minutes on average: use --wait 900)
#   --roster PATH   roster the stack was started with (default dev/roster.yaml)
#   --wait S        keep retrying each check for up to S seconds (default 60)
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ROSTER_ARG="dev/roster.yaml"
SESSIONS=0
WAIT=60
REGISTRY_BASE="http://127.0.0.1:8001"
CSMS_BASE="http://127.0.0.1:8000"
LOCATE_CAP=500           # /locate returns at most 500 matches

die() { echo "error: $*" >&2; exit 1; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --sessions) SESSIONS=1; shift ;;
    --roster) [[ $# -ge 2 ]] || die "--roster needs a path"; ROSTER_ARG="$2"; shift 2 ;;
    --wait) [[ $# -ge 2 ]] || die "--wait needs seconds"; WAIT="$2"; shift 2 ;;
    -h|--help) sed -n '2,9p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) die "unknown option: $1" ;;
  esac
done

case "$ROSTER_ARG" in
  /*) ROSTER="$ROSTER_ARG" ;;
  *) ROSTER="$PWD/$ROSTER_ARG" ;;
esac
cd "$ROOT"

VENV="$ROOT/.venv"
LOCAL_ENV="$ROOT/secrets/local.env"
[[ -x "$VENV/bin/python" ]] || die "no $VENV: run dev/run_local.sh first"
[[ -f "$LOCAL_ENV" ]] || die "no $LOCAL_ENV: run dev/run_local.sh first"
set -a
# shellcheck disable=SC1090
source "$LOCAL_ENV"
set +a

# Expectations come from the roster, so the same script works for big rosters.
EXPECTED="$("$VENV/bin/python" - "$ROSTER" <<'PY'
import sys
from evcommon.roster import load_roster

r = load_roster(sys.argv[1])
for c in r.cities:
    elements = 1 + 1 + len(c.stations) + sum(len(s.chargers) for s in c.stations)
    print("city", c.city, elements)
print("chargers", sum(1 for _ in r.iter_chargers()))
for sid, _, st in r.iter_stations():
    print("station", sid, st.dev_metrics_port)
PY
)" || die "cannot read roster $ROSTER"

PASSED=0
FAILED=0

# check NAME FUNCTION [ARGS...]: retry FUNCTION until it succeeds or WAIT seconds pass.
# FUNCTION prints a short detail on success, or the reason on failure.
check() {
  local name="$1" fn="$2" deadline out
  shift 2
  deadline=$((SECONDS + WAIT))
  while true; do
    if out="$("$fn" "$@" 2>&1)"; then
      echo "PASS $name${out:+ ($out)}"
      PASSED=$((PASSED + 1))
      return 0
    fi
    if (( SECONDS >= deadline )); then
      echo "FAIL $name: $out"
      FAILED=$((FAILED + 1))
      return 0
    fi
    sleep 1
  done
}

json_field() {  # json_field FIELD < body
  "$VENV/bin/python" -c 'import json,sys; print(json.load(sys.stdin)[sys.argv[1]])' "$1"
}

check_registry_health() {
  local body
  body="$(curl -fsS "$REGISTRY_BASE/health")" || { echo "no answer from $REGISTRY_BASE/health"; return 1; }
  grep -Eq '"status": ?"ok"' <<<"$body" || { echo "status is not ok: $body"; return 1; }
  echo "$REGISTRY_BASE/health"
}

check_csms_health() {
  local body
  body="$(curl -fsS "$CSMS_BASE/health")" || { echo "no answer from $CSMS_BASE/health"; return 1; }
  grep -Eq '"status": ?"ok"' <<<"$body" || { echo "status is not ok: $body"; return 1; }
  echo "$CSMS_BASE/health"
}

locate_check() {  # locate_check CITY EXPECTED_ELEMENTS
  local city="$1" want="$2" body got
  body="$(curl -fsS "$REGISTRY_BASE/locate/pt.$city")" || { echo "locate failed"; return 1; }
  got="$(json_field count <<<"$body")"
  want=$(( want < LOCATE_CAP ? want : LOCATE_CAP ))
  if [[ "$got" == "$want" ]]; then
    echo "$got elements under pt.$city"
  else
    echo "pt.$city: $got elements, expected $want"
    return 1
  fi
}

connections_check() {
  local body got want
  want="$(awk '$1 == "chargers" {print $2}' <<<"$EXPECTED")"
  body="$(curl -fsS -H "Authorization: Bearer ${CSMS_API_TOKEN:-}" "$CSMS_BASE/connections?prefix=pt")" \
    || { echo "GET /connections failed (check CSMS_API_TOKEN)"; return 1; }
  got="$(json_field count <<<"$body")"
  echo "$got of $want chargers connected"
  [[ "$got" == "$want" ]]
}

station_metrics_check() {
  local sid port bad=0 total=0 failed=""
  while read -r kind sid port; do
    [[ "$kind" == "station" ]] || continue
    total=$((total + 1))
    if ! curl -fsS -o /dev/null "http://127.0.0.1:$port/metrics"; then
      bad=$((bad + 1))
      failed="$failed $port"
    fi
  done <<<"$EXPECTED"
  if (( bad == 0 )); then
    echo "$total station metrics ports answer"
  else
    echo "$bad of $total station metrics ports do not answer:$failed"
    return 1
  fi
}

csms_metrics_check() {
  local body
  body="$(curl -fsS "$CSMS_BASE/metrics")" || { echo "no answer from $CSMS_BASE/metrics"; return 1; }
  grep -q 'evcharge_csms_connected_chargers' <<<"$body" \
    || { echo "evcharge_csms_connected_chargers missing"; return 1; }
  echo "evcharge_csms_connected_chargers present"
}

session_stop_check() {
  shopt -s nullglob
  local files=("$ROOT"/logs/csms-*.json)
  shopt -u nullglob
  (( ${#files[@]} > 0 )) || { echo "no logs/csms-*.json yet"; return 1; }
  grep -Eq '"event": ?"session\.stop"' "${files[@]}" \
    || { echo "no session.stop line yet"; return 1; }
  echo "session.stop found"
}

check "registry /health" check_registry_health
check "CSMS /health" check_csms_health
while read -r kind city want; do
  [[ "$kind" == "city" ]] || continue
  check "registry /locate/pt.$city" locate_check "$city" "$want"
done <<<"$EXPECTED"
check "CSMS /connections count = roster chargers" connections_check
check "station metrics ports" station_metrics_check
check "CSMS /metrics has connected_chargers" csms_metrics_check
if (( SESSIONS )); then
  check "csms log has session.stop" session_stop_check
fi

echo "---"
echo "$PASSED passed, $FAILED failed"
(( FAILED == 0 ))
