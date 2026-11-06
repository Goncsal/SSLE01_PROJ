# Alert rules and baseline thresholds

Files: `alerts.yml` (15 alerts, contract 14.3), `baseline.yml` (threshold series, 14.7),
`tests/*.test.yml` (`promtool test rules`). The central Prometheus loads both rule files;
Alertmanager (`../alertmanager/alertmanager.yml`) groups and inhibits.

Every alert has labels `severity`, `city`, `hier_id`, `threat` (the correlator's join keys)
and annotations `summary` and `description` that name the element and the value.

| Alert | Fires when | for | Threat | Why |
|---|---|---|---|---|
| `CSMSDown` | a CSMS pod is not scraped, or no CSMS target exists | 1m | infra | the platform core |
| `RegistryDown` / `BillingDown` | the service is not scraped | 1m | infra | identity source / billing |
| `EdgeDown` | the federation of a city fails or its nginx is down | 1m | infra | one alert for a dead edge or cut link |
| `StationDown` | a station's simulator endpoint is not scraped | 1m | infra | station off the air |
| `ChargerOffline` | the CSMS sees fewer chargers of a station than the city knows (absent gauge counts 0) | 2m | infra | lost or blocked chargers |
| `ChargerFlapping` | more than 5 reconnects in 5 min in a city | 5m | infra | unstable link, or a hijack/botnet retrying |
| `DuplicateChargerID` | a duplicate identity was refused | 0m | hijack | someone else uses a charger's id |
| `IdentityRejections` | handshakes refused (not duplicates): unknown id, bad cert, wrong IP... | 0m | hijack | rogue identities |
| `AuthFailureBurst` | Blocked/Invalid/Expired card refusals in 10 min above the baseline | 0m | fraud | card guessing or cloned cards |
| `MeterUnderReporting` | feeder meter exceeds the reported power by more than 1000 W | 5m | fraud | tampered meter |
| `MeterAnomalies` | the CSMS flagged a meter anomaly | 0m | fraud | backwards or impossible meter |
| `MsgRateAnomaly` | city OCPP message rate above the baseline | 2m | botnet | stations flooding |
| `ChargerMsgFlood` | one charger above 1 msg/s (CSMS sustained limit) | 1m | botnet | a single noisy bot |
| `CityLoadSpike` | feeder load above the baseline | 2m | botnet | coordinated load on the grid |

`ConcurrentTx` is not counted by `AuthFailureBurst`: the simulator draws cards at random and
refuses them regularly (normal traffic, contract 4.4).

## How thresholds are derived

The threshold alerts compare against `baseline:*` series, never against a number in
`alerts.yml`. `baseline.yml` holds constants per city, labelled `basis`.

1. Provisional (committed): load upper = 0.9 x installed capacity (460 kW per city, 414 kW),
   mean = 0.35 x capacity (a guess); message rate mean = 10 chargers x 0.15 msg/s = 1.5 msg/s,
   upper = 3 x mean = 4.5 msg/s; auth refusals upper = 5 per 10 min.
2. Measured: after recording at least 6 h (24 h preferred) of normal load, run
   `monitoring/baseline/run.sh --window 24h`. It samples the three series per city every 60 s,
   computes mean and standard deviation and sets `upper = mean + 3 sigma` (load capped at 0.9 x
   capacity). It rewrites `baseline.yml` with the window, sample count, mean and sigma in the
   header and `basis="24h-YYYYMMDD"`. See `../baseline/README.md`.

## Tests

`promtool test rules tests/*.test.yml` covers every alert with a firing and a quiet case,
including `ChargerOffline` with an absent gauge and `AuthFailureBurst` ignoring `ConcurrentTx`.
`tests/baseline.test.yml` loads `baseline.yml` for `CityLoadSpike`; the other files load
only `alerts.yml` and supply their own `baseline:*` series.

## Limitations

- The simulated load is stationary (no daily cycle), so one band per city is enough; real
  traffic would need hour-of-day bands.
- `ChargerFlapping` is a known false positive during a CSMS `rollout restart`: all chargers
  reconnect at once.
- If an attack runs during the recording window, mean and sigma are inflated and the
  thresholds are too high. Check the dashboards for the window before accepting a baseline.
- With mean + 3 sigma on a nearly constant series, sigma is tiny and the bound sits close
  to the mean; small natural fluctuations can cross it (`for: 2m` absorbs most).
- The load alert cannot fire if the cap (0.9 x capacity) is below the mean; the cap is only a
  ceiling.
