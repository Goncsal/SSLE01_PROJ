# W7 -- fraud: cloned cards and meter tampering, evidence and summary

Lab-only, time-boxed runs against the live platform (Aveiro plain `ws://`, Porto and Lisboa
mTLS; 30 chargers). Fraud service deployed from `k8s/fraud.yaml`, wired to the CSMS. Two
independent detectors are shown side by side on purpose: the **fraud service** (`fraud/`,
contract 4.4) and **Wazuh** (rules 110200-110209, active responses `block-card` /
`quarantine-charger`). The correlator is not deployed yet, so the fraud service's post to it
logs a dependency error -- expected, and the two detectors are captured separately here.
All times are UTC, 2026-11-09. Scripts: `poc/fraud/`. Raw captures sit next to this file.

## Summary

| Scenario | Fraud service | Wazuh | Response | Result |
|---|---|---|---|---|
| Cloned card, dry-run (before) | `impossible_travel` 975 km/h, `fraud.action block_card result=dry_run` | 110207 fires; `block-card` refused (card allow-listed for this step) | none | cloned sessions complete **unflagged**, card stays active |
| Cloned card, enforcing (after) | `impossible_travel` 1312 km/h, `fraud.action block_card ok` | 110207, `block-card ok` (110410), 110209 x5 sessions flagged | card **Blocked**, sessions flagged | later authorize returns `Blocked`, CSMS `auth.reject Blocked` |
| Meter rollback | `meter_backwards` x3 (flag session) | 110204 x3, 110208 x3, 110209 x3 | session flagged | CSMS `meter.anomaly backwards` x3 |
| Meter scale (under-report) | `peer_deviation` (warning) at +7 min of run 2; `grid_gap` (critical) at +33 min of run 2 | 110208, 110405 (quarantine) | `quarantine` + `change-availability` + `disconnect` on both chargers | Prometheus `MeterUnderReporting` firing (gap up to 34 kW); CSMS raised nothing |

## Settings in force

- `FRAUD_GRID_SETTLE_S` raised from 900 to **1400** s (env on the deployment and in
  `k8s/fraud.yaml`; no image rebuild). Reason: normal simulated sessions are log-normal, mean
  8 min, sigma 0.5, clipped to [2, 32] min, so the median is 7.1 min and the 99th percentile
  is about 22.6 min (1357 s). At 900 s about 7 % of normal sessions are still open when the
  settle ends, which lets the timing lag (energy is booked at session stop, the feeder meter
  integrates continuously) look like under-reporting. 1400 s covers about 99.2 % of sessions
  and leaves only the clipped tail to 32 min (about 0.1 %). Other guards unchanged:
  `FRAUD_GRID_MIN_SESSIONS=2`, `FRAUD_GRID_MIN_REPORTED_WH=500`, `FRAUD_GRID_SUSTAIN_S=600`,
  `FRAUD_GRID_FACTOR=0.8`, `FRAUD_MAX_KMH=200`.
- Normal-traffic check after the change (13:24 to 13:33 UTC, about 9 min of `/alerts` and
  pod logs): **no `grid_gap`**, none since 12:33 (before the guards). Actually quiet for the
  grid rule, but not for `impossible_travel` (see the false-positive section).
- `FRAUD_DRY_RUN`: `true` for the dry-run step, `false` for the supervised enforcing steps,
  **back to `true` at the end** (env and manifest). Deliberate deviation from the earlier
  "leave `false`" acceptance, because of the false positives below.

## Scenario 1 -- cloned card (impossible travel)

Command (repo root, `EVCHARGE_BASELINE_DONE=1`):

```
poc/fraud/clone_card.sh --run --phase before --card E3EE5BE2ED4120 --duration 900   # dry-run
poc/fraud/clone_card.sh --run --phase after  --card E3EE5BE2ED4120 --duration 900   # enforcing
```

Card `E3EE5BE2ED4120` (driver home city Porto) is presented by `aveiro-st-001` and
`lisboa-st-001` (`--clone-card ... --clone-sessions 2`). `sim.env` of both stations is
restored by the script on exit; verified empty `SIM_EXTRA_ARGS` afterwards.

