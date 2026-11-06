# Phase 4 evidence: monitoring backbone (milestone M1)

All times UTC, 2026-11-05 unless stated. Lab only: the rogue and the C2 stub were temporary
containers, deleted afterwards (`incus list | grep -E 'rogue|c2'` is empty).

## Order of the run

1. Deploy (`infra/04_build_push.sh`, `02_networks.sh`, `03_cities.sh monitoring`, `06_monitoring.sh`): all targets up.
2. **Scenarios first** (windows below), then the baseline recording and the bandwidth windows.
3. **Baseline recording start: 2026-11-05T23:30:00Z.** Nothing but normal load runs from then on:
   no alert fired after that time (`ALERTS{alertstate="firing"}` empty from 23:30 to the end).
   The central Prometheus is the recording; it was not restarted or wiped (the two config
   reloads for the direct-scrape window do not touch data). The bandwidth windows are inside the
   baseline window; the direct jobs add series for the stations but do not change `city:*` or
   CSMS series.
4. Window A (federation only) 23:30:00 to 00:00:00; window B (`--direct-scrape on`) 00:02 to 00:32:30;
   direct scrape switched **off** again afterwards.

Attack/fault windows (all finished before 23:30):

| Window | Scenario |
|---|---|
| 22:54:50 to 22:58:08 | `evsim` stopped in `lisboa-st-003` |
| 22:59:20 to 23:03:30 | `aveiro-edge` stopped (4 min 10 s) |
| 23:04:22 to 23:06:02 | `aveiro-rogue` runs `evsim` for `pt.aveiro.st-002.cp-1` (already connected) |
| 23:08:59 to 23:18 | `porto-st-004` compromised (`--compromised --c2-url`, then also `--arrival-rate 240`), C2 stub in `porto-c2` flooding at 20 Hz |
| 23:15:52 to 23:17:46, 23:19 to 23:21, 23:21:49 to 23:24:56 | `lisboa-st-003` stopped again (alert JSON, then screenshots of the grey marker and red ring) |

## Timings (steps 3 and 4)

Station stop (`lisboa-st-003`, stopped 22:54:50):

| Event | Seconds after the stop |
|---|---|
| Marker grey (map query value 0 through Grafana `/api/ds/query`) | 6 (worst case about 15: CSMS scrape interval) |
| `StationDown` in Alertmanager | 83 (rule `for: 1m`, federation 30 s) |
| `ChargerOffline` in Alertmanager | 130 (`for: 2m`) |
| Station started again: marker green | 15.5 |

Edge stop (`aveiro-edge`, stopped 22:59:20): `EdgeDown{city="aveiro"}` active at 101 s;
`ChargerOffline` of the 5 Aveiro stations appears at 131 s and is **inhibited** (state
`suppressed`, `inhibitedBy` = the EdgeDown alert); `/api/v2/alerts?inhibited=true` saved in
`alerts/inhibition-aveiro-edge-down.json`. `StationDown` for Aveiro cannot fire while the edge is
down (the `city:station_up:min` series come from the unreachable edge); the other cities showed
no alert. Edge started 23:03:30: all alerts resolved, 30/30 chargers reconnected within about 2 min.

## Alerts fired (step 5), `alerts/`

| Alert | Cause | Labels seen |
|---|---|---|
| `DuplicateChargerID` | rogue for an already connected id, refused with 409 | `city=aveiro hier_id=pt.aveiro threat=hijack severity=critical`; firing 23:07 to 23:09 (see fix 1) |
| `ChargerMsgFlood` | compromised station, 20 Hz in a session | `alerts/botnet-flood-porto.json`: `city=porto hier_id=pt.porto threat=botnet severity=critical` |
| `MsgRateAnomaly` | same, city rate 23 msg/s against 4.5 | same file, `threat=botnet severity=warning` |
| `MeterAnomalies` | `no_transaction` meter values sent by the flooding bot | `city=porto hier_id=pt.porto threat=fraud` |
| `StationDown`, `ChargerOffline` | station stop | `alerts/stationdown-lisboa-st-003.json`: `hier_id=pt.lisboa.st-003 threat=infra` |
| `EdgeDown` + inhibited `ChargerOffline` | edge stop | `alerts/inhibition-aveiro-edge-down.json` |

