#!/usr/bin/env bash
# City Prometheus on the edge (contract 14.1). Idempotent.
#
#   monitoring/city/deploy_city_prom.sh <city>|all
#
# Installs the Debian prometheus package in <city>-edge (without recommends, so no second
# node exporter), writes the flags, renders the config, checks it with promtool, enables and
# reloads the service and raises the edge memory limit. Only `incus` and bash are used on
# the host.
. "$(dirname "${BASH_SOURCE[0]}")/../../infra/lib.sh"

DIR="$ROOT/monitoring/city"
SD_REGISTRY="${SD_REGISTRY:-k3s-master.incus:30801}"
EDGE_MEM="${EDGE_MEM:-384MiB}"
ARGS_LINE='ARGS="--web.listen-address=0.0.0.0:9090 --storage.tsdb.retention.time=12h --storage.tsdb.retention.size=256MB"'

ix() { local i="$1"; shift; incus exec "$i" -- "$@"; }

# push stdin to a file only when the content differs; returns 0 if it changed
put_if_changed() { # <instance> <path> <mode>
  local inst="$1" path="$2" mode="$3" tmp new old
  tmp="$(mktemp)"; cat > "$tmp"
  new=$(sha256sum < "$tmp" | cut -d' ' -f1)
  old=$(ix "$inst" sh -c "[ -f '$path' ] && sha256sum < '$path' | cut -d' ' -f1 || true")
  if [ "$new" != "$old" ]; then
    incus file push -q --mode "$mode" "$tmp" "$inst$path"; rm -f "$tmp"; return 0
  fi
  rm -f "$tmp"; return 1
}

deploy() {
  local city="$1" edge="$1-edge" changed=0 cfg=0
  exists "$edge" || { bad "$edge missing"; return 1; }
  running "$edge" || { bad "$edge not running"; return 1; }

  # memory limit (live, no restart)
  if [ "$(incus config get "$edge" limits.memory)" != "$EDGE_MEM" ]; then
    incus config set "$edge" limits.memory="$EDGE_MEM"
    ok "$edge: limits.memory=$EDGE_MEM"
  fi

  if ! ix "$edge" test -x /usr/bin/prometheus -a -x /usr/bin/promtool; then
    log "$edge: installing prometheus"
    ix "$edge" sh -c 'export DEBIAN_FRONTEND=noninteractive
      apt-get update -qq && apt-get install -y -qq --no-install-recommends prometheus >/dev/null
      apt-get clean; rm -rf /var/lib/apt/lists/*'
  fi

  printf '%s\n' "$ARGS_LINE" | put_if_changed "$edge" /etc/default/prometheus 0644 && changed=1
  sed -e "s|@CITY@|$city|g" -e "s|@REGISTRY@|$SD_REGISTRY|g" "$DIR/prometheus.yml.tmpl" \
    | put_if_changed "$edge" /etc/prometheus/prometheus.yml 0644 && cfg=1
  put_if_changed "$edge" /etc/prometheus/city.rules.yml 0644 < "$DIR/city.rules.yml" && cfg=1

  ix "$edge" promtool check config /etc/prometheus/prometheus.yml >/dev/null \
    || { bad "$edge: promtool check config failed"; return 1; }

  ix "$edge" systemctl enable prometheus >/dev/null 2>&1
  if [ "$changed" = 1 ] || ! ix "$edge" systemctl is-active --quiet prometheus; then
    ix "$edge" systemctl restart prometheus
    ok "$edge: prometheus (re)started"
  elif [ "$cfg" = 1 ]; then
    ix "$edge" systemctl reload prometheus
    ok "$edge: prometheus reloaded"
  else
    ok "$edge: prometheus unchanged"
  fi
}

[ $# -eq 1 ] || { echo "usage: $0 <city>|all" >&2; exit 2; }
if [ "$1" = all ]; then
  mapfile -t cities < <(printf '%s\n' "${!CITY_NET[@]}" | sort)
else
  [ -n "${CITY_NET[$1]:-}" ] || { echo "unknown city: $1" >&2; exit 2; }
  cities=("$1")
fi
for c in "${cities[@]}"; do deploy "$c"; done