**Expected**: fraud `impossible_travel` with `distance_km` and `speed_kmh` above 200;
`block-card`; later authorizations refused.

### Before: fraud service in dry-run (13:34 to 13:49)

Observed, `clone-card/before-dry-run/`:

```
13:34:06.079 csms  session.start card E3EE5BE2ED4120 pt.lisboa.st-001.cp-2 tx 4039
13:34:06.116 fraud fraud.alert  impossible_travel: started a session in porto and 16.9 min later in lisboa: 274 km at 975 km/h (limit 200)
13:34:06.132 fraud fraud.action block_card E3EE5BE2ED4120: dry_run  (result=dry_run, nothing called)
13:34:06.143 wazuh 110207 Fraud service: impossible travel for E3EE5BE2ED4120
13:34:06.199 wazuh ar.action block-card refused: allowlisted
```

Evidence in `alerts/impossible_travel-cloned-card.json` (`distance_km` 274.2, `speed_kmh`
974.6, `minutes_apart` 16.88, `transaction_ids` [4004, 4039]). The Aveiro clone session
(tx 4049, 13:41:47) fell inside the 10 min dedup window of the first alert and added no
second alert. Billing afterwards (`billing-sessions-unflagged.txt`): sessions 4039 and 4049
`closed`, `flag_reason null`; the card `active`. **The fraud completed, undetected by the
accounting layer.**

To keep this step free of state change, the Wazuh side had to be neutralised on purpose:
`block-card` is wired to rule 110207, which is built on the fraud service's `fraud.alert`
log line, and that line is written in dry-run too (see "Findings"). The card was put on the
manager's `card-allowlist` for this step only (the active response then logs `refused:
allowlisted`, rule 110411); the list was emptied again before the enforcing step. Wazuh
110203/110206 did **not** fire in either run: they need two events for one card in two
cities within 120 s, and the cloned sessions were 8 to 17 minutes apart. The 120 s window
was chosen deliberately in the rule file (normal roaming trips the wider window); the
distance-aware detector is the fraud service (110207).

### After: enforcing (13:49 to 14:04)

`FRAUD_DRY_RUN=false`, allow-list empty, Wazuh block-rate state reset (see Findings).
Observed, `clone-card/after-enforcing/`:

```
13:51:47.490 csms  session.start card E3EE5BE2ED4120 pt.lisboa.st-001.cp-2 tx 4065
13:51:47.513 fraud fraud.alert  impossible_travel: started a session in aveiro and 10.0 min later in lisboa: 218 km at 1312 km/h (limit 200)
13:51:47.548 fraud fraud.action block_card E3EE5BE2ED4120: ok
13:51:47.623 wazuh 110207 Fraud service: impossible travel for E3EE5BE2ED4120
13:51:47.702 wazuh ar.evidence block-card evidence sessions
13:51:47.711 wazuh ar.action   block-card ok: card blocked   (rule 110410, idempotent after the fraud service)
13:51:48.313 wazuh 110209 billing.card_blocked ; 110209 session 3963/4004/4039/4049/4065 flagged
```

Billing (`billing-card-blocked.json`, `billing-sessions-flagged.json`): card `status blocked`,
reason "fraud rule impossible_travel ... 218 km at 1312 km/h", `blocked_at 13:51:47.5`; five
sessions `flagged`, including the two earlier cloned ones. `POST /authorize` for the same card
at an Aveiro charger returns `{"status":"Blocked"}` (`billing-authorize-blocked.json`), and
the simulated clone attempts that followed were refused by the CSMS (`auth.reject`,
`result=Blocked`, 13:53:54 Porto, 13:54:25 Lisboa, 13:56:28 Aveiro; `csms.jsonl`).

### Timeline and detect, locate, mitigate

| t | Event |
|---|---|
| +0 ms | second-city `session.start` (tx 4065) |
| +23 ms | fraud service **detects**: `impossible_travel`, `hier_id` pt.lisboa.st-001.cp-2 vs pt.aveiro.st-001.cp-1 (**locates** both stations and the card) |
| +58 ms | **mitigates**: billing card block by the fraud service |
| +133 ms | Wazuh 110207 alert, `block-card` runs and finds the card already blocked (ok, idempotent) |
| +0.8 s | all five sessions of the card flagged |

Both detectors agree, by different routes (fraud service: card events plus city coordinates;
Wazuh: same JSON log line). The response is the same one twice, harmlessly. Without the
correlator they are not merged into one incident.

### Before / after

Dry-run: two cloned sessions (2136 Wh and 1953 Wh) completed and were billed to the holder,
card active, nothing flagged. Enforcing: the second cloned start was detected inside 25 ms,
the card blocked within 60 ms, every recent session flagged and all later authorizations
refused. That is the value of the accounting detector. **Collateral**: the real holder is
blocked too; the follow-up is to contact the holder and re-issue the card (unblock is an
operator action only; the responders cannot unblock).

## Scenario 2a -- meter rollback (CSMS-visible, blunt case)

```
poc/fraud/tamper_meter.sh --run --station pt.porto.st-003 --mode rollback \
    --extra-args "--session-minutes 3 --arrival-rate 30" --duration 900     # 13:52:47 to 14:07:47
