#!/usr/bin/env bash
# Base platform: the K3s nodes (k3s-master, k3s-node1), the docker host with the lab registry,
# and the K3s <-> registry wiring. Only `incus` (and `sudo sysctl` in container mode) is needed.
#
# Usage: infra/01_base.sh [all|profiles|nodes|k3s|docker|registry|check] [--dry-run]
#   profiles  container mode: profile `k3s`; vm mode: profile `vm-lab`
#   nodes     launch k3s-master, k3s-node1, docker (and host/VM sysctls)
#   docker    Docker CE inside the docker instance
#   registry  insecure-registries in daemon.json, registry:2 container, registries.yaml on the nodes
#   k3s       K3s server on k3s-master, agent join on k3s-node1
#   check     read-only health check, exit 1 on failure
#   all       profiles, nodes, docker, registry, k3s, check
# --dry-run prints every incus/sudo command that would change something and changes nothing.
#
# Mode (K3S_NODE, infra/env.sh):
#   container  privileged Incus containers with the course guide's `k3s` profile (Debian VM host)
#   vm         Incus VMs with profile `vm-lab` (bare-metal host, where a privileged container is
#              root on the host; see the "Deviation" section of the guide G2)
# Every step is idempotent: an existing and correct object is reported ok and left alone.
# An explicit K3S_NODE=... on the command line wins over infra/local.env (which env.sh sources last).
CALLER_MODE="${K3S_NODE:-}"
. "$(dirname "$0")/lib.sh"
[ -z "$CALLER_MODE" ] || K3S_NODE=$CALLER_MODE

STEP=all; DRY=0
for a in "$@"; do
  case "$a" in
    --dry-run) DRY=1 ;;
    all|profiles|nodes|k3s|docker|registry|check) STEP=$a ;;
    *) echo "usage: $0 [all|profiles|nodes|k3s|docker|registry|check] [--dry-run]" >&2; exit 2 ;;
  esac
done

DOCKER_MEM="${DOCKER_MEM:-2GiB}"
DOCKER_CPU="${DOCKER_CPU:-2}"
# K3s arguments for nodes running inside LXC containers:
#  - kube-proxy cannot write nf_conntrack_max from a container, so it must not try (0 = leave it)
#  - on some storage pools (ZFS) overlayfs does not work inside the container: set K3S_SNAPSHOTTER=native
# In vm mode the nodes are real kernels, so neither is needed.
if [ "$K3S_NODE" = vm ]; then K3S_CONNTRACK0="${K3S_CONNTRACK0:-0}"; else K3S_CONNTRACK0="${K3S_CONNTRACK0:-1}"; fi
K3S_SNAPSHOTTER="${K3S_SNAPSHOTTER:-}"
K3S_VERSION="${K3S_VERSION:-}"            # empty = latest stable channel
LXC_EXTRA="${LXC_EXTRA:-}"                # extra raw.lxc lines, e.g. "lxc.mount.auto=proc:rw sys:rw cgroup:rw"
SYSCTL_CONF=$'vm.overcommit_memory=1\nkernel.panic=10\nkernel.panic_on_oops=1'

# --- helpers ---------------------------------------------------------------------------
run() {  # run a changing command, or print it in dry-run mode
  if [ "$DRY" = 1 ]; then printf '  [dry-run]'; printf ' %q' "$@"; printf '\n'; else "$@"; fi
}
rsh() {  # rsh <instance> <script>: run a shell script inside an instance
  run incus exec "$1" -- bash -c "$2"
}
ix() { incus exec "$1" -- bash -c "$2"; }   # read-only query inside an instance
active() { ix "$1" "systemctl is-active --quiet $2" >/dev/null 2>&1; }
kind() { incus list "^$1\$" -f csv -c t | tr 'A-Z' 'a-z'; }

want_registries() { printf 'mirrors:\n  "%s":\n    endpoint:\n      - "http://%s"\n' "$REGISTRY" "$REGISTRY"; }

