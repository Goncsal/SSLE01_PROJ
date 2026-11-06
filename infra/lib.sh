# Shared helpers, sourced by the infra scripts. Only `incus` is needed on the host.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
. "$ROOT/infra/env.sh"

log()  { printf '\033[1;34m==>\033[0m %s\n' "$*"; }
ok()   { printf '  \033[32mok\033[0m   %s\n' "$*"; }
bad()  { printf '  \033[31mFAIL\033[0m %s\n' "$*"; }

exists()  { incus info "$1" >/dev/null 2>&1; }
running() { [ "$(incus list "^$1\$" -f csv -c s)" = "RUNNING" ]; }
# first IPv4 of an instance (containers use eth0, VMs enp5s0)
ipof() { incus list "^$1\$" -f csv -c 4 | tr '\n' ' ' | grep -oE '[0-9.]+ \((eth0|enp5s0)\)' | head -1 | cut -d' ' -f1; }

# wait until the instance accepts exec (VM agent up) and has working DNS
wait_ready() {
  local i
  for i in $(seq 1 90); do incus exec "$1" -- true >/dev/null 2>&1 && break; sleep 2; done
  for i in $(seq 1 60); do
    incus exec "$1" -- sh -c 'getent hosts deb.debian.org >/dev/null 2>&1' && return 0; sleep 2
  done
  echo "instance $1 not ready" >&2; return 1
}

# kubectl runs inside the master, nothing is installed on the host
kc() { incus exec k3s-master -- kubectl "$@"; }

# Profile flags for a K3s node / docker host according to K3S_NODE.
node_launch_flags() {
  if [ "$K3S_NODE" = vm ]; then echo "--vm --profile default --profile vm-lab"
  else echo "--profile default --profile k3s"; fi
}
