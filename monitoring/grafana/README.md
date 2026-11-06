# Grafana dashboards

Provisioned files for the EV charging network. `infra/06_monitoring.sh` mounts
`provisioning/` and `dashboards/` into the Grafana pod; the admin password comes from the
`grafana-env` Secret. Contract: `docs/interfaces.md`, section 14.5.

Datasources: `central` (default, `http://prometheus:9090`, 15 s) and `city-porto`,
`city-lisboa`, `city-aveiro` (`http://edge.<city>.pt.lab:9090`, 5 s). Per-charger series
exist only in the city datasources. All dashboards refresh every 10 s and show the last hour.

## Dashboards

### `evcharge-map` (the live view)
- **Charging stations (Geomap):** one marker per station. Green: all chargers connected;
  orange: some; grey: none. A red ring appears while an alert fires on the station. The
  tooltip shows element, city, station, connected/total chargers and share. Coordinates come
  from `evcharge_registry_station_info`; stations stay on the map (grey) even when the CSMS
  reports no chargers for them.
- **Chargers connected per city, Power reported vs feeder meter, Sessions started in the last hour.**
- **Firing alerts:** table of `ALERTS{alertstate="firing"}` with severity, city and element.
- **Grid power per city with baseline limit** and **Chargers connected over time.**

### `evcharge-city` (variable `city`)
- From the city Prometheus: charger status table (status names from the OCPP codes), station
  feeder vs reported power, power and energy per charger, message rate per charger,
  reconnects, station power gap, edge connections and requests.
- From the central Prometheus: city load with baseline mean and upper limit, identity
  rejections by reason, card authorisation by result, meter anomalies.

### `evcharge-platform`
CSMS replicas up, registry and billing status, connected chargers per replica, OCPP handle
latency p50 and p95 by action, handshakes, dependency errors and latency, pod CPU and
memory, firing alerts with `threat="infra"`.

### `evcharge-bandwidth`
Federated bytes per minute per city (body size times two scrapes per minute), the same for
the `direct-*` counterfactual jobs (5 s, so times twelve) when they run, samples per scrape,
and head series in the central Prometheus.

## Checks

```bash
.venv/bin/python monitoring/grafana/check_dashboards.py
```
Parses every dashboard, checks unique ids, that datasource uids are provisioned and that
every metric in an expression is known to the contract.

## Screenshots for the report

1. Log in to Grafana (NodePort 30300 on a K3s node) with the admin account.
2. Open EV charging / `evcharge-map`, set the time range to the last 15 minutes, press `d`
   then `k` for kiosk mode and take a browser screenshot of the whole window (the image
   renderer is not installed, so panel PNG export is not available).
3. Take one capture in normal state (all markers green) and one during an incident (grey or
   orange marker, red ring, alert in the table). Hover a marker for the tooltip.
4. For the bandwidth section, capture `evcharge-bandwidth` after the 30 minute windows of
   section 14.6.