The flood is only visible while a charger is in a session (the bot raises the meter-value rate), so
the station needed `--arrival-rate 240`; `sim.env` was restored to `SIM_EXTRA_ARGS=` afterwards.

## Locate (step 7)

`StationDown` (`hier_id=pt.lisboa.st-003`) from the alert JSON, then
`GET http://<node>:30801/locate/pt.lisboa.st-003`: host `lisboa-st-003`, ip `10.32.0.86`, network
`city-lisboa`, edge `pt.lisboa.edge`, dns `st-003.lisboa.pt.lab`, lat/lon. **Located in 0.013 s**
(API call; reading the alert is the human part). City-level alerts (`pt.porto`) locate to the city
and its subtree, not to a station.

## Bandwidth (step 6, contract 14.6)

Method: window A = 30 min federation only, window B = 30 min with the `direct-<city>` jobs (5 s,
7 targets per city: 5 stations and the edge exporters 9113 and 9100), same load. Uncompressed:
`sum by (city) (avg_over_time(scrape_body_size_bytes{job=~"federate-.+"}[30m])) * 60 / 30` and the
same for `direct-.+` with `* 60 / 5`, evaluated at the window end (`windowA.json`, `windowB.json`).
Gzip: `eval/bandwidth.sh` from a throwaway pod, one `/federate` per city and one request per direct
target with `Accept-Encoding: gzip`, scaled by the scrape interval (`bandwidth.txt`). TCP/HTTP overhead ignored.

| | Federated (A) | Direct (B) | Saving |
|---|---|---|---|
| Bytes/min, uncompressed, total | 47,234 (46 KiB) | 6,383,567 (6.1 MiB) | **99.3 %** |
| Bytes/min, gzip, total | 4,372 | 962,688 | **99.5 %** |
| Per city, uncompressed (aveiro / lisboa / porto) | 15,847 / 15,884 / 15,502 | 2,130,751 / 2,128,245 / 2,124,570 | 99.3 % each |
| Samples/min, total | 324 (108 per city) | 55,427 (about 18,470 per city) | 99.4 % |
| Series per scrape | 54 per city | about 1,540 per city | |
| Central Prometheus RSS (30 min avg) | 131.8 MiB | 148.1 MiB | +12 % |
| Central Prometheus working set (cAdvisor) | 105.5 MiB | 120.0 MiB | +14 MiB |
| Central Prometheus CPU (cores, avg) | 0.007 | 0.037 | 5x |
| Head series (avg) | 8,952 | 9,057 | |

Scaling: B grows with chargers and stations (every station target at 5 s), A grows with the
number of `city:*` series (about 54 per city, independent of stations). Federation traffic is
constant at 15.5 KB per city per minute for 10 chargers; the direct cost was 2.1 MB per city per minute.

## RAM (host free: 5,440 MiB available at the end, was 5,643 at the start)

| Item | Used |
|---|---|
| `porto-edge`, `lisboa-edge`, `aveiro-edge` (incl. city Prometheus) | 93, 93, 86 MiB |
| a station container (`porto-st-001` etc.) | about 50 MiB each (15 stations, about 750 MiB) |
| Monitoring pods (Prometheus, Grafana, Alertmanager) | 111, 102, 20 MiB |
| `k3s-master` / `k3s-node1` | 1,359 and 1,081 MiB of 2,845 MiB (cAdvisor view 49 % and 38 %) |

## Dashboards (step 8)

Panel queries of all four dashboards were run through Grafana `/api/ds/query` (`panels/panel-queries.json`:
38 panels, 0 errors; the city dashboard against `city-lisboa`). The dashboards were also rendered
in headless Chromium from the host and the screenshots are in `screenshots/`. The host has no
internet, so the OpenStreetMap tiles are missing in those images (dark background); on the user's
browser the basemap loads.

