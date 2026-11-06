# City network ACLs

`infra/08_acls.sh` closes the gap left by the earlier phases: until now a city network could
reach every central K3s NodePort, and each city's Prometheus API (9090, no authentication)
was readable from its own network. The script builds Incus network ACLs for the three
`city-*` networks from the allow list of `docs/interfaces.md` section 16 and attaches them.

## Model

Two ACLs per city, both attached to the network (`city-<city>`):

- `evcharge-<city>-stations`: rules whose source or destination is the city's stations.
- `evcharge-<city>-edge`: rules whose source or destination is the city's edge.

The network also gets `security.acls.default.egress.action=drop` and
`security.acls.default.ingress.action=drop`, so anything not listed is dropped. Rules use
`state: enabled`, so replies to allowed connections pass.

## Allow list

| Role | May reach | Port |
|---|---|---|
| station | its own edge | 443 (80 in a `PLAIN_WS_CITIES` city) |
| station | Wazuh manager | 1514, 1515 |
| station, edge | the city gateway | DNS 53, DHCP 67, ICMP |
| edge | K3s nodes | 30900, 30801 |
| edge | Wazuh manager | 1514, 1515 |
| K3s nodes (ingress to the edge) | city edge | 9090, 9100, 9113 |
| station, edge (only with `--allow-updates`) | internet (NAT) | 80, 443 |

Ingress to a city instance is otherwise limited to the gateway. Stations cannot reach the
NodePorts, other stations, other cities, `lab-ca`, or the Prometheus and Grafana NodePorts.

## Why Incus ACLs, and why on the network

Incus network ACLs are part of Incus, so they behave the same on this laptop and on the
Debian VM, and the script only needs the `incus` command (the course-way rule). Host nft
rules would depend on the host.

The ACLs are attached to the network (`security.acls`), not to each instance NIC. Per-NIC
attachment would mean overriding the NIC device on every station and edge: it changes the
instances, it is lost when an instance is rebuilt from its profile, and it needs a pass
over 18 instances. Attached to the network, one `incus network set` per city covers every
member, including rebuilt ones, and removal is one command. The price is that roles are
told apart by address, not by ACL name, so addresses are resolved from `incus list` at
apply time. If an instance gets a new address, run `apply` again.

## Workflow

```
infra/08_acls.sh status                     # read-only: attached ACLs and rules
infra/08_acls.sh apply                      # dry-run (the default): prints the plan
infra/08_acls.sh --check                    # the computed rules equal the allow list
EVCHARGE_BASELINE_DONE=1 infra/08_acls.sh apply --run [city ...]
EVCHARGE_BASELINE_DONE=1 infra/08_acls.sh apply --run --allow-updates   # while rebuilding
EVCHARGE_BASELINE_DONE=1 infra/08_acls.sh remove --run                  # back to the open state
```

`apply` and `remove` only print what they would do unless `--run` is given, and `--run` is
refused unless `EVCHARGE_BASELINE_DONE=1`, so the ACLs cannot be applied during the baseline
run. Both are idempotent. `remove` keeps any ACL on the network that is not an `evcharge-*`
one. The script only touches `city-*` networks of cities in `CITY_NET`.
Use `--allow-updates` before rebuilding a station or edge (package installs), then re-run
`apply --run` without it.

## Before and after (hijacking PoC)

Before: a rogue station connects straight to a central NodePort and reaches the CSMS, which
refuses it with `ident.untrusted_peer`. After `apply --run`: the same connection from the
station is dropped at the network and never reaches the CSMS. The botnet phase gets the
same control: a bot cannot reach a C2 outside the allow list on 80/443 unless updates are
open (a C2 on the central bridge is reachable only if the test allows it explicitly).

## Limitations

- L3/L4 only. A fully compromised station still has the legitimate path to its own edge
  and so can abuse the OCPP connection; the identity checks and rate limits cover that.
- `--allow-updates` opens 80/443 to any destination, central bridge included.
- Wazuh agent traffic is allowed on tcp only.
