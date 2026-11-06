#!/usr/bin/env bash
# Deploy the central monitoring (Prometheus, Alertmanager, Grafana) to K3s, namespace monitoring.
#   infra/06_monitoring.sh [--direct-scrape on|off] [--reload]
# - renders monitoring/central/prometheus.yml.tmpl (one federate-<city> job per CITY_NET entry;
#   the direct-<city> jobs of contract 14.6 only with --direct-scrape on, default off)
# - packages whatever exists in monitoring/rules, monitoring/alertmanager and monitoring/grafana
#   into ConfigMaps (all optional: the script works while those directories are empty)
# - applies k8s/monitoring, waits for the rollouts and reloads a component whose config changed
#   (--reload forces the reload). Idempotent: a second run changes nothing.
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

DIRECT=off
FORCE_RELOAD=0
while [ "$#" -gt 0 ]; do
  case "$1" in
    --direct-scrape) DIRECT="${2:-}"; shift 2 ;;
    --reload) FORCE_RELOAD=1; shift ;;
    *) echo "usage: $0 [--direct-scrape on|off] [--reload]" >&2; exit 2 ;;
  esac
done
case "$DIRECT" in on|off) ;; *) echo "--direct-scrape takes on or off" >&2; exit 2 ;; esac

NS=monitoring
REMOTE_K8S=/root/evcharge-k8s-monitoring
STAGE=/root/.monitoring-stage
MON="$ROOT/monitoring"
WORK="$(mktemp -d)"
cleanup() { rm -rf "$WORK"; incus exec k3s-master -- rm -rf "$STAGE" >/dev/null 2>&1 || true; }
trap cleanup EXIT

log "checking nodes"
for n in $K3S_NODES; do
  st="$(kc get node "$n" -o jsonpath='{.status.conditions[?(@.type=="Ready")].status}' 2>/dev/null || true)"
  if [ "$st" = True ]; then ok "$n Ready"; else bad "$n not Ready"; exit 1; fi
done

# Secret grafana-env (also creates the namespace)
"$ROOT/infra/secrets_to_k8s.sh"

# ---- 1. stage the ConfigMap contents ----------------------------------------------------------
mkdir -p "$WORK/prometheus-config" "$WORK/prometheus-rules" "$WORK/alertmanager-config" \
         "$WORK/grafana-datasources" "$WORK/grafana-dashboards" "$WORK/grafana-dashboard-provider"

log "rendering prometheus.yml (direct scrape $DIRECT)"
cities="$(printf '%s\n' "${!CITY_NET[@]}" | sort | tr '\n' ' ')"
python3 - "$MON/central/prometheus.yml.tmpl" "$WORK/prometheus-config/prometheus.yml" "$DIRECT" $cities <<'PY'
import re, sys
tmpl, out, direct, cities = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4:]
text = open(tmpl).read()
def block(m):
    name, body = m.group(1), m.group(2)
    if name == "direct" and direct != "on":
        return ""
    return "".join(body.replace("@CITY@", c) for c in cities)
text = re.sub(r"# @@BEGIN (\w+)@@\n(.*?)# @@END \1@@\n", block, text, flags=re.S)
open(out, "w").write(text)
PY
ok "cities: $cities"

