#!/usr/bin/env bash
# Install and configure the edge gateway of one city. Idempotent.
# Usage: gateway/deploy_edge.sh <city> [--tls|--plain] [--instance NAME] [--pki-skip]
#                               [--upstreams "h1:p1 h2:p2"]
# Mode default: --plain for cities in PLAIN_WS_CITIES, else --tls.
# --pki-skip: certificates are already in /etc/evcharge/pki (tests).
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=../infra/lib.sh
. "$HERE/../infra/lib.sh"

city="${1:-}"
case "$city" in ""|-h|--help) sed -n '2,7p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; [ -n "$city" ] && exit 0; exit 2 ;; esac
shift
mode=""; inst="$city-edge"; pki=1; ups=()
while [ $# -gt 0 ]; do
  case "$1" in
    --tls) mode=tls ;;
    --plain) mode=plain ;;
    --pki-skip) pki=0 ;;
    --instance) inst="${2:?--instance needs a value}"; shift ;;
    --upstreams) ups=(--upstreams "${2:?--upstreams needs a value}"); shift ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
  shift
done
if [ -z "$mode" ]; then
  mode=tls
  for c in $PLAIN_WS_CITIES; do [ "$c" = "$city" ] && mode=plain; done
fi

exists "$inst" && running "$inst" || { echo "instance $inst must exist and run" >&2; exit 1; }
ix() { incus exec "$inst" -- "$@"; }

log "edge $city ($inst, $mode): packages"
if ! ix sh -c 'dpkg -s nginx prometheus-nginx-exporter prometheus-node-exporter >/dev/null 2>&1'; then
  ix sh -c 'apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq nginx prometheus-nginx-exporter prometheus-node-exporter >/dev/null'
fi
ix rm -f /etc/nginx/sites-enabled/default /etc/nginx/conf.d/default.conf
# one worker is plenty for a 1 CPU / 256 MiB edge (saves RAM)
ix sed -i 's/^worker_processes .*/worker_processes 1;/' /etc/nginx/nginx.conf
ix mkdir -p /var/log/evcharge /etc/evcharge/pki
ix chmod 0755 /var/log/evcharge
ix chmod 0750 /etc/evcharge/pki

# exporters: nginx exporter scrapes stub_status on loopback, listens on 9113
ix sh -c 'cat > /etc/default/prometheus-nginx-exporter' <<'CFG'
ARGS="--nginx.scrape-uri=http://127.0.0.1:8080/stub_status --web.listen-address=:9113"
CFG

if [ "$mode" = tls ] && [ "$pki" = 1 ]; then
  log "edge $city: certificates"
  "$ROOT/pki/export.sh" "pt.$city.edge" "$inst" --crl
fi
if [ "$mode" = tls ]; then
  for f in ca.crt crl.pem "pt.$city.edge.crt" "pt.$city.edge.key"; do
    ix test -s "/etc/evcharge/pki/$f" || { echo "missing /etc/evcharge/pki/$f in $inst" >&2; exit 1; }
  done
fi

log "edge $city: configuration"
conf=/etc/nginx/conf.d/evcharge.conf
ix sh -c "[ -f $conf ] && cp $conf $conf.prev || true"
"$HERE/render.sh" "$city" "--$mode" "${ups[@]}" | ix sh -c "cat > $conf.new && mv $conf.new $conf"
if ! ix nginx -t 2>&1; then
  ix sh -c "[ -f $conf.prev ] && mv $conf.prev $conf || rm -f $conf"
  echo "nginx -t failed, previous configuration restored" >&2; exit 1
fi
ix rm -f $conf.prev
ix systemctl enable nginx prometheus-nginx-exporter prometheus-node-exporter >/dev/null 2>&1
ix systemctl restart prometheus-nginx-exporter
ix systemctl start prometheus-node-exporter
ix systemctl is-active --quiet nginx && ix systemctl reload nginx || ix systemctl restart nginx

st="$(ix systemctl is-active nginx prometheus-nginx-exporter prometheus-node-exporter | tr '\n' ' ')"
echo "edge $city: mode=$mode instance=$inst services(nginx,nginx-exporter,node-exporter)=$st"
