#!/usr/bin/env bash
# Compute the baseline from the central Prometheus in a throwaway pod and write
# monitoring/rules/baseline.yml.   usage: run.sh [--window 24h] [--end ISO]
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
. "$ROOT/infra/lib.sh"

WINDOW=24h
END=()
while [ $# -gt 0 ]; do
  case "$1" in
    --window) WINDOW="$2"; shift 2 ;;
    --end) END=(--end "$2"); shift 2 ;;
    *) echo "usage: $0 [--window 24h] [--end ISO]" >&2; exit 2 ;;
  esac
done

IMAGE=docker.incus:5000/evcharge-sim:latest
PROM=http://prometheus.monitoring:9090
OUT="$ROOT/monitoring/rules/baseline.yml"
TMP="$(mktemp)"
trap 'rm -f "$TMP"' EXIT

# stdin of the pod: write the roster to a file, then the script itself
{
  printf "import base64\nopen('/tmp/roster.yaml','wb').write(base64.b64decode('%s'))\n" \
    "$(base64 -w0 "$ROOT/dev/roster.yaml")"
  cat "$HERE/compute.py"
} | kc -n monitoring run "baseline-$$" --rm -i --restart=Never --quiet --image "$IMAGE" \
    --image-pull-policy=Always -- python - \
    --prometheus "$PROM" --window "$WINDOW" "${END[@]}" --roster /tmp/roster.yaml > "$TMP"

grep -q '^groups:' "$TMP" || { echo "no baseline produced" >&2; exit 1; }
mv "$TMP" "$OUT"; trap - EXIT
log "wrote $OUT (window $WINDOW)"
echo "Load it:  infra/06_monitoring.sh   (reloads the rules ConfigMap and Prometheus)"
