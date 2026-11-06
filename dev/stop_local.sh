#!/usr/bin/env bash
# Stop every process listed in logs/run_local.pids: TERM first, KILL after 10 s.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PIDFILE="$ROOT/logs/run_local.pids"

if [[ ! -f "$PIDFILE" ]]; then
  echo "nothing to stop (no $PIDFILE)"
  exit 0
fi

any_alive() {
  local pid name
  while read -r pid name; do
    if [[ -n "${pid:-}" ]] && kill -0 "$pid" 2>/dev/null; then
      return 0
    fi
  done < "$PIDFILE"
  return 1
}

while read -r pid name; do
  [[ -n "${pid:-}" ]] || continue
  if kill -0 "$pid" 2>/dev/null; then
    kill -TERM "$pid" 2>/dev/null || true
    echo "TERM $name (pid $pid)"
  fi
done < "$PIDFILE"

deadline=$((SECONDS + 10))
while any_alive && (( SECONDS < deadline )); do
  sleep 0.2
done

while read -r pid name; do
  [[ -n "${pid:-}" ]] || continue
  if kill -0 "$pid" 2>/dev/null; then
    kill -KILL "$pid" 2>/dev/null || true
    echo "KILL $name (pid $pid): did not stop within 10 s"
  fi
done < "$PIDFILE"

rm -f "$PIDFILE"
echo "stopped"