# --- profiles --------------------------------------------------------------------------
step_profiles() {
  log "profiles ($K3S_NODE mode)"
  if [ "$K3S_NODE" = vm ]; then
    if incus profile show vm-lab >/dev/null 2>&1; then
      ok "profile vm-lab exists (left untouched, instances carry their own limits)"
    else
      # Deviation from G2: no limits on the profile; cpu/memory are set per instance at launch.
      run incus profile create vm-lab
      run incus profile device add vm-lab root disk path=/ pool=default size=20GiB
      run incus profile device add vm-lab eth0 nic network="$BRIDGE" name=eth0
    fi
    return
  fi
  local raw
  # The guide's profile. Deviation: the guide's `lxc.cgroup.devices.allow=a *:* rwm` is a cgroup v1
  # key and Incus rejects it on a cgroup v2 host; `lxc.cgroup2.devices.allow=a *:* rwm` fails to
  # load the BPF device program; only `lxc.cgroup2.devices.allow=a` works.
  raw=$'lxc.apparmor.profile=unconfined\nlxc.cgroup2.devices.allow=a\nlxc.cap.drop='
  [ -z "$LXC_EXTRA" ] || raw+=$'\n'"$LXC_EXTRA"
  if incus profile show k3s >/dev/null 2>&1; then
    if [ "$(incus profile get k3s security.privileged)" = true ] \
       && [ "$(incus profile get k3s security.nesting)" = true ] \
       && incus profile get k3s raw.lxc | grep -qx 'lxc.cgroup2.devices.allow=a' \
       && incus profile device show k3s | grep -q '^kmsg:'; then
      ok "profile k3s exists and matches"
    else
      bad "profile k3s exists but differs from the expected one; fix or delete it by hand (instances use it)"
      return 1
    fi
    return
  fi
  run incus profile create k3s
  run incus profile set k3s security.privileged=true security.nesting=true \
      linux.kernel_modules=ip_tables,ip6_tables,nf_nat,overlay,br_netfilter
  run incus profile set k3s raw.lxc="$raw"
  # K3s (kubelet) logs through /dev/kmsg, which a container does not have by default.
  run incus profile device add k3s kmsg unix-char source=/dev/kmsg path=/dev/kmsg
}

# --- host sysctls (container mode: the nodes share the host kernel) --------------------
host_sysctl() {
  [ "$K3S_NODE" = vm ] && return 0
  log "host sysctls (containers share the host kernel)"
  local f=/etc/sysctl.d/90-k3s.conf
  if [ -f "$f" ] && [ "$(cat "$f")" = "$SYSCTL_CONF" ] \
     && [ "$(sysctl -n vm.overcommit_memory)" = 1 ] && [ "$(sysctl -n kernel.panic)" = 10 ] \
     && [ "$(sysctl -n kernel.panic_on_oops)" = 1 ]; then
    ok "$f present and active"; return 0
  fi
  local S=""; [ "$(id -u)" = 0 ] || S=sudo
  if [ "$DRY" = 0 ] && [ -n "$S" ] && ! command -v sudo >/dev/null 2>&1; then
    bad "sudo not available; as root run:"
    printf '  printf "vm.overcommit_memory=1\\nkernel.panic=10\\nkernel.panic_on_oops=1\\n" > %s\n  sysctl --system\n' "$f"
    return 1
  fi
  if [ "$DRY" = 1 ]; then
    printf '  [dry-run] printf %q | %s tee %s\n' "$SYSCTL_CONF"$'\n' "${S:-}" "$f"
    printf '  [dry-run] %s sysctl --system\n' "${S:-}"
  else
    printf '%s\n' "$SYSCTL_CONF" | $S tee "$f" >/dev/null
    $S sysctl --system >/dev/null
  fi
  [ "$DRY" = 1 ] || ok "sysctls set (vm.overcommit_memory=1 kernel.panic=10 kernel.panic_on_oops=1)"
}

