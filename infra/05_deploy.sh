#!/usr/bin/env bash
# Deploy the central platform (Postgres, registry, billing, CSMS) to K3s.
#   [IMAGE_TAG=20261106-1200] infra/05_deploy.sh
# Order: infra scripts 01-04 (cluster up) -> images pushed -> this script.
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

IMAGE_TAG="${IMAGE_TAG:-latest}"
FAILED=0
REMOTE_DIR=/root/evcharge-k8s
NS=evcharge

log "checking nodes"
for n in $K3S_NODES; do
  st="$(kc get node "$n" -o jsonpath='{.status.conditions[?(@.type=="Ready")].status}' 2>/dev/null || true)"
  if [ "$st" = True ]; then ok "$n Ready"; else bad "$n not Ready"; exit 1; fi
done

"$ROOT/infra/secrets_to_k8s.sh"

log "roster ConfigMap"
incus file push --mode 644 "$ROOT/dev/roster.yaml" k3s-master/root/roster.yaml >/dev/null
incus exec k3s-master -- sh -c "kubectl -n $NS create configmap roster --from-file=roster.yaml=/root/roster.yaml --dry-run=client -o yaml \
  | kubectl label --local -f - app.kubernetes.io/name=roster app.kubernetes.io/part-of=evcharge -o yaml \
  | kubectl apply -f - && rm -f /root/roster.yaml" >/dev/null
ok "roster"

log "csms-edge ConfigMap (created once; the city setup owns its values afterwards)"
if kc -n "$NS" get configmap csms-edge >/dev/null 2>&1; then
  ok "csms-edge exists, left as it is"
else
  kc -n "$NS" create configmap csms-edge --from-literal=CSMS_ALLOW_DIRECT=true \
    --from-literal=CSMS_TRUSTED_PROXIES= >/dev/null
  kc -n "$NS" label configmap csms-edge app.kubernetes.io/name=csms-edge \
    app.kubernetes.io/part-of=evcharge >/dev/null
  ok "csms-edge created (direct connections allowed, no proxies)"
fi

log "pushing k8s/ to k3s-master:$REMOTE_DIR (tag $IMAGE_TAG)"
incus exec k3s-master -- rm -rf "$REMOTE_DIR"
incus exec k3s-master -- mkdir -p "$REMOTE_DIR"
incus file push -r "$ROOT/k8s/." "k3s-master$REMOTE_DIR/" >/dev/null
incus exec k3s-master -- sed -i "s/^\( *newTag:\) .*/\1 $IMAGE_TAG/" "$REMOTE_DIR/kustomization.yaml"

log "kubectl apply -k"
kc apply -k "$REMOTE_DIR"

log "waiting for postgres"
kc -n "$NS" rollout status statefulset/postgres --timeout=300s

for d in registry billing csms correlator; do
  if [ "$IMAGE_TAG" = latest ]; then
    kc -n "$NS" rollout restart deployment/"$d" >/dev/null
  fi
  kc -n "$NS" rollout status deployment/"$d" --timeout=300s || { bad "$d not ready (image missing?)"; FAILED=1; }
done

log "state"
kc -n "$NS" get pods -o wide
kc -n "$NS" get svc
M="$(ipof k3s-master)"; N="$(ipof k3s-node1)"
cat <<URLS

NodePort URLs (a node answers csms-ocpp only if it runs a csms pod):
  csms API     http://$M:30800   http://$N:30800
  registry     http://$M:30801   http://$N:30801
  billing      http://$M:30802   http://$N:30802
  csms OCPP    ws://$M:30900/ocpp/<hier_id>   ws://$N:30900/ocpp/<hier_id>
URLS

[ "$FAILED" = 0 ] || exit 1
