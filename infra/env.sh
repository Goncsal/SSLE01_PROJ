# Deployment settings (sourced by every infra script).
# Host-specific overrides go in infra/local.env (not committed).

# How the K3s nodes and the docker host are created:
#   container  privileged Incus containers, the course guide's "k3s" profile (Debian VM host)
#   vm         Incus virtual machines (bare-metal hosts, where a privileged container would be root on the host)
K3S_NODE="${K3S_NODE:-container}"

IMAGE="${IMAGE:-images:debian/trixie}"
BRIDGE="${BRIDGE:-incus-default}"
BRIDGE_CIDR="${BRIDGE_CIDR:-10.255.255.1/24}"

K3S_NODES="k3s-master k3s-node1"
K3S_MEM="${K3S_MEM:-3GiB}"
K3S_CPU="${K3S_CPU:-2}"
DOCKER_HOST_NAME="docker"
REGISTRY="${REGISTRY:-docker.incus:5000}"
WAZUH_HOST="${WAZUH_HOST:-wazuh-container}"

# city name -> network address (gateway address of the Incus bridge)
declare -A CITY_NET=(
  [porto]="10.31.0.1/24"
  [lisboa]="10.32.0.1/24"
  [aveiro]="10.33.0.1/24"
)
STATIONS_PER_CITY="${STATIONS_PER_CITY:-5}"
CHARGERS_PER_STATION="${CHARGERS_PER_STATION:-2}"
# cities that start without TLS on the charger link (legacy site)
PLAIN_WS_CITIES="${PLAIN_WS_CITIES:-aveiro}"

ENV_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
[ -f "$ENV_DIR/local.env" ] && . "$ENV_DIR/local.env"
