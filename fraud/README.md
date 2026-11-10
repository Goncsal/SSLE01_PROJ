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
| `impossible_travel` | same card starts sessions in two cities and `km / hours` exceeds `FRAUD_MAX_KMH` (alert), or an earlier session in another city has not stopped (overlap) | critical | 0.95 | `block_card` only on high confidence: overlap, or speed at least `FRAUD_BLOCK_KMH`; otherwise none |
| `power_gap` | feeder power minus reported power (`city:station_power_gap_watts`) is at least `FRAUD_POWER_GAP_W` and `FRAUD_POWER_GAP_RATIO` of the feeder power, in `FRAUD_POWER_GAP_FRAC` of the charging samples of the last `FRAUD_POWER_GAP_WINDOW_S`, with at least `FRAUD_POWER_GAP_MIN_SAMPLES` charging samples | critical | 0.90 | `quarantine` |
| `grid_gap` | energy the CSMS saw is below `FRAUD_GRID_FACTOR` of the feeder meter over `FRAUD_WINDOW_H`, needs 1 kWh on the feeder, `FRAUD_GRID_MIN_SESSIONS` completed sessions with `FRAUD_GRID_MIN_REPORTED_WH` reported, the newest `FRAUD_GRID_SETTLE_S` of feeder energy left out, and the same gap `FRAUD_GRID_SUSTAIN_S` earlier | warning | 0.75 | none (alert only) |
| `over_energy` | stopped session `energy_wh` > `max_kw x hours x 1000 x FRAUD_ENERGY_TOL` | warning | 0.75 | flag the session |
| `meter_backwards` | `meter.anomaly` with reason `backwards` or `stop_mismatch` | warning | 0.75 | flag the session |
| `peer_deviation` | a station's mean energy per session is below `FRAUD_PEER_FACTOR` x the median of its city's other stations, with at least `FRAUD_MIN_SESSIONS` peer sessions (and 3 of its own) | warning | 0.70 | none |
| `card_velocity` | more than `FRAUD_MAX_SESSIONS_H` session starts for one card in an hour | warning | 0.70 | none |

Detection and response are separate on purpose. `impossible_travel` ALERTS above
`FRAUD_MAX_KMH` (200) but only auto-blocks when the sessions overlap (same card, other city,
earlier session not stopped, younger than `FRAUD_CONCURRENT_MAX_AGE_S`, 3600) or the implied
speed is at least `FRAUD_BLOCK_KMH` (400, never below `FRAUD_MAX_KMH`): honest roaming in the
simulation sits right above 200 km/h (201 seen), the live clone read 1312. `grid_gap`
compares cumulative energy over six hours; honest stations span 0.71 to 1.38 there and honest
history dilutes a real tamper, so it only alerts. The quarantine trigger is `power_gap`,
the near-real-time signal behind `MeterUnderReporting`: honest stations read feeder equal to
reported power (gap 0), a scale-0.5 tamper reads a feeder of twice the reported power. Only
samples where the station is charging (feeder at least `FRAUD_POWER_GAP_W`) count. Its samples
are kept in memory (the window is minutes), so a restart needs the window to refill.

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
without `FRAUD_PROMETHEUS_URL` the poller is off and `grid_gap` and `power_gap` stay quiet.
Timing lag versus tampering: the CSMS books a session's energy only at `session.stop`, the
feeder meter integrates continuously, so a station that is merely charging reads low. Four
settings keep that quiet: `FRAUD_GRID_MIN_SESSIONS` (2) and `FRAUD_GRID_MIN_REPORTED_WH` (500)
need completed sessions to compare against; `FRAUD_GRID_SETTLE_S` (900) leaves the newest
feeder energy out, since its session has not stopped; `FRAUD_GRID_SUSTAIN_S` (600) requires
the same under-factor reading, with energy already booked, that long before. A meter that
keeps reporting about half of the feeder across completed sessions stays under the factor at
every point and fires; lag closes as sessions stop and does not. Limitation: a session longer
than the settle lag that starts after energy was booked can still read low.

## Responses

Only isolate-only responder tokens are used, never admin tokens:

- `block_card`: billing `POST /cards/{card}/block`, then `POST /sessions/{id}/flag` for the
  card's sessions in the window.
- `flag_session`: billing `POST /sessions/{id}/flag`.
- `quarantine`: registry `POST /quarantine/{station}`, then for every charger the CSMS
  reports connected under it, `change-availability` (`Inoperative`, connector 0) and
  `disconnect`, then billing `POST /sessions/{id}/flag` for the sessions started at the
  station in the window (review markers, nothing is reversed). The station is quarantined because the feeder meter cannot tell which
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

Every `fraud.alert` log line carries `dry_run` (the setting) and `auto_action` (`block_card`,
`quarantine`, `flag_session` or `none`: what the service would do). Wazuh's block-card rule
(110210) requires `auto_action=block_card` and `dry_run=false`, so a dry run suppresses the
Wazuh response as well. Dry run alone never did: it only gates this service's own API calls.

## Environment

Section 3.2 of the contract, plus these additions: `FRAUD_MAX_ACTIONS_H` (10),
`FRAUD_PROMETHEUS_URL` (empty) and `FRAUD_GRID_POLL_S` (30), `FRAUD_BLOCK_KMH` (400), `FRAUD_CONCURRENT_MAX_AGE_S` (3600), `FRAUD_POWER_GAP_W` (1000), `FRAUD_POWER_GAP_RATIO` (0.3), `FRAUD_POWER_GAP_WINDOW_S` (900), `FRAUD_POWER_GAP_MIN_SAMPLES` (10), `FRAUD_POWER_GAP_FRAC` (0.8), `FRAUD_GRID_MIN_SESSIONS` (2), `FRAUD_GRID_MIN_REPORTED_WH` (500), `FRAUD_GRID_SETTLE_S` (900) and `FRAUD_GRID_SUSTAIN_S` (600). Tokens must be at least 16
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
