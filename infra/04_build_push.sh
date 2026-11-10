#!/usr/bin/env bash
# Build the service images inside the `docker` instance, push them to the lab registry,
# mirror the third-party images (postgres, prometheus, alertmanager, grafana), and point the K3s nodes at the registry.
# Usage: infra/04_build_push.sh [service ...]   (default: every service dir with a Dockerfile)
# Idempotent: re-running rebuilds the tag and restarts K3s only if registries.yaml changed.
. "$(dirname "$0")/lib.sh"

CTX=/root/evcharge-ctx
TAG=${IMAGE_TAG:-$(date -u +%Y%m%d-%H%M)}
CONTEXT_PATHS="constraints.txt common registry csms billing fraud sim wazuh .dockerignore"
WAZUH_AGENT_TAG="${WAZUH_AGENT_TAG:-4.14.9}"

# service name -> Dockerfile path (the Wazuh agent image lives under wazuh/agents)
dockerfile_of() { case "$1" in wazuh-agent) echo wazuh/agents/Dockerfile ;; *) echo "$1/Dockerfile" ;; esac; }

# 1. the docker instance exists, is running and has docker
exists "$DOCKER_HOST_NAME" && running "$DOCKER_HOST_NAME" \
  || { bad "instance $DOCKER_HOST_NAME is not running (create it with the base infra script first)"; exit 1; }
incus exec "$DOCKER_HOST_NAME" -- sh -c 'command -v docker >/dev/null 2>&1' \
  || { bad "docker is not installed in $DOCKER_HOST_NAME (create it with the base infra script first)"; exit 1; }

# services to build
if [ "$#" -gt 0 ]; then
  SERVICES=("$@")
