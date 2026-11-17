# Fraud PoC (Phase 7, threat 2: fraud)

Design background: `DESIGN-fraud.md`. Interfaces: `docs/interfaces.md` v6 sections 4.4
(fraud service rules), 6 (event vocabulary), 7.2 (feeder meter), 9.2/9.4 (simulator
flags), 13.3 (station runtime, `sim.env`).

Every script here defaults to `--dry-run`: it prints the exact `sim.env` it would write
and the commands it would run, and changes nothing -- a dry run never calls `incus`. A
live run additionally needs `--run` *and* the environment variable
`EVCHARGE_BASELINE_DONE=1` (set in the environment, once the baseline
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
`aveiro,lisboa`), one city after the other, sets `SIM_EXTRA_ARGS="--clone-card <card>
--clone-sessions 1 --arrival-rate 90 --session-minutes 1"`, restarts `evsim`, waits (polling
the CSMS log, 240 s per city) until the cloned session has started and stopped, restores
that station's `sim.env`, then does the next city; in the last city it stops waiting at
`session.start` (the block happens at the second start) and the exit trap restores. The two `session.start` events a couple
of minutes apart over ~218 km are the impossible-travel signature. The sessions are
sequential on purpose: the CSMS refuses a second concurrent transaction for one card
(`auth.reject result=ConcurrentTx`), which prevents an overlapping clone but neither
alerts nor blocks the card, so no second-city `session.start` would reach the fraud
service. `--duration` (default 480 s) is the overall time-box; `--arrival-rate` and
`--session-minutes` override the fast defaults. At the end the script prints the gap
between the two starts and, if the billing token is available, the card's state.

```
clone_card.sh [--home porto] [--used-in aveiro,lisboa] [--phase before|after] [--card CARD] [--run] [--duration N]
```

`--card` pins the card instead of taking the first roster card of `--home`.

- **Expected detections**: fraud `impossible_travel` (`distance_km`, `speed_kmh` >
  `FRAUD_MAX_KMH`, city coordinates from the registry); Wazuh **110207** (impossible
  travel) and **110208** (fraud service alert).
- **Expected response**: Wazuh **110210** (high-confidence cloned card) -> `block-card`
  (billing: card `Blocked` + the sessions flagged).
- **`--phase after`** (the default): the fraud service is running with
  `FRAUD_DRY_RUN=false` (the lab default) -- expect `fraud.alert impossible_travel` then
  `fraud.action block_card`.
- **`--phase before`**: the fraud service is stopped, or running with
  `FRAUD_DRY_RUN=true` -- the cloned sessions complete unflagged, the detector-off
  baseline for the before/after comparison. This script only changes the printed
  expectation; the operator starts/stops the fraud service between the two calls.

### 2. `tamper_meter.sh` -- meter tamper

Sets `SIM_EXTRA_ARGS` on one station's `sim.env` (default `pt.lisboa.st-003`) to
`--tamper F --tamper-mode rollback|scale|freeze` and restarts `evsim`.

```
tamper_meter.sh [--city lisboa] [--station pt.lisboa.st-003] [--mode rollback|scale|freeze] [--factor 0.5] [--extra-args "SIM FLAGS"] [--run] [--duration N]
```

`--extra-args` appends simulator flags to the tamper flags (for example
`"--session-minutes 3 --arrival-rate 30"`: short, frequent sessions so `power_gap` has enough
completed sessions inside the window -- useful for the `scale` mode).

- **`--mode rollback`** (register jumps back 500 Wh, the default): the fast, blunt case
  the **CSMS anomaly check does catch on its own** -- CSMS `meter.anomaly`
  (`reason=backwards`), fraud `meter_backwards`, Wazuh **110204** (meter anomaly at the
  charger). Default because it shows a result in seconds, not after a run-up of samples.
- **`--mode scale`** (under-report): the readings the charger sends stay internally
  consistent, so the **CSMS anomaly check cannot see it** -- it is caught only by the
  **real-time `power_gap` fraud rule** (feeder power vs reported power) and Prometheus
  `MeterUnderReporting` (`city:station_power_gap_watts`). The point of this mode is that
  under-reporting is invisible to the CSMS and caught only by the independent out-of-band
  feeder-meter gap; it needs enough charging samples, so it is slower and not the default.
- **`--mode freeze`** (register stops mid-session): fraud `peer_deviation` (station well
  below its city peers) and `grid_gap` against the feeder meter.
- **Expected response** (all modes): `quarantine-charger` (operator-applied: isolate +
  flag the station's recent sessions for review -- not a card block, the card is innocent).

### `run.sh` -- both scenarios in order

Runs `clone_card.sh --phase after` then `tamper_meter.sh --mode rollback`, with a banner
before each (what, expected detection, expected response), and finishes by checking that
every `sim.env` this run touched (`pt.aveiro.st-001`, `pt.lisboa.st-001`,
`pt.lisboa.st-003`) is back to the content it held before this script started. Each
scenario keeps its own fast default time-box (clone ~2-6 min, rollback tamper ~90 s);
`--duration` overrides both.

```
run.sh [--run] [--duration N]
```

### `selfcheck.sh`

Runs every script above on its `--dry-run` path, checks each exits 0 and prints a plan,
and checks that `--run` is refused without `EVCHARGE_BASELINE_DONE=1`. Changes nothing.

## Before/after steps the run performs, in order

1. **Baseline**, nothing from this PoC has run yet. `EVCHARGE_BASELINE_DONE` unset; the
   fraud service is either stopped or running with `FRAUD_DRY_RUN=true`.
2. `clone_card.sh --run --phase before`: the cloned-card sessions complete with no
   `block-card` -- shows the cost of running without the accounting detector.
3. Start the fraud service with `FRAUD_DRY_RUN=false` (or restart it that way).
4. `clone_card.sh --run --phase after`: same scenario, now blocked -- the value of the
   detector, captured as a before/after pair.
5. `tamper_meter.sh --run --mode rollback` (the default): the blunt case the CSMS anomaly
   check *does* catch -- CSMS `meter.anomaly` (`reason=backwards`), Wazuh 110204, then the
   `quarantine-charger` response. Fast to show.
6. `tamper_meter.sh --run --mode scale` (optional contrast): show the CSMS does *not* flag
   it (no `meter.anomaly`) while Prometheus `MeterUnderReporting` and the fraud `power_gap`
   alert both fire -- the CSMS-anomaly-check-vs-feeder-gap comparison from `DESIGN-fraud.md`.
7. Capture evidence per `DESIGN-fraud.md` "Evidence to capture" into `evidence/fraud/`:
   the cloned-card sessions in both cities, the `impossible_travel` alert, the Wazuh
   110207/110208 alert, the `block-card` `ar.action` (110210), billing showing the card
   `Blocked`; the meter.anomaly / feeder-vs-reported power gap, the `quarantine-charger`
   response; the detect -> locate -> mitigate timeline.
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
