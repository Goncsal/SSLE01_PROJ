#!/usr/bin/env bash
# Render the edge nginx configuration for one city on stdout.
# Usage: gateway/render.sh <city> --tls|--plain [--upstreams "h1:p1 h2:p2"]
# Environment: EDGE_RATE (2r/s), EDGE_BURST (20), EDGE_CONN (24).
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../infra/env.sh
. "$HERE/../infra/env.sh"

usage() { sed -n '2,5p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; }
[ "${1:-}" = "-h" ] || [ "${1:-}" = "--help" ] && { usage; exit 0; }

city="${1:-}"; [ -n "$city" ] || { usage >&2; exit 2; }
shift
mode=""; upstreams=""
while [ $# -gt 0 ]; do
  case "$1" in
    --tls) mode=tls ;;
    --plain) mode=plain ;;
    --upstreams) upstreams="${2:?--upstreams needs a value}"; shift ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
  shift
done
[ -n "$mode" ] || { echo "need --tls or --plain" >&2; exit 2; }
[[ "$city" =~ ^[a-z0-9-]+$ ]] || { echo "bad city name: $city" >&2; exit 2; }

if [ -z "$upstreams" ]; then
  for n in $K3S_NODES; do upstreams+="${upstreams:+ }$n.incus:30900"; done
fi
ups=""
for u in $upstreams; do
  ups+="    server $u max_fails=0;"$'\n'
done
ups="${ups%$'\n'}"

tls_bool=true; [ "$mode" = plain ] && tls_bool=false

awk -v mode="$mode" '
  /^#if /  { skip = ($2 != mode); next }
  /^#endif/{ skip = 0; next }
  !skip    { print }
' "$HERE/nginx.conf.tmpl" |
  sed -e "s|@CITY@|$city|g" -e "s|@MODE@|$mode|g" -e "s|@TLS_BOOL@|$tls_bool|g" \
      -e "s|@RATE@|${EDGE_RATE:-2r/s}|g" -e "s|@BURST@|${EDGE_BURST:-20}|g" \
      -e "s|@CONN@|${EDGE_CONN:-24}|g" |
  awk -v ups="$ups" '$0 == "@UPSTREAMS@" { print ups; next } { print }'
