# EV charging network: threat detection, monitoring and prevention

SSLE Project 1 (Security in Large-Scale Systems, Universidade de Aveiro).

A regional operator runs EV chargers in several cities. Chargers talk to a central
Charging Station Management System (CSMS) over OCPP 1.6J. The platform adds billing,
fraud scoring, Prometheus monitoring with per-city federation, and Wazuh SIEM with
automated responses.

## Platform

The lab runs on one Linux host with Incus. The K3s nodes (`k3s-master`, `k3s-node1`) and
the Docker host are Incus VMs on this host: `infra/local.env` sets `K3S_NODE=vm`.
`K3S_NODE=container` is the course setup (privileged Incus containers on a Debian VM).
`infra/env.sh` holds the default (`container`); overrides go in `infra/local.env`.
Each city is a separate Incus network with an edge gateway and one container per
charging station.

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
| `eval/` `evidence/` | scalability runs, captured evidence |
| `report/` | the report: LaTeX source (`report.tex`), Graphviz figures, built `report.pdf` |
| `docs/` | interface contract (`interfaces.md`), setup notes, demo runbook (`demo.md`) |

## Report

The report is `report/report.pdf`, built from `report/report.tex` with `make -C report`. Evidence is under `evidence/`. The demo
runbook is `docs/demo.md`.
