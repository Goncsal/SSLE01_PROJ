# Wazuh agents

Contract: `docs/interfaces.md` 15.1 and 15.7. Manager: `wazuh-container` (ports 1514 and 1515).
Everything is deployed with `infra/07_wazuh_agents.sh [all|groups|stations|edges|central] [city ...]`
(only `incus` and bash on the host; idempotent, a second run changes nothing).

## Topology

| Where | Agent name | Groups | Reads |
|---|---|---|---|
| K3s nodes (DaemonSet `wazuh-agent`, namespace `wazuh`) | `pt.central.<node>` | `central` | `/var/log/evcharge/*.json` |
| city edges | `pt.<city>.edge` | `city-<city>`, `edge` | `edge-*.json`, nginx `error.log` |
| stations (15) | `pt.<city>.st-NNN` | `city-<city>`, `station` | `sim-*.json`, `netwatch.json`, `station-sockets` |
| manager | `wazuh-container` (agent 000) | | the response audit log |

Package: `wazuh-agent=4.14.9-1` from the Wazuh 4.x apt repository (key checked into a
keyring file, package put on hold so an upgrade never passes the manager's version).
Group configuration: `wazuh/groups/<group>/agent.conf`, pushed to
`/var/ossec/etc/shared/<group>/` and checked with `verify-agent-conf`.

## Files

- `install_native.sh <instance> <hier-id> <groups>`: package, enrolment, edge `iptables`,
  station netwatch and `logcollector.remote_commands=1` (needed for `full_command`).
- `netwatch.py`, `netwatch.service`, `netwatch.timer`: the station outbound watcher, run every
  10 s. A destination (`ip:port`) not in `/etc/evcharge/netwatch-allow.txt` is reported once per
  hour as `host.new_outbound` in `/var/log/evcharge/netwatch.json`. Connections to a local
  listening port (inbound, such as the metrics scrape) and to loopback are ignored. The allow
  file (city edge, city gateway, manager) is written at install time from `incus list`.
  Tests: `.venv/bin/python -m pytest -q wazuh/agents/tests` (canned `ss` output).
- `Dockerfile`, `entrypoint.sh`: the image of the DaemonSet, built with
  `infra/04_build_push.sh wazuh-agent` as `docker.incus:5000/evcharge-wazuh-agent:4.14.9`.
  `client.keys` and the log read positions are kept in the hostPath
  `/var/lib/evcharge-wazuh/agent`, so a restarted pod keeps its identity and does not re-read
  old log lines.
- Manifests: `k8s/wazuh/`. The Secret `wazuh-enroll` is created by the script from
  `WAZUH_ENROLL_PASSWORD` in `secrets/platform.env` (stdin only).

## Enrolment password

When the manager requires a password (`<use_password>yes`), the script sends the value of
`WAZUH_ENROLL_PASSWORD` on stdin; the agent writes it to `authd.pass` for the duration of
`agent-auth` and the file is removed straight after. Before that, agents enrol without one.

## Edge response files

Edges also get `/var/ossec/active-response/bin/evcharge-edge-drop`, `/etc/evcharge/role`
(`edge`) and `/etc/evcharge/city-net` (the city network, from `infra/env.sh`); stations get the
script only, where it does nothing because they have no role file. The station's
`/var/log/evcharge/netwatch.json` is created empty at install: the log collector ignores a file
that does not exist when it starts.

## Memory (measured)

Agent processes: about 55 MiB resident on a station or edge, about 31 MiB per pod as reported
by `kubectl top`. First station (`aveiro-st-001`): working set 99 to 104 MiB with the agent and
`evsim` (gate 180 MiB); `incus list` shows higher values (150 to 190 MiB of the 224 MiB limit)
because it includes the page cache. Host available memory 5.5 GiB before, 5.1 GiB after.
Fallback if memory runs short: `ONE_PER_CITY=1 infra/07_wazuh_agents.sh stations`.

## Re-enrol or remove

- Re-enrol one agent: on the manager `agent_control`/`manage_agents -r <id>` (or
  `/var/ossec/bin/manage_agents -r <id>`), then in the instance
  `: > /var/ossec/etc/client.keys` and rerun `infra/07_wazuh_agents.sh stations <city>`.
  For a K3s node, delete `/var/lib/evcharge-wazuh/agent/client.keys` on the node and delete the pod.
- Remove from a station: `incus exec <inst> -- sh -c 'apt-mark unhold wazuh-agent && apt-get purge -y wazuh-agent;
  systemctl disable --now netwatch.timer; rm -f /etc/systemd/system/netwatch.* /opt/evcharge/netwatch.py'`,
  then `manage_agents -r <id>` on the manager.
- Remove the DaemonSet: `kubectl delete namespace wazuh`.
