# Platform setup: Incus, K3s and the registry

This describes how the central platform's infrastructure is built from scratch with
`infra/01_base.sh`, in the two supported modes, and how to test the container mode.

## Two modes

The platform is three Incus instances: `k3s-master`, `k3s-node1` (K3s server and agent) and
`docker` (image builds and the private registry `docker.incus:5000`). `K3S_NODE` in
`infra/env.sh` (override in `infra/local.env`) selects how they are created.

| Mode | Instances | Host | Why |
|---|---|---|---|
| `container` | privileged Incus containers, profile `k3s` from the course guide (G2) | a Debian VM with Incus; the host has only `incus` | the course way, light, fast |
| `vm` | Incus virtual machines, profile `vm-lab` | bare-metal workstation | blast radius |

The guide's `k3s` profile sets `security.privileged=true`, an unconfined AppArmor profile,
`lxc.cap.drop=` (no capability dropped) and allows every device. A privileged container with
those settings is effectively root on the kernel it shares with the host. Inside a disposable
Debian VM that is acceptable, because the VM is the boundary. On a bare-metal workstation it
is not, so there the same instances are Incus VMs with their own kernel. The scripts are the
same in both modes; only `node_launch_flags` (in `infra/lib.sh`) and a few K3s arguments differ.

## Prerequisites of the Debian VM (container mode)

- Debian 13 with Incus from the Debian archive (`apt install incus`), the user in
  `incus-admin`.
- A storage pool and the bridge: `incus admin init` with a `dir`, `btrfs` or `zfs` pool and
  the default bridge `incus-default` (name from `BRIDGE`). On ZFS set `K3S_SNAPSHOTTER=native`.
- Nested virtualisation is not needed (containers). The VM needs about 8 GiB RAM and 4 CPUs.
- Host sysctls, global to the VM kernel: `vm.overcommit_memory=1`, `kernel.panic=10`,
  `kernel.panic_on_oops=1`. `01_base.sh` writes `/etc/sysctl.d/90-k3s.conf` with `sudo`; without
  `sudo` it prints the commands and stops.
- `infra/local.env` must not set `K3S_NODE=vm` (or run `K3S_NODE=container infra/01_base.sh ...`).

## Bring-up order

```
infra/01_base.sh        # profiles, nodes, Docker CE, registry, K3s, registries.yaml, check
infra/02_networks.sh    # city networks
infra/04_build_push.sh  # build and push the service images
infra/05_deploy.sh      # deploy the platform to K3s
infra/03_cities.sh      # city networks' stations and chargers
```

`01_base.sh [all|profiles|nodes|k3s|docker|registry|check] [--dry-run]`. Every step is
idempotent: an existing and correct object is reported ok and left alone. `--dry-run` prints
every `incus`/`sudo` command that would change something. The K3s node token is read at
runtime and passed through the environment of one `incus exec`; it is never written to a file.

Tunables (environment): `K3S_MEM`, `K3S_CPU`, `DOCKER_MEM`/`DOCKER_CPU` (vm mode),
`K3S_SNAPSHOTTER`, `K3S_CONNTRACK0` (default 1 in container mode, 0 in vm mode),
`K3S_VERSION`, `LXC_EXTRA` (additional `raw.lxc` lines).

Deviations from the guide, each also commented in the script:
- cgroup v2 device rule: `lxc.cgroup2.devices.allow=a` instead of `lxc.cgroup.devices.allow=a *:* rwm`.
- `--disable=traefik` at cluster creation (the platform uses NodePorts).
- kube-proxy `conntrack-max-per-core=0` in container mode.
- `daemon.json` is merged, not overwritten.
- the worker join runs on `k3s-node1` (the guide shows the master prompt).
- vm mode: limits are set per instance at launch, not on the profile.

## Testing container mode on the Debian VM

1. Dry run, then the real run:
   ```
   K3S_NODE=container infra/01_base.sh all --dry-run
   K3S_NODE=container infra/01_base.sh all
   K3S_NODE=container infra/01_base.sh check
   ```
2. Look at the instances and the profile:
   ```
   incus list
   incus config show k3s-master --expanded | grep -E 'privileged|nesting|raw.lxc|kmsg'
   incus profile show k3s
   ```
3. The cluster:
   ```
   incus exec k3s-master -- kubectl get nodes -o wide     # both Ready
   incus exec k3s-master -- kubectl -n kube-system get pods
   ```
4. A pod pulling from the lab registry:
   ```
   infra/04_build_push.sh                                   # pushes images, mirrors postgres
   incus exec k3s-master -- kubectl run pulltest --image=docker.incus:5000/postgres:17-alpine \
       --restart=Never --command -- sleep 60
   incus exec k3s-master -- kubectl get pod pulltest        # Running, no ErrImagePull
   incus exec k3s-master -- kubectl delete pod pulltest
   ```
5. A NodePort reached from a city network: after `05_deploy.sh` and `02_networks.sh`, run a
   request from an instance attached to a city network against `k3s-node1.incus` and the
   service's NodePort (`kubectl -n evcharge get svc`); the TCP connection must open.
6. Re-run `01_base.sh all`: every line must be ok and no K3s restart (compare
   `systemctl show k3s -p ActiveEnterTimestamp` before and after).

### Known failure modes

| Symptom | Cause | Fix |
|---|---|---|
| `incus start` fails parsing `raw.lxc`, or "lxc.cgroup.devices.allow" unknown | cgroup v1 key on a cgroup v2 host | use `lxc.cgroup2.devices.allow=a` (the script does) |
| container starts but kubelet cannot access devices, BPF device program fails to load | `lxc.cgroup2.devices.allow=a *:* rwm` | only `lxc.cgroup2.devices.allow=a` works |
| kubelet or K3s fails on `/dev/kmsg` | no `/dev/kmsg` in the container | profile device `kmsg` (`unix-char`, source and path `/dev/kmsg`) |
| kube-proxy crash loop, `open /proc/sys/net/netfilter/nf_conntrack_max: permission denied` | a container cannot set a host sysctl | K3s argument `--kube-proxy-arg=conntrack-max-per-core=0` (the script does) |
| containerd or pods fail with overlayfs errors, `invalid argument` | storage pool (ZFS) without overlayfs support in the container | `K3S_SNAPSHOTTER=native` and reinstall the node |
| K3s warns about `/proc/sys/vm/overcommit_memory` or `kernel.panic` | host sysctls missing | `/etc/sysctl.d/90-k3s.conf`, `sysctl --system` on the host |
| kubelet cannot mount or read `/sys` | read-only mounts in the container | add `LXC_EXTRA="lxc.mount.auto=proc:rw sys:rw cgroup:rw"` to the profile |
| pods `ErrImagePull` against `docker.incus:5000` | `registries.yaml` missing or K3s not restarted | `infra/01_base.sh registry` |
| `docker push` refuses http | `insecure-registries` missing | `infra/01_base.sh registry` |