# --- nodes -----------------------------------------------------------------------------
launch_one() {  # launch_one <name> [extra incus launch args...]
  local n=$1; shift
  if exists "$n"; then
    local k want=container; [ "$K3S_NODE" = vm ] && want=virtual-machine
    k=$(kind "$n")
    [ "$k" = "$want" ] || printf '  note %s is a %s, K3S_NODE=%s expects a %s (left as is)\n' "$n" "$k" "$K3S_NODE" "$want"
    if [ "$DRY" = 1 ]; then  # show what a fresh host would run
      # shellcheck disable=SC2046
      printf '  [dry-run] (only if missing)'; printf ' %q' incus launch "$IMAGE" "$n" $(node_launch_flags) "$@"; printf '\n'
    fi
    if running "$n"; then ok "$n exists and runs"; else run incus start "$n"; ok "$n started"; fi
    return
  fi
  # shellcheck disable=SC2046  # flags are a word list on purpose
  run incus launch "$IMAGE" "$n" $(node_launch_flags) "$@"
  [ "$DRY" = 1 ] || wait_ready "$n"
  ok "$n launched"
}

node_sysctl() {  # vm mode: the guide's sysctls inside each VM (they have their own kernel)
  [ "$K3S_NODE" = vm ] || return 0
  local n
  for n in $K3S_NODES; do
    if ix "$n" '[ "$(sysctl -n vm.overcommit_memory)" = 1 ] && [ "$(sysctl -n kernel.panic)" = 10 ] && [ "$(sysctl -n kernel.panic_on_oops)" = 1 ]' 2>/dev/null; then
      ok "$n sysctls already set"
    else
      rsh "$n" "printf 'vm.overcommit_memory=1\nkernel.panic=10\nkernel.panic_on_oops=1\n' > /etc/sysctl.d/90-k3s.conf && sysctl --system >/dev/null"
      ok "$n sysctls set"
    fi
  done
}

step_nodes() {
  host_sysctl
  log "nodes: $K3S_NODES $DOCKER_HOST_NAME"
  local n
  for n in $K3S_NODES; do
    launch_one "$n" -c limits.memory="$K3S_MEM" -c limits.cpu="$K3S_CPU"
  done
  if [ "$K3S_NODE" = vm ]; then
    launch_one "$DOCKER_HOST_NAME" -c limits.memory="$DOCKER_MEM" -c limits.cpu="$DOCKER_CPU"
  else
    # dockerd inside a container needs nesting (the guide sets it on the docker instance)
    launch_one "$DOCKER_HOST_NAME" -c security.nesting=true
  fi
  node_sysctl
  for n in $K3S_NODES; do
    if ix "$n" 'command -v curl >/dev/null'; then ok "$n has curl"; else
      rsh "$n" 'export DEBIAN_FRONTEND=noninteractive; apt-get update -y && apt-get install -y curl ca-certificates'; ok "$n curl installed"
    fi
  done
}

# --- docker ----------------------------------------------------------------------------
step_docker() {
  log "docker host: Docker CE in $DOCKER_HOST_NAME"
  if exists "$DOCKER_HOST_NAME" && ix "$DOCKER_HOST_NAME" 'command -v docker >/dev/null && command -v python3 >/dev/null'; then
    ok "docker and python3 present"; return 0
  fi
  [ "$DRY" = 1 ] || exists "$DOCKER_HOST_NAME" || { bad "no $DOCKER_HOST_NAME instance (run the nodes step)"; return 1; }
  rsh "$DOCKER_HOST_NAME" '
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -y && apt-get install -y curl ca-certificates gnupg python3-minimal
    if ! command -v docker >/dev/null; then
      install -m 0755 -d /etc/apt/keyrings
      curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc
      chmod a+r /etc/apt/keyrings/docker.asc
      echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/debian $(. /etc/os-release && echo $VERSION_CODENAME) stable" > /etc/apt/sources.list.d/docker.list
      apt-get update -y
      apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
      systemctl enable --now docker
    fi'
  ok "docker installed"
}

