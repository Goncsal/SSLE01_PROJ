#!/usr/bin/env bash
# Start the local stack: registry, CSMS, one simulator process per station.
#
#   dev/run_local.sh [--fresh] [--roster PATH] [--sim-args "..."]
#
#   --fresh          delete data/*.db and logs/* first (new databases, empty logs)
#   --roster PATH    roster file (default dev/roster.yaml)
#   --sim-args "..." extra flags for every evsim process (see docs/interfaces.md, 9.2)
#
# Needs python3 >= 3.13 (override with PYTHON=...), bash, curl. Stop with dev/stop_local.sh.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="${PYTHON:-python3}"
REGISTRY_PORT=8001
CSMS_API_PORT=8000
ROSTER_ARG="dev/roster.yaml"
SIM_ARGS_STR=""
FRESH=0

die() { echo "error: $*" >&2; exit 1; }

usage() {
  sed -n '2,11p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --fresh) FRESH=1; shift ;;
    --roster) [[ $# -ge 2 ]] || die "--roster needs a path"; ROSTER_ARG="$2"; shift 2 ;;
    --sim-args) [[ $# -ge 2 ]] || die "--sim-args needs a string"; SIM_ARGS_STR="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) die "unknown option: $1 (see --help)" ;;
  esac
done

# Resolve the roster against the caller's directory, then work from the repo root.
case "$ROSTER_ARG" in
  /*) ROSTER="$ROSTER_ARG" ;;
  *) ROSTER="$PWD/$ROSTER_ARG" ;;
esac
cd "$ROOT"

command -v curl >/dev/null || die "curl is required"
command -v "$PYTHON" >/dev/null || die "$PYTHON not found (set PYTHON=/path/to/python3.13)"
[[ -f "$ROSTER" ]] || die "roster not found: $ROSTER"

VENV="$ROOT/.venv"
STAMP="$VENV/.deps-stamp"
LOG_DIR="$ROOT/logs"
DATA_DIR="$ROOT/data"
PIDFILE="$LOG_DIR/run_local.pids"
SECRETS_DIR="$ROOT/secrets"
LOCAL_ENV="$SECRETS_DIR/local.env"
EXAMPLE_ENV="$ROOT/secrets.example/platform.env.example"
REGISTRY_URL_LOCAL="http://127.0.0.1:$REGISTRY_PORT"
CSMS_URL_LOCAL="http://127.0.0.1:$CSMS_API_PORT"

# Refuse to start on top of a running stack.
if [[ -s "$PIDFILE" ]]; then
  while read -r pid name; do
    if [[ -n "${pid:-}" ]] && kill -0 "$pid" 2>/dev/null; then
      die "a local stack is already running ($name, pid $pid); stop it with dev/stop_local.sh"
    fi
  done < "$PIDFILE"
fi

# 1. Virtual environment and packages (reinstalled when a pyproject.toml is newer than the stamp).
if [[ ! -x "$VENV/bin/python" ]]; then
  "$PYTHON" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 13) else 1)' \
    || die "$PYTHON is older than 3.13; set PYTHON to a 3.13 interpreter"
  echo "creating $VENV"
  "$PYTHON" -m venv "$VENV"
fi
"$VENV/bin/python" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 13) else 1)' \
  || die "$VENV was made with an older Python; remove it and run again"

needs_install=0
if [[ ! -f "$STAMP" ]]; then
  needs_install=1
else
  for pkg in common registry csms sim; do
    if [[ "$ROOT/$pkg/pyproject.toml" -nt "$STAMP" ]]; then needs_install=1; fi
  done
fi
if (( needs_install )); then
  echo "installing common, registry, csms, sim into $VENV"
  "$VENV/bin/python" -m pip install --quiet -c constraints.txt \
    -e common -e registry -e csms -e sim httpx
  touch "$STAMP"
fi

# The roster decides which stations to start and which cards are blocked.
STATIONS="$("$VENV/bin/python" - "$ROSTER" <<'PY'
import sys
from evcommon.roster import load_roster

r = load_roster(sys.argv[1])
for sid, _, st in r.iter_stations():
    if st.dev_metrics_port is None:
        sys.exit(f"{sid}: no dev_metrics_port in the roster (needed for local runs)")
    print(sid, st.dev_metrics_port)
PY
)"
BLOCKED_CARDS="$("$VENV/bin/python" - "$ROSTER" <<'PY'
import sys
from evcommon.roster import load_roster

print(",".join(c.card for c in load_roster(sys.argv[1]).cards if c.blocked))
PY
)"

# 2. Local secrets: random tokens for every *_TOKEN name in the template, mode 600.
mkdir -p "$SECRETS_DIR"
if [[ ! -f "$LOCAL_ENV" ]]; then
  echo "creating $LOCAL_ENV (random tokens, mode 600)"
  umask 077
  tmp_env="$LOCAL_ENV.tmp.$$"
  {
    echo "# Local development secrets. Gitignored; mode 600."
    while IFS= read -r line || [[ -n "$line" ]]; do
      case "$line" in ''|\#*) continue ;; esac
      name="${line%%=*}"
      case "$name" in
        *TOKEN*)
          token="$("$PYTHON" -c 'import secrets; print(secrets.token_urlsafe(32))')" \
            || die "cannot generate a token"
          printf '%s=%s\n' "$name" "$token"
          ;;
      esac
    done < "$EXAMPLE_ENV"
  } > "$tmp_env"
  mv "$tmp_env" "$LOCAL_ENV"
fi
chmod 600 "$LOCAL_ENV"
set -a
# shellcheck disable=SC1090
source "$LOCAL_ENV"
set +a
while IFS= read -r line; do
  case "$line" in ''|\#*) continue ;; esac
  name="${line%%=*}"
  case "$name" in *TOKEN*)
    [[ -n "${!name:-}" ]] || die "$name is empty in $LOCAL_ENV"
    ;;
  esac
done < "$EXAMPLE_ENV"

# 3. Fresh start: remove databases and logs.
mkdir -p "$DATA_DIR" "$LOG_DIR"
if (( FRESH )); then
  shopt -s nullglob
  rm -f -- "$DATA_DIR"/*.db*
  rm -f -- "$LOG_DIR"/*
  shopt -u nullglob
  echo "fresh start: databases and logs removed"
fi
: > "$PIDFILE"

# Start a background process; its pid is appended to logs/run_local.pids.
STARTED_PID=""
start_proc() {
  local name="$1"; shift
  nohup "$@" </dev/null >"$LOG_DIR/$name.out" 2>&1 &
  STARTED_PID=$!
  printf '%s %s\n' "$STARTED_PID" "$name" >> "$PIDFILE"
  echo "started $name (pid $STARTED_PID)"
}

# Refuse to start when another process already answers on the port: the health check
# would otherwise pass against the wrong process.
ensure_port_free() {
  local url="$1"
  if curl -fsS -o /dev/null "$url" 2>/dev/null; then
    die "$url already answers: another process is using that port (stop it, then run again)"
  fi
}

wait_health() {
  local name="$1" url="$2" pid="$3" i
  for ((i = 0; i < 60; i++)); do
    if curl -fsS -o /dev/null "$url" 2>/dev/null; then
      if ! kill -0 "$pid" 2>/dev/null; then
        die "$name exited during startup and $url answers from another process; see $LOG_DIR/$name.out"
      fi
      echo "$name is healthy ($url)"
      return 0
    fi
    if ! kill -0 "$pid" 2>/dev/null; then
      die "$name exited during startup; see $LOG_DIR/$name.out (stop the rest with dev/stop_local.sh)"
    fi
    sleep 0.5
  done
  die "$name did not answer $url within 30 s; see $LOG_DIR/$name.out (stop with dev/stop_local.sh)"
}

# 4. Registry, then seed it from the roster.
ensure_port_free "$REGISTRY_URL_LOCAL/health"
start_proc registry env LOG_DIR="$LOG_DIR" LOG_STDOUT=false \
  DATABASE_URL="sqlite:///$DATA_DIR/registry.db" \
  "$VENV/bin/python" -m evregistry
wait_health registry "$REGISTRY_URL_LOCAL/health" "$STARTED_PID"
REGISTRY_URL="$REGISTRY_URL_LOCAL" "$VENV/bin/python" "$ROOT/dev/seed_registry.py" \
  --roster "$ROSTER" --local \
  || die "registry seeding failed; the stack is still running (stop it with dev/stop_local.sh)"

# 5. CSMS, accepting direct connections (no edge in front of it locally).
ensure_port_free "$CSMS_URL_LOCAL/health"
start_proc csms env LOG_DIR="$LOG_DIR" LOG_STDOUT=false \
  CSMS_ALLOW_DIRECT=true \
  REGISTRY_URL="$REGISTRY_URL_LOCAL" \
  DATABASE_URL="sqlite:///$DATA_DIR/csms.db" \
  CSMS_LOCAL_BLOCKED_CARDS="$BLOCKED_CARDS" \
  "$VENV/bin/python" -m evcsms
wait_health csms "$CSMS_URL_LOCAL/health" "$STARTED_PID"

# 6. One simulator per station.
SIM_EXTRA=()
read -r -a SIM_EXTRA <<<"$SIM_ARGS_STR"
first_port=""
last_port=""
while read -r sid port; do
  [[ -n "$sid" ]] || continue
  [[ -n "$first_port" ]] || first_port="$port"
  last_port="$port"
  start_proc "sim-$sid" env LOG_DIR="$LOG_DIR" LOG_STDOUT=false \
    "$VENV/bin/python" -m evsim \
    --roster "$ROSTER" --station "$sid" --metrics-port "$port" \
    --log-dir "$LOG_DIR" ${SIM_EXTRA[@]+"${SIM_EXTRA[@]}"}
done <<< "$STATIONS"

# 7. Processes that die at once (bad flags, bad roster) are reported now.
sleep 2
dead=""
while read -r pid name; do
  [[ -n "${pid:-}" ]] || continue
  if ! kill -0 "$pid" 2>/dev/null; then dead="$dead $name"; fi
done < "$PIDFILE"
if [[ -n "$dead" ]]; then
  die "exited right after start:$dead (see $LOG_DIR/<name>.out); stop the rest with dev/stop_local.sh"
fi

# 8. Summary.
cat <<EOF

Local stack is running.
  registry        http://127.0.0.1:$REGISTRY_PORT   (/health, /nodes, /locate/pt.porto)
  CSMS API        http://127.0.0.1:$CSMS_API_PORT   (/health, /connections, /metrics; bearer CSMS_API_TOKEN)
  CSMS OCPP       ws://127.0.0.1:9000/ocpp/<hier-id>
  station metrics http://127.0.0.1:$first_port/metrics .. http://127.0.0.1:$last_port/metrics
  logs            $LOG_DIR   (<name>.out: process output; csms-*.json, sim-*.json: event lines)
  secrets         $LOCAL_ENV
  check           dev/smoke.sh
  stop            dev/stop_local.sh
EOF