n=0
for f in "$MON"/rules/*.yml; do [ -f "$f" ] && { cp "$f" "$WORK/prometheus-rules/"; n=$((n+1)); }; done
ok "$n rule file(s)"

if [ -f "$MON/alertmanager/alertmanager.yml" ]; then
  cp "$MON/alertmanager/alertmanager.yml" "$WORK/alertmanager-config/alertmanager.yml"
  ok "alertmanager.yml from monitoring/alertmanager"
else
  cat > "$WORK/alertmanager-config/alertmanager.yml" <<'YML'
# Default until monitoring/alertmanager/alertmanager.yml exists (contract 14.4).
route:
  receiver: default
  group_by: [alertname, city, hier_id]
  group_wait: 10s
  group_interval: 1m
  repeat_interval: 4h
receivers:
  - name: default
inhibit_rules:
  - source_matchers: [alertname="EdgeDown"]
    target_matchers: [alertname=~"ChargerOffline|StationDown|ChargerFlapping|MsgRateAnomaly|CityLoadSpike"]
    equal: [city]
  - source_matchers: [alertname="CSMSDown"]
    target_matchers: [alertname=~"ChargerOffline|ChargerFlapping"]
YML
  ok "alertmanager.yml: built-in default"
fi

ds=0
for f in "$MON"/grafana/datasources/*.y*ml "$MON"/grafana/provisioning/datasources/*.y*ml; do
  [ -f "$f" ] && { cp "$f" "$WORK/grafana-datasources/$(basename "$f")"; ds=$((ds+1)); }
done
if [ "$ds" -eq 0 ]; then
  {
    printf 'apiVersion: 1\ndatasources:\n'
    printf '  - name: central\n    uid: central\n    type: prometheus\n    access: proxy\n'
    printf '    url: http://prometheus.monitoring.svc:9090\n    isDefault: true\n'
    printf '    jsonData:\n      timeInterval: 15s\n'
    for c in $cities; do
      printf '  - name: city-%s\n    uid: city-%s\n    type: prometheus\n    access: proxy\n' "$c" "$c"
      printf '    url: http://edge.%s.pt.lab:9090\n    jsonData:\n      timeInterval: 5s\n' "$c"
    done
  } > "$WORK/grafana-datasources/datasources.yaml"
  ok "datasources: central + one per city (generated)"
else
  ok "datasources: $ds file(s) from monitoring/grafana"
fi

# dashboard provider: the file from monitoring/grafana if present, else a default
prov=0
for f in "$MON"/grafana/provisioning/dashboards/*.y*ml; do
  [ -f "$f" ] && { cp "$f" "$WORK/grafana-dashboard-provider/$(basename "$f")"; prov=$((prov+1)); }
done
if [ "$prov" -eq 0 ]; then
  cat > "$WORK/grafana-dashboard-provider/dashboards.yaml" <<'YML'
apiVersion: 1
providers:
  - name: evcharge
    orgId: 1
    type: file
    disableDeletion: true
    allowUiUpdates: false
    updateIntervalSeconds: 30
    options:
      path: /var/lib/grafana/dashboards
YML
fi

d=0
if [ -d "$MON/grafana" ]; then
  while IFS= read -r f; do cp "$f" "$WORK/grafana-dashboards/$(basename "$f")"; d=$((d+1)); done \
    < <(find "$MON/grafana" -name '*.json' -type f | sort)
fi
ok "$d dashboard file(s)"
size="$(du -sb "$WORK/grafana-dashboards" | cut -f1)"
[ "$size" -lt 1000000 ] || { bad "dashboards are $size bytes, over the ConfigMap limit"; exit 1; }

# ---- 2. push and apply ConfigMaps ---------------------------------------------------------------
dirhash() { (cd "$WORK/$1" && find . -type f | sort | xargs -r sha256sum | sha256sum | cut -c1-16); }

incus exec k3s-master -- sh -c "rm -rf $STAGE && mkdir -p $STAGE"
tar -C "$WORK" -cf - . | incus exec k3s-master -- tar -x -m -C "$STAGE"

had_prom=0; kc -n "$NS" get deployment prometheus >/dev/null 2>&1 && had_prom=1
had_am=0;   kc -n "$NS" get deployment alertmanager >/dev/null 2>&1 && had_am=1
had_gf=0;   kc -n "$NS" get deployment grafana >/dev/null 2>&1 && had_gf=1
kc get namespace "$NS" >/dev/null

declare -A changed
log "ConfigMaps"
for cm in prometheus-config prometheus-rules alertmanager-config grafana-datasources grafana-dashboard-provider grafana-dashboards; do
  h="$(dirhash "$cm")"
  old="$(kc -n "$NS" get configmap "$cm" -o jsonpath="{.metadata.annotations['content-hash']}" 2>/dev/null || true)"
  if [ "$old" = "$h" ]; then
    changed[$cm]=0; ok "$cm unchanged"
  else
    changed[$cm]=1
    incus exec k3s-master -- sh -c "kubectl -n $NS create configmap $cm --from-file=$STAGE/$cm --dry-run=client -o yaml \
      | kubectl label --local -f - app.kubernetes.io/name=${cm%%-*} app.kubernetes.io/part-of=evcharge -o yaml \
      | kubectl annotate --local -f - content-hash=$h -o yaml \
      | kubectl apply --server-side --force-conflicts -f -" >/dev/null
    ok "$cm applied"
  fi
done

log "kubectl apply -k k8s/monitoring"
incus exec k3s-master -- rm -rf "$REMOTE_K8S"
incus exec k3s-master -- mkdir -p "$REMOTE_K8S"
incus file push -r "$ROOT/k8s/monitoring/." "k3s-master$REMOTE_K8S/" >/dev/null
kc apply -k "$REMOTE_K8S"

FAILED=0
for d in prometheus alertmanager grafana; do
  kc -n "$NS" rollout status deployment/"$d" --timeout=300s || { bad "$d not ready"; FAILED=1; }
done

# ---- 3. reload what changed ---------------------------------------------------------------------
# A mounted ConfigMap reaches the pod after the kubelet sync (up to about a minute): wait until the
# files in the pod match the staged ones, then ask the component to reload.
inpod_sum() { # deployment dir
  kc -n "$NS" exec "deploy/$1" -- sh -c "cd $2 && for f in *.y*ml; do [ -e \"\$f\" ] && sha256sum \"\$f\"; done | sort -k2" 2>/dev/null || true
}
reload_component() { # deployment, port, mount dir, staged cm dir
  local want got i
  want="$(cd "$WORK/$4" && sha256sum ./*.y*ml 2>/dev/null | sed 's# \./# #' | sort -k2 || true)"
  for i in $(seq 1 40); do
    got="$(inpod_sum "$1" "$3")"
    [ "$got" = "$want" ] && break
    sleep 5
  done
  [ "$got" = "$want" ] || { bad "$1: mounted config did not reach the pod in time"; FAILED=1; return; }
  if kc -n "$NS" exec "deploy/$1" -- wget -q -O /dev/null --post-data= "http://127.0.0.1:$2/-/reload"; then
    ok "$1 reloaded"
  else
    bad "$1 reload failed (check the config)"; FAILED=1
  fi
}

if [ "$had_prom" = 1 ] && { [ "$FORCE_RELOAD" = 1 ] || [ "${changed[prometheus-config]}" = 1 ]; }; then
  reload_component prometheus 9090 /etc/prometheus-config prometheus-config
fi
if [ "$had_prom" = 1 ] && { [ "$FORCE_RELOAD" = 1 ] || [ "${changed[prometheus-rules]}" = 1 ]; } \
   && [ "$(ls "$WORK/prometheus-rules" | wc -l)" -gt 0 ]; then
  reload_component prometheus 9090 /etc/prometheus/rules prometheus-rules
elif [ "$had_prom" = 1 ] && [ "${changed[prometheus-rules]}" = 1 ]; then
  sleep 70   # all rule files were removed: let the kubelet sync, then reload
  kc -n "$NS" exec deploy/prometheus -- wget -q -O /dev/null --post-data= http://127.0.0.1:9090/-/reload && ok "prometheus reloaded"
fi
if [ "$had_am" = 1 ] && { [ "$FORCE_RELOAD" = 1 ] || [ "${changed[alertmanager-config]}" = 1 ]; }; then
  reload_component alertmanager 9093 /etc/alertmanager alertmanager-config
fi
if [ "$had_gf" = 1 ] && { [ "${changed[grafana-datasources]}" = 1 ] || [ "${changed[grafana-dashboard-provider]}" = 1 ]; }; then
  kc -n "$NS" rollout restart deployment/grafana >/dev/null
  kc -n "$NS" rollout status deployment/grafana --timeout=300s || FAILED=1
  ok "grafana restarted (datasources or provider changed; dashboards are picked up every 30 s)"
fi

log "state"
kc -n "$NS" get pods,pvc,svc -o wide
N="$(ipof k3s-node1)"; M="$(ipof k3s-master)"
cat <<URLS

direct scrape: $DIRECT
NodePort URLs (any node answers):
  Prometheus    http://$N:30090   http://$M:30090
  Alertmanager  http://$N:30093
  Grafana       http://$N:30300   (user admin, password GRAFANA_ADMIN_PASSWORD in secrets/platform.env)
URLS

[ "$FAILED" = 0 ] || exit 1
