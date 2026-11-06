# Fraud PoC (Phase 7, threat 2: fraud)

Design background: `DESIGN-fraud.md`. Interfaces: `docs/interfaces.md` v6 sections 4.4
(fraud service rules), 6 (event vocabulary), 7.2 (feeder meter), 9.2/9.4 (simulator
flags), 13.3 (station runtime, `sim.env`).

Every script here defaults to `--dry-run`: it prints the exact `sim.env` it would write
and the commands it would run, and changes nothing -- a dry run never calls `incus`. A
live run additionally needs `--run` *and* the environment variable
`EVCHARGE_BASELINE_DONE=1` (set only by the matching `W7-run` card, once the baseline
recording is captured). Every live scenario is time-boxed (`--duration`) and restores the
station's `sim.env` to exactly what it held before, on exit, success, failure or
`INT`/`TERM` -- a station is never left cloning or tampering.

Both scenarios reuse the simulator's existing attack flags (`--clone-card`,
`--clone-sessions`, `--tamper`, `--tamper-mode`, section 9.2/9.4): no new attacker
containers are created, only `/etc/evcharge/sim.env` on an already-running station, and
`systemctl restart evsim` to pick it up.

## Scenarios

### 1. `clone_card.sh` -- cloned card, impossible travel

Picks a non-blocked card whose home city is `--home` (default `porto`) from
`dev/roster.yaml`, then on one station in each `--used-in` city (default
`aveiro,lisboa`) sets `SIM_EXTRA_ARGS="--clone-card <card> --clone-sessions 2"` and
restarts `evsim`. The same card id then authorises sessions in two cities within
minutes -- the impossible-travel signature.

```
clone_card.sh [--home porto] [--used-in aveiro,lisboa] [--phase before|after] [--run] [--duration N]
```

- **Expected detections**: fraud `impossible_travel` (`distance_km`, `speed_kmh` >
  `FRAUD_MAX_KMH`, city coordinates from the registry); Wazuh **110203** (`ConcurrentTx`
  across cities) and **110206** (`session.start` across cities).
- **Expected response**: `block-card` (billing: card `Blocked` + the sessions flagged).
- **`--phase before`**: the fraud service is stopped, or running with
  `FRAUD_DRY_RUN=true` -- the cloned sessions complete unflagged. This is the
  detector-off baseline.
- **`--phase after`**: the fraud service is running with `FRAUD_DRY_RUN=false` -- expect
  `fraud.alert impossible_travel` then `fraud.action block_card`. This script only
  changes the printed expectation; the RUN card starts/stops the fraud service itself
  between the two calls.

### 2. `tamper_meter.sh` -- meter tamper

Sets `SIM_EXTRA_ARGS` on one station's `sim.env` (default `pt.lisboa.st-003`) to
`--tamper F --tamper-mode scale|rollback|freeze` and restarts `evsim`.

```
tamper_meter.sh [--city lisboa] [--station pt.lisboa.st-003] [--mode scale|rollback] [--factor 0.5] [--run] [--duration N]
```

- **`--mode scale`** (under-report, default): the readings the charger sends stay
  internally consistent, so the **CSMS anomaly check cannot see it** -- it is caught by
  the **feeder-meter `grid_gap` fraud rule** and Prometheus `MeterUnderReporting`
  (`city:station_power_gap_watts`). **Tampering under-reporting is invisible to the
  CSMS and caught only by the independent out-of-band feeder-meter gap** -- the point of
  this PoC.
- **`--mode rollback`** (register decreases): CSMS `meter.anomaly` (`reason=backwards`),
  fraud `meter_backwards`, Wazuh 110204/110205 (meter.anomaly frequency per charger).
- **`--mode freeze`** (register stops mid-session): fraud `peer_deviation` (station well
  below its city peers) and `grid_gap` against the feeder meter.
- **Expected response** (all modes): `quarantine-charger` (isolate + flag the station's
  recent sessions for review -- not a card block, the card is innocent).

### `run.sh` -- both scenarios in order

Runs `clone_card.sh --phase after` then `tamper_meter.sh --mode scale`, with a banner
before each (what, expected detection, expected response), and finishes by checking that
every `sim.env` this run touched (`pt.aveiro.st-001`, `pt.lisboa.st-001`,
`pt.lisboa.st-003`) is back to the content it held before this script started.

```
run.sh [--run] [--duration N]
```

### `selfcheck.sh`

Runs every script above on its `--dry-run` path, checks each exits 0 and prints a plan,
and checks that `--run` is refused without `EVCHARGE_BASELINE_DONE=1`. Changes nothing.

## Before/after steps the RUN card performs, in order

1. **Baseline**, nothing from this PoC has run yet. `EVCHARGE_BASELINE_DONE` unset; the
   fraud service is either stopped or running with `FRAUD_DRY_RUN=true`.
2. `clone_card.sh --run --phase before`: the cloned-card sessions complete with no
   `block-card` -- shows the cost of running without the accounting detector.
3. Start the fraud service with `FRAUD_DRY_RUN=false` (or restart it that way).
4. `clone_card.sh --run --phase after`: same scenario, now blocked -- the value of the
   detector, captured as a before/after pair.
5. `tamper_meter.sh --run --mode scale`: show the CSMS does not flag it (no
   `meter.anomaly`) while Prometheus `MeterUnderReporting` and the fraud `grid_gap` alert
   both fire -- the CSMS-anomaly-check-vs-grid-gap comparison from `DESIGN-fraud.md`.
6. `tamper_meter.sh --run --mode rollback` (separately): the blunt case the CSMS anomaly
   check *does* catch, for contrast.
7. Capture evidence per `DESIGN-fraud.md` "Evidence to capture" into `evidence/fraud/`:
   the cloned-card sessions in both cities, the `impossible_travel` alert, the Wazuh
   110203/110206 alert, the `block-card` `ar.action`, billing showing the card `Blocked`;
   the feeder-vs-reported power gap, the `grid_gap` alert, the quarantine; the
   detect -> locate -> mitigate timeline.
8. `run.sh` itself is also available to run both scenarios back to back with one
   command, for a combined timeline.

## Lab-safety notes

- **No new containers**: both scripts only edit `/etc/evcharge/sim.env` on an
  already-running station and restart `evsim`; nothing else in the lab is touched.
- **Save/restore, not just "set it back"**: each script reads the station's current
  `sim.env` before its first edit and registers the restore as a cleanup action, so the
  original content comes back even if the script is killed mid-run (`INT`/`TERM`) or
  fails partway through -- never leaves a station cloning or tampering.
- **Time-box**: every live scenario takes `--duration` and the station runs the tamper
  or clone flags only for that window before the restore.
- **Two independent detectors on purpose**: the fraud service and Wazuh read the same
  JSON logs by different routes; the design note and the report compare what each one
  catches (and whether the correlator merges or double-counts them), rather than relying
  on either alone.