else
  SERVICES=()
  for d in "$ROOT"/*/; do
    [ -f "$d/Dockerfile" ] && SERVICES+=("$(basename "$d")")
  done
fi
for s in "${SERVICES[@]}"; do
  [ -f "$ROOT/$(dockerfile_of "$s")" ] || { bad "no $(dockerfile_of "$s")"; exit 1; }
done

# 2. docker trusts the lab registry over plain http; registry:2 runs on port 5000
log "docker host: insecure registry and registry container"
# check only; 01_base.sh docker/registry creates and merges daemon.json
if incus exec "$DOCKER_HOST_NAME" -- python3 -c 'import json,sys; sys.exit(0 if sys.argv[1] in json.load(open("/etc/docker/daemon.json")).get("insecure-registries",[]) else 1)' "$REGISTRY" 2>/dev/null \
   || incus exec "$DOCKER_HOST_NAME" -- sh -c "command -v jq >/dev/null && jq -e --arg r '$REGISTRY' '.\"insecure-registries\" // [] | index(\$r)' /etc/docker/daemon.json >/dev/null 2>&1"; then
  ok "daemon.json already trusts $REGISTRY"
elif incus exec "$DOCKER_HOST_NAME" -- sh -c 'command -v python3 >/dev/null'; then
  incus exec "$DOCKER_HOST_NAME" -- python3 -c 'import json,os,sys
p="/etc/docker/daemon.json"
try: d=json.load(open(p))
except Exception: d={}
l=d.setdefault("insecure-registries",[])
if sys.argv[1] not in l: l.append(sys.argv[1])
os.makedirs("/etc/docker",exist_ok=True)
json.dump(d,open(p,"w"),indent=2); open(p,"a").write("\n")' "$REGISTRY"
  incus exec "$DOCKER_HOST_NAME" -- systemctl restart docker
  ok "registry entry merged into daemon.json, docker restarted"
else
  bad "daemon.json lacks $REGISTRY and python3 is missing in $DOCKER_HOST_NAME; run infra/01_base.sh docker"; exit 1
fi
if ! incus exec "$DOCKER_HOST_NAME" -- docker inspect registry >/dev/null 2>&1; then
  incus exec "$DOCKER_HOST_NAME" -- docker run -d --restart=always -p 5000:5000 --name registry registry:2 >/dev/null
  ok "registry container created"
fi
incus exec "$DOCKER_HOST_NAME" -- docker start registry >/dev/null
[ "$(incus exec "$DOCKER_HOST_NAME" -- docker inspect -f '{{.State.Running}}' registry)" = true ] \
  || { bad "registry container is not running"; exit 1; }
ok "registry container running"

# 3. ship the build context (repo root subset) into the docker instance
log "build context -> $DOCKER_HOST_NAME:$CTX"
ctx_paths=()
for p in $CONTEXT_PATHS; do [ -e "$ROOT/$p" ] && ctx_paths+=("$p"); done
incus exec "$DOCKER_HOST_NAME" -- sh -c "rm -rf $CTX && mkdir -p $CTX"
tar -C "$ROOT" -cf - --exclude=.venv --exclude=__pycache__ --exclude=data --exclude=logs \
  "${ctx_paths[@]}" | incus exec "$DOCKER_HOST_NAME" -- tar -x -C "$CTX"
ok "context: ${ctx_paths[*]}"

# 4. build and push each service, two tags: $TAG and latest
log "build and push tag $TAG"
for s in "${SERVICES[@]}"; do
  img="$REGISTRY/evcharge-$s"
  start=$SECONDS
  itag=$TAG; [ "$s" = wazuh-agent ] && itag=$WAZUH_AGENT_TAG   # the agent image is tagged by package version
  incus exec "$DOCKER_HOST_NAME" -- sh -c "cd $CTX && docker build -f $(dockerfile_of "$s") -t $img:$itag -t $img:latest ."
  incus exec "$DOCKER_HOST_NAME" -- docker push "$img:$itag" >/dev/null
  incus exec "$DOCKER_HOST_NAME" -- docker push "$img:latest" >/dev/null
  ok "$s pushed as $img:$itag and :latest ($((SECONDS - start)) s)"
done

# 5. mirror the third-party images the cluster needs (exact versions, also named in k8s/)
MIRROR_IMAGES="postgres:17-alpine prom/prometheus:v3.5.0 prom/alertmanager:v0.28.1 grafana/grafana-oss:11.6.4"
for img in $MIRROR_IMAGES; do
  log "mirror $img"
  incus exec "$DOCKER_HOST_NAME" -- sh -c "docker pull -q $img >/dev/null && docker tag $img $REGISTRY/$img && docker push $REGISTRY/$img >/dev/null"
  ok "$REGISTRY/$img"
done

# 6. K3s nodes pull through the registry mirror (plain http, lab shortcut)
log "K3s registry mirror on: $K3S_NODES"
want=$(printf 'mirrors:\n  "%s":\n    endpoint:\n      - "http://%s"\n' "$REGISTRY" "$REGISTRY")
for n in $K3S_NODES; do
  have=$(incus exec "$n" -- sh -c 'cat /etc/rancher/k3s/registries.yaml 2>/dev/null' || true)
  if [ "$have" = "$want" ]; then
    ok "$n registries.yaml unchanged, no restart"
    continue
  fi
  printf '%s\n' "$want" | incus exec "$n" -- sh -c 'mkdir -p /etc/rancher/k3s && cat > /etc/rancher/k3s/registries.yaml'
  incus exec "$n" -- sh -c 'if systemctl cat k3s >/dev/null 2>&1; then systemctl restart k3s; else systemctl restart k3s-agent; fi'
  ok "$n registries.yaml written, K3s restarted"
done
for n in $K3S_NODES; do
  kc wait --for=condition=Ready "node/$n" --timeout=180s >/dev/null 2>&1 && ok "$n Ready" || bad "$n not Ready after restart"
done

# 7. report
log "catalog (from k3s-master)"
incus exec k3s-master -- curl -s "http://$REGISTRY/v2/_catalog"; echo
log "tag $TAG, image sizes"
for s in "${SERVICES[@]}"; do
  incus exec "$DOCKER_HOST_NAME" -- docker image ls --filter "reference=$REGISTRY/evcharge-$s:$TAG" --format '  {{.Repository}}:{{.Tag}}  {{.Size}}'
done