# --- registry --------------------------------------------------------------------------
PY_HAS='import json,sys
try: d=json.load(open("/etc/docker/daemon.json"))
except Exception: d={}
sys.exit(0 if sys.argv[1] in d.get("insecure-registries",[]) else 1)'
PY_MERGE='import json,sys,os
p="/etc/docker/daemon.json"
try: d=json.load(open(p))
except Exception: d={}
l=d.setdefault("insecure-registries",[])
if sys.argv[1] not in l: l.append(sys.argv[1])
os.makedirs("/etc/docker",exist_ok=True)
json.dump(d,open(p,"w"),indent=2); open(p,"a").write("\n")'

ensure_registries() {  # ensure_registries <node>: write registries.yaml, restart K3s only if it changed
  local n=$1 have svc=""
  have=$(ix "$n" 'cat /etc/rancher/k3s/registries.yaml 2>/dev/null' || true)
  if [ "$have" = "$(want_registries)" ]; then ok "$n registries.yaml unchanged, no restart"; return 0; fi
  if [ "$DRY" = 1 ]; then
    printf '  [dry-run] write %s:/etc/rancher/k3s/registries.yaml (mirror %s -> http://%s)\n' "$n" "$REGISTRY" "$REGISTRY"
  else
    want_registries | incus exec "$n" -- bash -c 'mkdir -p /etc/rancher/k3s && cat > /etc/rancher/k3s/registries.yaml'
  fi
  active "$n" k3s && svc=k3s
  active "$n" k3s-agent && svc=k3s-agent
  if [ -n "$svc" ]; then rsh "$n" "systemctl restart $svc"; ok "$n registries.yaml written, $svc restarted"
  else ok "$n registries.yaml written (K3s not running yet, no restart needed)"; fi
}

step_registry() {
  log "registry on $DOCKER_HOST_NAME ($REGISTRY)"
  local d=$DOCKER_HOST_NAME reg=${REGISTRY}
  if exists "$d" && ix "$d" 'command -v docker >/dev/null'; then
    if ix "$d" "python3 -c '$PY_HAS' '$reg'"; then
      ok "daemon.json already trusts $reg"
    else
      # Deviation from G2 (which overwrote the file): merge, keep any other daemon settings.
      rsh "$d" "python3 -c '$PY_MERGE' '$reg' && systemctl restart docker"
      ok "daemon.json merged, docker restarted"
    fi
    if ix "$d" 'docker inspect registry >/dev/null 2>&1'; then
      if [ "$(ix "$d" "docker inspect -f '{{.State.Running}}' registry")" = true ]; then ok "registry container running"
      else rsh "$d" 'docker start registry'; ok "registry container started"; fi
    else
      rsh "$d" 'docker run -d --restart=always -p 5000:5000 --name registry registry:2'
      ok "registry container created"
    fi
  elif [ "$DRY" = 1 ]; then
    printf '  [dry-run] (docker step first) merge insecure-registries into daemon.json, run registry:2 --restart=always -p 5000:5000\n'
  else
    bad "docker is not installed in $d (run the docker step)"; return 1
  fi
  local n
  for n in $K3S_NODES; do
    if exists "$n"; then ensure_registries "$n"; fi
  done
}

