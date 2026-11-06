# EV charging network: threat detection, monitoring and prevention

SSLE Project 1 (Security in Large-Scale Systems, Universidade de Aveiro).

A regional operator runs EV chargers in several cities. Chargers talk to a central
Charging Station Management System (CSMS) over OCPP 1.6J. The platform adds billing,
fraud scoring, Prometheus monitoring with per-city federation, and Wazuh SIEM with
automated responses.

## Platform

The lab follows the course setup: a Debian VM running Incus, with the K3s nodes as
privileged Incus containers and Docker in its own Incus instance. Each city is a
separate Incus network with an edge gateway and one container per charging station.

`infra/env.sh` selects how K3s nodes are created (`K3S_NODE=container`, the default,
or `K3S_NODE=vm` on a bare-metal host). Host-specific overrides go in `infra/local.env`.

## Layout

| Path | Contents |
|---|---|
| `sim/` `csms/` `registry/` `billing/` `fraud/` `correlator/` `common/` | services (Python) |
| `gateway/` `pki/` | city edge gateway (nginx) and lab CA |
| `infra/` | Incus and K3s setup scripts |
| `k8s/` | Kubernetes manifests |
| `monitoring/` | Prometheus (city and central), alert rules, Grafana dashboards |
| `wazuh/` | decoders, rules, active responses, integrations |
| `poc/` | attack proofs of concept, one folder per threat |
| `eval/` `evidence/` `report/` | scalability runs, captured evidence, report |

## Status

Work in progress.
