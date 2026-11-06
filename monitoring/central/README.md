# Central monitoring (K3s, namespace `monitoring`)

Contract: `docs/interfaces.md` section 14.2 to 14.6. Deploy with `infra/06_monitoring.sh`.

## Components

| Component | Image (mirrored by `infra/04_build_push.sh`) | NodePort | Notes |
|---|---|---|---|
| Prometheus | `prom/prometheus:v3.5.0` | 30090 | Deployment, `Recreate`, PVC 5 Gi `local-path`, retention 7 d, `extra-scrape-metrics`, lifecycle API on |
| Alertmanager | `prom/alertmanager:v0.28.1` | 30093 | config from `monitoring/alertmanager/alertmanager.yml` (built-in default with the 14.4 inhibition rules if absent) |
| Grafana | `grafana/grafana-oss:11.6.4` | 30300 | anonymous off, admin password from Secret `grafana-env` (`GRAFANA_ADMIN_PASSWORD` in `secrets/platform.env`) |

All run non-root with all capabilities dropped, probes and the resource limits of 14.2, preferably on
`k3s-node1`. Prometheus has a ServiceAccount with a ClusterRole (pods, nodes, nodes/proxy, endpoints:
get, list, watch).

## ConfigMaps (built by the script from the repo)

| ConfigMap | Source | Mounted at |
|---|---|---|
| `prometheus-config` | `prometheus.yml.tmpl` rendered | `/etc/prometheus-config/prometheus.yml` |
| `prometheus-rules` | `monitoring/rules/*.yml` (top level only) | `/etc/prometheus/rules/` |
| `alertmanager-config` | `monitoring/alertmanager/alertmanager.yml` | `/etc/alertmanager/` |
| `grafana-datasources` | `monitoring/grafana/provisioning/datasources/*.yml` (else generated: `central` + `city-<city>`) | `/etc/grafana/provisioning/datasources/` |
| `grafana-dashboard-provider` | `monitoring/grafana/provisioning/dashboards/*.yml` (else a default) | `/etc/grafana/provisioning/dashboards/` |
| `grafana-dashboards` | every `*.json` under `monitoring/grafana/` | `/var/lib/grafana/dashboards/` |

Each ConfigMap carries a `content-hash` annotation. A changed Prometheus or Alertmanager config is
reloaded in place (`/-/reload`, after the mounted files match); changed Grafana datasources restart
Grafana; dashboards are re-read every 30 s. Re-running with no change touches nothing.

## Jobs

| Job | Targets | Interval |
|---|---|---|
| `csms`, `registry`, `billing` (job = pod label `app.kubernetes.io/name`, so later services appear too) | pods of `evcharge` with `prometheus.io/scrape=true`, port and path from the annotations; labels `pod`, `node` | 15 s |
| `cadvisor` | each node through the API proxy; only `container_cpu_usage_seconds_total` and `container_memory_working_set_bytes` of `evcharge` and `monitoring` | 30 s |
| `prometheus`, `alertmanager`, `grafana` | the in-namespace Services | 30 s |
| `federate-<city>` | `edge.<city>.pt.lab:9090/federate`, `match[]={__name__=~"city:.*"}`, `honor_labels` | 30 s, timeout 10 s |
| `direct-<city>` | counterfactual: station SD of the registry plus the edge exporters 9113 and 9100 | 5 s, only when on |

`*.pt.lab` resolves in pods because `infra/02_networks.sh` adds `server=/<city>.pt.lab/<city gateway>`
to `raw.dnsmasq` of the central bridge and CoreDNS forwards to the node resolver.

## Direct scrape (bandwidth counterfactual, 14.6)

```
infra/06_monitoring.sh --direct-scrape on     # renders the direct-<city> jobs and reloads
infra/06_monitoring.sh --direct-scrape off    # default; removes them
```
Compare `scrape_body_size_bytes{job=~"federate-.+"}` with `{job=~"direct-.+"}`. The setting is not
remembered: a run without the flag means `off`.

## Add a city

Add it to `CITY_NET` in `infra/env.sh`, run `infra/02_networks.sh` (zone forwarder) and
`infra/06_monitoring.sh` (job `federate-<city>` and datasource, if the datasource file is generated;
a file in `monitoring/grafana/provisioning/datasources/` needs the new entry by hand).