```

Expected: CSMS `meter.anomaly backwards`; fraud `meter_backwards`; Wazuh 110204 (and 110205 for
three on one charger within 10 min). Observed (`tamper-rollback/`, `alerts/meter_backwards-*.json`):

```
13:54:43.9 csms  meter.anomaly backwards pt.porto.st-003.cp-2 tx 4066   -> fraud meter_backwards (flag session) ; Wazuh 110204, 110208 (13:54:44-45)
13:56:38.4 csms  meter.anomaly backwards pt.porto.st-003.cp-1 tx 4070   -> 110204, 110208, 110209 session flagged
13:59:12.9 csms  meter.anomaly backwards pt.porto.st-003.cp-2 tx 4073   -> 110204, 110208, 110209 session flagged
```

Result: 3 anomalies, 3 fraud alerts, 3 sessions flagged, in about 2 min each. **110205 did not
fire**: it counts three anomalies on the same `hier_id` (charger) in 600 s and these were
split over two chargers (2 on cp-2, 1 on cp-1, 4.5 min apart for cp-2). A longer run or one
charger would reach it; it is not claimed. The rollback happens once per session at a fixed
500 Wh, and sessions only came every 2 to 3 min on this station, so the run was not extended.
`sim.env` of `porto-st-003` restored by the script and verified.

## Scenario 2b -- meter under-reporting (`scale`), invisible to the CSMS

```
poc/fraud/tamper_meter.sh --run --station pt.lisboa.st-004 --mode scale --factor 0.5 \
    --extra-args "--session-minutes 3 --arrival-rate 30" --duration 4800   # run 1, 13:52:34, stopped 14:34
poc/fraud/tamper_meter.sh --run --station pt.lisboa.st-004 --mode scale --factor 0.1 \
    --extra-args "--session-minutes 3 --arrival-rate 30" --duration 3000   # run 2, 14:34:09, stopped 15:08
