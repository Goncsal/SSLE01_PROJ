# fraud (evfraud)

Scores cards and chargers from the events the CSMS posts (`POST /events`), raises
`FraudAlert`s and, for the critical rules, asks billing, the registry and the CSMS to
isolate. It is a second detector beside the Wazuh fraud rules (110200-110209) on the same
facts; the correlator joins the two. API and contract: `docs/interfaces.md` section 4.4.

## Rules

Each rule is a pure function in `src/evfraud/rules.py`; the scorer (`scorer.py`) loads the
stored state, calls them and writes alerts.

| Rule | Fires when | Severity | Weight | Action |
|---|---|---|---|---|
| `impossible_travel` | same card starts sessions in two cities and `km / hours` exceeds `FRAUD_MAX_KMH` | critical | 0.95 | `block_card` |
| `grid_gap` | energy the CSMS saw is below `FRAUD_GRID_FACTOR` of the feeder meter over `FRAUD_WINDOW_H` (needs 1 kWh on the feeder) | critical | 0.90 | `quarantine` |
| `over_energy` | stopped session `energy_wh` > `max_kw x hours x 1000 x FRAUD_ENERGY_TOL` | warning | 0.75 | flag the session |
| `meter_backwards` | `meter.anomaly` with reason `backwards` or `stop_mismatch` | warning | 0.75 | flag the session |
| `peer_deviation` | a station's mean energy per session is below `FRAUD_PEER_FACTOR` x the median of its city's other stations, with at least `FRAUD_MIN_SESSIONS` peer sessions (and 3 of its own) | warning | 0.70 | none |
| `card_velocity` | more than `FRAUD_MAX_SESSIONS_H` session starts for one card in an hour | warning | 0.70 | none |

Boundaries are strict: a speed exactly at the limit, an energy exactly at the limit or a
ratio exactly at the factor do not fire. A single start in another city (what the CSMS
reports as `ConcurrentTx` traffic) is never a finding: only distance over time is.

## Scoring

A rule adds its weight; a finding whose score reaches `FRAUD_ALERT_SCORE` (0.7) is written
to `fraud_alerts`, logged as `fraud.alert` and counted. A repeat of the same subject
inside a cool-down (15 min for a card's travel, 1 h for a station, per session for energy
and meter rules) is not alerted again. `GET /scores/cards/{card}` and
`/scores/chargers/{hier_id}` combine the alerts of the last `FRAUD_WINDOW_H` hours as
`1 - prod(1 - score)`; a charger's score includes alerts about its station. Time in the
rules is the event time, so a recorded stream replays the same way.

## Data

Tables `card_events`, `station_stats`, `fraud_alerts` (`src/evfraud/db.py`), created at start
with `evcommon.dbschema.add_missing_columns`. Rows older than 48 h are pruned by the grid
poller. Station coordinates come from the registry (`GET /nodes/{station}`, falling back to
the city node) and are cached. The feeder meter is read from Prometheus
(`city:station_grid_power_watts:sum`, every `FRAUD_GRID_POLL_S`) and integrated into energy;
without `FRAUD_PROMETHEUS_URL` the poller is off and `grid_gap` stays quiet.
Limitation: sessions still open count on the feeder but not yet in the CSMS energy, so a
window that ends mid-session reads low; the 1 kWh floor and the 0.8 factor absorb it.

## Responses

Only isolate-only responder tokens are used, never admin tokens:

- `block_card`: billing `POST /cards/{card}/block`, then `POST /sessions/{id}/flag` for the
  card's sessions in the window.
- `flag_session`: billing `POST /sessions/{id}/flag`.
- `quarantine`: registry `POST /quarantine/{station}`, then for every charger the CSMS
  reports connected under it, `change-availability` (`Inoperative`, connector 0) and
  `disconnect`. The station is quarantined because the feeder meter cannot tell which
  charger under-reports.

Every call carries `reason`, `source=fraud`. Each one logs `fraud.action` (`rule`, `target`,
`result`, `action`) after the `fraud.alert` line. Results: `ok`, `error`, `dry_run`,
`rate_limited`, `not_configured`, `skipped`. Block and quarantine are limited to
`FRAUD_MAX_ACTIONS_H` (10) per hour. Critical alerts are also posted to the correlator
(`POST /signals/fraud`). Actions run after the `202` is sent, so a slow peer never delays
the CSMS.

## Dry run

`FRAUD_DRY_RUN=true`: alerts are written and logged, each intended action is logged with
`result=dry_run`, and no billing, registry (writes), CSMS or correlator call is made. The
registry is still read for coordinates and Prometheus for the feeder meter. Used for the
baseline and the first deploy.

## Environment

Section 3.2 of the contract, plus three additions: `FRAUD_MAX_ACTIONS_H` (10),
`FRAUD_PROMETHEUS_URL` (empty) and `FRAUD_GRID_POLL_S` (30). Tokens must be at least 16
characters. Required: `FRAUD_API_TOKEN`. Peers: `BILLING_URL`, `BILLING_RESPONDER_TOKEN`,
`REGISTRY_URL`, `REGISTRY_RESPONDER_TOKEN`, `CSMS_API_URL`, `CSMS_RESPONDER_TOKEN`,
`CORRELATOR_URL`, `CORRELATOR_API_TOKEN`; a missing pair means that action logs
`not_configured`.

## Run and test

```
pip install -c constraints.txt -e common -e 'fraud[test]'
FRAUD_API_TOKEN=... python -m evfraud
cd fraud && python -m pytest -q
```

`tests/events.jsonl` is a short recorded stream; `test_replay_of_recorded_events` replays it.

## Deployment (Phase 7)

`k8s/fraud.yaml` (Deployment of 1, Service `fraud` ClusterIP 8003) is kept out of
`k8s/kustomization.yaml` until the RUN card enables it. Needs: a Postgres role and database
`fraud` (the init script only runs on an empty volume, so create them by hand), the image
`docker.incus:5000/evcharge-fraud`, and Secret `fraud-env`. It starts with
`FRAUD_DRY_RUN=true`; the RUN card sets it to `false` for the live PoC. The CSMS needs
`FRAUD_URL=http://fraud:8003` and `FRAUD_API_TOKEN` to send events.
