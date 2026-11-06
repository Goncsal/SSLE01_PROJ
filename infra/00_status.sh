#!/usr/bin/env bash
# Health check of the lab: instances, K3s, registry, Wazuh, city networks.
. "$(dirname "$0")/lib.sh"
set +e
fail=0
check() { if eval "$2" >/dev/null 2>&1; then ok "$1"; else bad "$1"; fail=1; fi; }

log "mode: K3S_NODE=$K3S_NODE"
log "core instances"
for i in $K3S_NODES $DOCKER_HOST_NAME $WAZUH_HOST; do check "$i running ($(ipof "$i"))" "running $i"; done

log "kubernetes"
check "all nodes Ready" "[ \"\$(kc get nodes --no-headers | awk '\$2!=\"Ready\"' | wc -l)\" = 0 ] && kc get nodes --no-headers | grep -q ."
kc get nodes -o wide --no-headers 2>/dev/null | awk '{print "       " $1, $2, $6}'

log "registry"
check "$REGISTRY answers" "incus exec k3s-master -- curl -sf http://$REGISTRY/v2/_catalog"

log "wazuh"
for s in wazuh-manager wazuh-indexer wazuh-dashboard; do
  check "$s active" "incus exec $WAZUH_HOST -- systemctl is-active --quiet $s"
done

log "city networks"
for c in "${!CITY_NET[@]}"; do
  if incus network show "city-$c" >/dev/null 2>&1; then ok "city-$c exists"; else printf '  ---  city-%s not created yet\n' "$c"; fi
done

exit $fail