# --- k3s -------------------------------------------------------------------------------
step_k3s() {
  log "K3s: server on k3s-master, agent on k3s-node1"
  local args="" aargs="" ver=""
  [ "$K3S_CONNTRACK0" = 0 ] || args+=" --kube-proxy-arg=conntrack-max-per-core=0"
  [ -z "$K3S_SNAPSHOTTER" ] || args+=" --snapshotter=$K3S_SNAPSHOTTER"
  aargs=$args
  [ -z "$K3S_VERSION" ] || ver="INSTALL_K3S_VERSION=$K3S_VERSION "
  # Deviation from G2: --disable=traefik (the platform is reached through NodePorts, no ingress
  # controller is needed); it applies to a new cluster only, an existing server is not touched.
  local scmd="curl -sfL https://get.k3s.io | ${ver}INSTALL_K3S_EXEC=\"server --disable=traefik$args\" sh -"
  local m=k3s-master a=k3s-node1

  if exists "$m" && active "$m" k3s; then
    ok "$m: k3s server active, install skipped"
    [ "$DRY" = 0 ] || { printf '  [dry-run] on a new cluster: incus exec %s -- bash -c %q\n' "$m" "$scmd"; }
  else
    exists "$m" && ensure_registries "$m"
    rsh "$m" "$scmd"
    ok "$m: k3s server installed"
  fi
  [ "$DRY" = 1 ] || ix "$m" 'for i in $(seq 60); do k3s kubectl get nodes >/dev/null 2>&1 && exit 0; sleep 2; done; exit 1'

  if exists "$a" && active "$a" k3s-agent; then
    ok "$a: k3s agent active, join skipped"
    [ "$DRY" = 0 ] || printf '  [dry-run] on a new cluster: incus exec %s --env K3S_TOKEN=<node-token read at runtime> -- bash -c %q\n' \
      "$a" "curl -sfL https://get.k3s.io | ${ver}K3S_URL=https://$m.incus:6443 INSTALL_K3S_EXEC=\"agent$aargs\" sh -"
  else
    exists "$a" && ensure_registries "$a"
    if [ "$DRY" = 1 ]; then
      printf '  [dry-run] incus exec %s --env K3S_TOKEN=<node-token read at runtime> -- bash -c %q\n' \
        "$a" "curl -sfL https://get.k3s.io | ${ver}K3S_URL=https://$m.incus:6443 INSTALL_K3S_EXEC=\"agent$aargs\" sh -"
    else
      # The token is read at runtime, passed through the environment, and never written anywhere.
      # G2 shows this step at the master prompt; it must run on the worker.
      local tok; tok=$(incus exec "$m" -- cat /var/lib/rancher/k3s/server/node-token)
      incus exec "$a" --env K3S_TOKEN="$tok" -- bash -c \
        "curl -sfL https://get.k3s.io | ${ver}K3S_URL=https://$m.incus:6443 INSTALL_K3S_EXEC=\"agent$aargs\" sh -"
    fi
    ok "$a: k3s agent joined"
  fi
  if [ "$DRY" = 0 ]; then
    local n
    for n in $K3S_NODES; do
      kc wait --for=condition=Ready "node/$n" --timeout=180s >/dev/null 2>&1 && ok "$n Ready" || { bad "$n not Ready"; return 1; }
    done
  fi
}

# --- check -----------------------------------------------------------------------------
step_check() {
  log "check"
  local rc=0 n
  for n in $K3S_NODES $DOCKER_HOST_NAME; do
    if exists "$n" && running "$n"; then ok "$n running"; else bad "$n not running"; rc=1; fi
  done
  [ "$rc" = 0 ] || return 1
  local nodes; nodes=$(kc get nodes --no-headers 2>/dev/null || true)
  for n in $K3S_NODES; do
    if printf '%s\n' "$nodes" | grep -E "^$n +Ready( |,)" >/dev/null; then ok "node $n Ready"; else bad "node $n not Ready"; rc=1; fi
  done
  if incus exec "$DOCKER_HOST_NAME" -- docker info >/dev/null 2>&1; then ok "docker answers"; else bad "docker does not answer"; rc=1; fi
  if incus exec k3s-master -- curl -sf "http://$REGISTRY/v2/_catalog" >/dev/null 2>&1; then ok "registry answers at $REGISTRY"; else bad "registry does not answer at $REGISTRY"; rc=1; fi
  for n in $K3S_NODES; do
    if incus exec "$n" -- test -f /etc/rancher/k3s/registries.yaml; then ok "$n registries.yaml present"; else bad "$n registries.yaml missing"; rc=1; fi
  done
  return "$rc"
}

case "$STEP" in
  profiles) step_profiles ;;
  nodes)    step_nodes ;;
  docker)   step_docker ;;
  registry) step_registry ;;
  k3s)      step_k3s ;;
  check)    step_check ;;
  all)      step_profiles; step_nodes; step_docker; step_registry; step_k3s; [ "$DRY" = 1 ] || step_check ;;
esac