```

The station `pt.lisboa.st-004` was chosen for its small honest history in the 6 h window. No
CSMS `meter.anomaly` was raised for it (the readings stay internally consistent).

**Feeder versus reported (the plot data).** Prometheus recording rule
`city:station_power_gap_watts{hier_id="pt.lisboa.st-004"}` (30 s step,
`tamper-scale/prom-station_power_gap_watts-lisboa-st-004.json`): the gap reaches **about 27.8
kW at 13:55 and up to 34 kW** (feeder power minus reported power; 0 W whenever nothing is
charging, in line with an honest station). `MeterUnderReporting` (`> 1000 W for 5 m`) was
`pending` from 13:55 and **`firing` from 14:15:30** (23 min after the tamper began; the
`for: 5m` resets whenever the station is idle between sessions), again at the end until
15:08:30 (`prom-MeterUnderReporting-ALERTS-range.json`). Plot: x = time, y1 = feeder power,
y2 = reported power, y3 = the gap series above.

**fraud service.** Run 1 at `--factor 0.5` ran 41 min and **did not make `grid_gap` fire**:
the rule works on 6 h cumulative energy and the station already held about 14 kWh of honest
history, so after 40 min the ratio was still 1.10 (reported 21.3 kWh against 19.3 kWh settled
feeder). It was stopped and run 2 used `--factor 0.1`. Observed:

```
14:41:23 fraud peer_deviation: station pt.lisboa.st-004 averages 1043 Wh per session, 48% of the lisboa median (2184 Wh)  -> Wazuh 110208 (warning, no action)
14:52 ratio reported/settled feeder 0.847 ; 15:01 0.744 (below the 0.8 limit; sustain check not yet true)
15:08:02.058 fraud fraud.alert grid_gap: station pt.lisboa.st-004 reported 22518 Wh against 31057 Wh on the feeder meter (73%, limit 80%), sustained over 10 min, 27 sessions
15:08:02.085 fraud fraud.action quarantine pt.lisboa.st-004: ok
15:08:02.127-.210 change-availability + disconnect on cp-1 and cp-2: ok
15:08:02.341 wazuh 110405 quarantine pt.lisboa.st-004 ; 110208 fraud service alert grid_gap
```

So `grid_gap` fired live **33 min after the second run started and 76 min after the first
tamper began**. The latency is structural, not a defect of the PoC: energy is booked at
session stop (settle 1400 s), the gap must also have held 600 s earlier (sustain), and the
ratio is cumulative over 6 h so earlier honest energy dilutes it. The fast signal is the
Prometheus instantaneous gap (firing at +23 min, and nonzero from the first charging session
at +3 min). Registry state `registry-quarantine.json`: `pt.lisboa.st-004` `quarantined`,
`state_source fraud`, reason the `grid_gap` sentence. The quarantine isolates the station
(the card is innocent); the station was released afterwards, see Restore.

**Billing "flag the station's recent sessions for review" did NOT happen.** The
quarantine action only calls the registry and the CSMS (contract 4.4 lists no session flag
for it), so the tampered sessions stay `closed` in billing (only unrelated sessions were
flagged by `impossible_travel` cards). The design note (`DESIGN-fraud.md`, response section)
promises the flag; see "Contract problems".

### Detect, locate, mitigate (tamper)

| t after tamper start | Event |
|---|---|
| run 1 +3 min (13:55) | first tampered session; feeder gap 27 kW in Prometheus |
| run 1 +23 min (14:15:30) | `MeterUnderReporting` firing |
| run 2 +7 min (14:41:23) | `peer_deviation` (warning, no action) |
| run 2 +33 min (15:08:02) | `grid_gap` (critical) -> quarantine of both chargers (mitigation, within 0.15 s) |
| +76 min after the first tamper | total detect-to-mitigate latency of the accounting rule |

## False positives and tuning

- **Roaming versus impossible travel** (resolved later by the city-local card selection, see
  "Travel-time fix in the simulator"). The simulator draws 20 % of sessions on a card of
  another city and does not model travel time, so a roaming driver "teleports". Over the
  2.8 h the fraud service was up there were **26 `impossible_travel` alerts, 24 of them on
  honest cards** (about 9 per hour; speeds 220 to 975 km/h, e.g. Porto to Aveiro 59 km in 9
  min). In dry-run each one still became a Wazuh `block-card` through rule 110207, and in
  enforcing mode the fraud service blocked the card itself. Between 12:44 and 13:49 alone 10
  distinct honest cards were blocked. I unblocked them at each restore. The 200 km/h
  limit is right for real traffic; the data set is what is unrealistic. Real mitigations:
  alert only when the earlier session is still open or ended less than minutes before, use
  a card-level hold (flag, not block) for the first alert, and require a second signal.
- **`grid_gap` lag versus tamper.** Raising the settle from 900 to 1400 s removed the early
  burst (11 `grid_gap` alerts at 12:26-12:33, all startup artefacts). It did not remove a
  real false positive: **`pt.porto.st-002` was quarantined by `grid_gap` twice, at 14:08:13
  (79 %) and 15:08:32 (71 %)** while the platform was in enforcing mode; both were released
  (`false-positive-grid-gap-porto-st-002/`). The honest ratio of reported to settled feeder
  energy over the 6 h window spans 0.71 to 1.38 across the 15 stations
  (`station-ratios-at-15-10.txt`); the tampered station ended at 0.71, equal to this honest
  one. A 0.8 limit on the cumulative ratio therefore cannot separate a 10 % tamper from honest
  variation; it needs a per-station baseline or a shorter, power-based window. The
  Prometheus gap (27 to 34 kW against a few kW at most for honest stations) separates
  cleanly and is what should drive the quarantine. The previous ~7 %/900 s finding stands as
  the lag part; this is the variance part.
- **Enforcing mode is not safe to leave unattended**, hence `FRAUD_DRY_RUN=true` at the end.

## Hardening after the false positives (2026-11-09, afternoon)

The runs above showed honest targets being hit by automatic responses, so I changed what is
allowed to act automatically. Principle: detect sensitively, act automatically only on high
confidence, prefer isolating and keeping evidence, avoid collateral damage. Nothing below was
re-attacked live; the threat-catch is covered by unit tests and by the captures above.

- **Interim stop.** The `block-card` active response was commented out of the live
  `ossec.conf` and the manager restarted while the rest was redesigned; it was re-enabled
  only after the changes below were deployed.
- **dry-run now reaches Wazuh.** The earlier lesson was that `FRAUD_DRY_RUN` only gates the
  fraud service's own API calls, while Wazuh read the same `fraud.alert` line and blocked for
  real. Every `fraud.alert` now carries `dry_run` and `auto_action` (what the service would
  do). Rule 110207 stays as the alert (no response attached). The response is bound to a new
  child rule 110210 that needs `auto_action=block_card` AND `dry_run=false`; a line without
  the fields never blocks. `wazuh-logtest` on sample lines: live high-confidence -> 110210;
  dry run -> 110207; 201 km/h alert-only (live or dry run) -> 110207.
- **Detection stays at 200 km/h, auto-block does not.** `impossible_travel` still alerts above
  `FRAUD_MAX_KMH` (200) because that is the right physical limit and the alert is useful to an
  analyst. It auto-blocks only when the sessions overlap (the same card has an unstopped
  session in another city, younger than `FRAUD_CONCURRENT_MAX_AGE_S`, 1 h) or the implied
  speed is at least `FRAUD_BLOCK_KMH` (400). Honest cross-city reuse produced speeds from 201
  km/h upward in the simulation, the live clone 1312 km/h. The alert-only and block-worthy
  hits have separate cool-down keys, so a 201 km/h alert cannot mute a later clone.
- **Cumulative `grid_gap` demoted, real-time `power_gap` added.** The 6 h cumulative ratio
  spans 0.71 to 1.38 for honest stations and is diluted by honest history, so it can neither
  avoid false quarantines nor reliably catch a 50 % tamper: it is now a warning with no
  action. The quarantine trigger is `power_gap`, built on the signal behind
  `MeterUnderReporting` (`city:station_power_gap_watts`, feeder minus reported power, exactly 0
  on every honest station in real time). It needs a gap of at least 1000 W and 30 % of the
  feeder power in 80 % of at least 10 charging samples over 15 min. A scale-0.5 tamper shows a
  feeder of twice the reported power and satisfies this; honest stations never do.
- **Quarantine flags the sessions.** After isolating the station the service now flags the
  station's recent sessions in billing for review (nothing is reversed), as the design said.

Verification on normal traffic only (about 10 min after the redeploy, `FRAUD_DRY_RUN=true`):
10 samples one minute apart (15:37 to 15:47 UTC): registry quarantine 0 throughout, no
`billing.card_blocked`, no Wazuh `block-card` action, no `fraud.action` other than
`dry_run`. The service raised 3 `impossible_travel` alerts on honest cards: 257 km/h and
204.5 km/h (both `auto_action=none`, i.e. alert only, which is the case the change is for) and
one at 2086 km/h (`auto_action=block_card`, logged as an intended action with `result=dry_run`,
and rule 110207 only on the Wazuh side because `dry_run=true`). That last one is a limitation of
the simulator, which does not model travel time: a roaming driver can start a session in
another city minutes after the last one, which no speed threshold separates from a clone.
With the service live it would have blocked an honest card, so `FRAUD_DRY_RUN=true` stays on.

### Residual limitations

- `power_gap` trusts the feeder meter and the reported-power metric; a compromised feeder
  meter, or a tamper that also falsifies the metric, defeats it. Its samples live in memory,
  so after a restart a tamper needs the window to refill, and a tamper short enough to finish
  within one window (under about 5 min of charging) is not caught. The power signal is
  simulated; real meters will have measurement noise that needs tuned margins.
- The overlap test relies on stop events: a lost stop older than the 1 h bound looks like a
  gone session, and a lost stop younger than it looks like an overlap. The 400 km/h
  threshold is a margin chosen against this data set, not a measured optimum, and a clone
  used slowly (sequential, under 400 km/h, after the first session stopped) is alerted but
  not blocked.
- Wazuh rules 110203 and 110206 (CSMS-side concurrency in two cities within 120 s) key on the
  CSMS logs, which carry no dry-run flag. They are now alert-only (removed from the
  `block-card` active response); 110210 is the single block path and honours `dry_run`.
- Travel-time modelling is the simplest one that is sound: honest cards are city-local, so
  there is no legitimate roaming in the data set at all. A real network would have some
  (a driver charging in another city after a drive), which needs a per-card trip schedule
  with in-transit gaps sized by distance over a road speed; that is documented as a
  simplification, not implemented.
- The real card holder is still blocked together with the clone; the recovery path is manual.
- `grid_gap` still alerts on honest variance and adds noise to the Wazuh alert stream.

### Travel-time fix in the simulator (2026-11-09, evening)

The false positives above all had one cause: `driver.pick_card` drew a card of any city
with probability 0.2 and used it at once, so one honest card could charge in Porto and in
Lisboa minutes apart (2086 km/h seen live). RFID cards are issued per city operator and
honest drivers charge in their home city, so honest selection is now city-local: a station
only draws cards whose `home_city` is its own city (`sim/src/evsim/driver.py`). `--home-bias`
default is now 1.0 and only controls how often an unblocked card is preferred; it never
widens the pool to another city. The clone attack (`CloneState`, `--clone-card`) bypasses
this selection, so a cloned card is still replayed in a second city and now stands out
against honest traffic, where cross-city use of one card does not occur.

The new sim was installed on all 15 stations and 30 of 30 chargers reconnected (aveiro
plain, porto and lisboa mTLS unchanged). With `FRAUD_DRY_RUN=true` kept on, I watched honest
traffic from 16:58 to 17:11. Two `impossible_travel` alerts appeared: card CD40A17EA0986F
(lisboa home, 391 km/h at about 16:59) and card 392A3725999B16 (aveiro home, 274 km/h at
about 17:05). Both were alert-only (`auto_action=none`, `dry_run=true`) and nothing was
blocked. They are residue of the old simulator: in each pair the newer session was in the
card's home city, while the earlier session (about 16:17, in a foreign city) predates the
city-local deploy and stays in the fraud service's 6 h window (`window_h`). A few such
old-history alerts can therefore still appear until about 22:50 unless the fraud pod is
restarted. The verified result: no block or quarantine action on honest targets, no 110203,
110206 or 110210 alert, registry quarantine at 0, no billing `card_blocked`, and the
steady-state city-local honest traffic produces no cross-city use of one card. The
`impossible_travel` rate fell from about one alert every 6 to 7 minutes before the deploy
to none in the last minutes of the watch. This resolves the honest-card false positives of
the detector: the thresholds are now meaningful because new honest traffic no longer
contains impossible travel.

Residual: enforcing mode (`FRAUD_DRY_RUN=false`) can be turned on after this, pending a
supervised re-demo of the clone attack that confirms the block fires only on the clone. The
real card holder is still blocked with the clone and the recovery path is manual.

## Findings and risks

1. **Dry-run does not mean "no card blocked".** Wazuh rule 110207 matches the fraud
   service's `fraud.alert` log line and `block-card` is its active response; the line is
   written in dry-run too, so honest cards were blocked from 12:44 on, before this work. Confirmed again after the
   final restore with `FRAUD_DRY_RUN=true`: at 15:13:47 Wazuh blocked the PoC card (honest
   roaming, `pt.porto.st-001` to `pt.lisboa.st-001`) and I unblocked it once more. Expect honest
   cards to keep being blocked until this is fixed.
   Proposal: add `dry_run` (true/false) to the `fraud.alert` line and exclude it in 110207
   (`<field name="dry_run" negate="yes">^true$</field>`), or move the block decision wholly
   to the fraud service.
2. **Rate guard starvation.** `block-card` allows 10 distinct cards per hour; honest
   `impossible_travel` false positives used it up (11 entries, 10 distinct, at 13:49), so a
   real cloned card would be refused by Wazuh (`rate_guard`). State saved in
   `clone-card/after-enforcing/wazuh-block-rate-state-before-live.json`, then reset to `[]`
   for the supervised run. Here the fraud service's own block would still have caught it.
3. **Wazuh 110203/110206 do not catch a slow clone** (events minutes apart, window 120 s).
   Only the distance-aware fraud service does.
4. **Script fix**: `clone_card.sh` / `tamper_meter.sh` slept in the foreground so an
   INT/TERM was only handled after the whole `--duration`; now `sleep & wait`, the cleanup
   runs at once. `--card` and `--extra-args` options added.

## Restore (done, verified)

- Every card blocked since the start unblocked (the PoC card, and the honest cards blocked by
  the fraud rules: 4 + 1 early on, 21 at the final restore including the PoC card; 12 cards
  blocked on 11-06 and 11-07 by the earlier, since-tightened rules and 2 seeded blocked ones
  were there before and left as they were).
- Quarantines released (registry `/quarantine/{id}/release`, then CSMS `change-availability`
  `Operative`): `pt.porto.st-002` (twice, false positive) and `pt.lisboa.st-004`.
- `sim.env` restored on `aveiro-st-001`, `lisboa-st-001`, `porto-st-003`, `lisboa-st-004`
  (`SIM_EXTRA_ARGS=` empty) and `evsim` restarted; 30 of 30 chargers connected; registry
  quarantine count 0; Wazuh `card-allowlist` empty again; no PoC process left.
- `FRAUD_DRY_RUN=true` in the deployment and `k8s/fraud.yaml`; `FRAUD_GRID_SETTLE_S=1400`
  in both.

## Contract problems and proposed wording (docs/ not edited)

- 4.4, `quarantine_charger`: add "and `POST /sessions/{id}/flag` for the station's closed
  sessions of the last `FRAUD_WINDOW_H` hours, reason `grid_gap`", or drop that promise from
  the design note. Today the implementation does not flag them.
- 4.4, `FraudAlert` log line: "`fraud.alert` carries `dry_run` (bool); active responses must
  ignore lines with `dry_run=true`."
- 4.4 defaults: `FRAUD_GRID_SETTLE_S` default 1400 (the code default is still 900; the
  deployment overrides it by environment).

## Evidence index

```
evidence/fraud/
  README.md
  alerts/                              fraud-service-alerts-all.json, impossible_travel-cloned-card.json,
                                       meter_backwards-rollback-porto-st-003.json, peer_deviation-lisboa-st-004.json,
                                       grid_gap-tampered-lisboa-st-004.json, grid_gap-FALSE-POSITIVE-porto-st-002.json
  clone-card/before-dry-run/           csms/fraud/wazuh/ar lines, poc-run.log, billing-sessions-unflagged.txt
  clone-card/after-enforcing/          same + billing-card-blocked.json, billing-sessions-flagged.json,
                                       billing-authorize-blocked.json, wazuh-block-rate-state-before-live.json
  tamper-rollback/                     csms, fraud, wazuh, ar lines, poc-run.log
  tamper-scale/                        csms, fraud, wazuh, ar lines, poc-run-1/2 logs, registry-quarantine.json,
                                       prom-station_power_gap_watts-lisboa-st-004.json, prom-MeterUnderReporting-ALERTS-range.json
  false-positive-grid-gap-porto-st-002/ fraud logs (both occurrences), registry state, station-ratios-at-15-10.txt
```
Screenshots were not taken (no GUI in this run); the Prometheus range exports above replace
the feeder-versus-reported plot data.