Screenshots for the user to take (browser, Grafana `http://<node>:30300`, user `admin`):

1. **Map, all green**: `evcharge-map`, last 30 minutes, 10 s refresh. Three clusters (Porto, Lisboa,
   Aveiro) of green markers, city totals 10/10/10, firing-alerts table empty. Scroll the map once to
   zoom into one city and show the 5 separate markers.
2. **Map, grey station**: stop `incus exec lisboa-st-003 -- systemctl stop evsim`, wait 15 s, zoom on Lisboa:
   one grey marker, tooltip shows Element, City, Station, Share connected 0 %, Chargers 0 of 2.
3. **Map, red alert ring**: the same, after about 90 s: a red ring around the grey marker and
   `StationDown` in the table. Start the station again afterwards.
4. **City overview**: `evcharge-city`, City = porto, last 30 minutes: charger table (status, link, power),
   station power, baseline panel.
5. **Bandwidth**: `evcharge-bandwidth`, time range 2026-11-05 23:40 to 2026-11-06 00:45 UTC (browser time
   zone matters): federated about 46 KiB/min against direct about 6 MiB/min, step at 00:02.
6. **Alertmanager** `http://<node>:30093`: during an edge stop, `EdgeDown` active and the Aveiro
   `ChargerOffline` entries under "inhibited".

## Fixes made during integration

1. `monitoring/rules/alerts.yml`, `DuplicateChargerID`, `IdentityRejections`, `MeterAnomalies`: the CSMS creates
   these counters at the first event, so `increase()` over a series born at 1 is 0 and the first
   incident never fired. Expression is now `m - (m offset 5m or 0 * m)` (growth over 5 min counting a new
   series from 0). New promtool test case in `monitoring/rules/tests/threats.test.yml` (promtool is not
   installed on the host; the expression was checked live against Prometheus and the alert fired).
2. `monitoring/grafana/dashboards/evcharge-map.json`: zoom 7 to 7.6, centre lat 39.95, smaller markers (9) and
   ring (24) so the stations of a city do not fully overlap; lat/lon shown with 4 decimals in the tooltip.
3. `monitoring/grafana/dashboards/evcharge-city.json`, charger table: `merge` left Link and Power empty on
   separate rows; replaced by `joinByField` on `hier_id` (outer) with the duplicate label columns hidden.
4. `eval/bandwidth.sh` is new (wire sizes per city and the 14.6 PromQL results).

## Known limitations and notes

- `ChargerMsgFlood`, `MsgRateAnomaly`, `DuplicateChargerID` carry the city `hier_id` (`pt.porto`), not the
  station, so the map shows no ring for them and locate returns the city subtree. The correlator (Phase 9)
  has to narrow it down (the CSMS logs or `evcharge_sim_*` per charger).
- `ChargerOffline` carries `site="pt.central"` (it is computed from CSMS metrics) while `hier_id` is the station.
- Aveiro federated 51 vs 54 series earlier: the count depends on station state (a stopped station
  removes its series); all three cities show 54 in steady state, it is not a TLS effect.
- Grey marker: the map keeps stopped stations through `or on (hier_id) 0 * registry_station_info`; the
  red ring is only for station-level alerts.
- The baseline thresholds in `monitoring/rules/baseline.yml` are still `provisional`.
- Edge down hides station-level `StationDown`; central only sees the `EdgeDown` alert.
- The host clock of the containers is a few seconds ahead of the K3s VMs (as in Phase 3).

## Commands

```
infra/04_build_push.sh; infra/02_networks.sh; infra/03_cities.sh monitoring; infra/06_monitoring.sh
infra/06_monitoring.sh --direct-scrape on|off
eval/bandwidth.sh --window 30m [--end <unix seconds>]
monitoring/baseline/run.sh --window 24h && infra/06_monitoring.sh     # when 6 h (target 24 h) have passed since 23:30Z
```
