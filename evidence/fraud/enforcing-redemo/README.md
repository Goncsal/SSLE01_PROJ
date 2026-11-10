# W7 -- fraud: enforcing re-demo on clean traffic (2026-11-09, 17:40 to 18:15 UTC)

Purpose: after the hardening (alert above 200 km/h, auto-block only on overlap or at least 400 km/h; quarantine
driven by the real-time `power_gap` rule; `block-card` wired to rule 110210 only; city-local honest simulator),
show that the automatic responses fire in enforcing mode (`FRAUD_DRY_RUN=false`) on the attack, and **only** on the
attack. Lab only, supervised, time-boxed. Scripts: `poc/fraud/`. Raw captures sit in the subfolders here; the
earlier runs and the hardening notes are in `../README.md`.

## Summary

| Scenario | Fraud service | Wazuh | Response | Result |
|---|---|---|---|---|
| Clean slate | 0 alerts, 0 `fraud.action` (17:41 to 17:47) | quiet | none | honest traffic is quiet after the pod restart |
| Cloned card, enforcing | `impossible_travel` 629 km/h, `auto_action=block_card`, `dry_run=false`; `block_card ok` | **110210** -> `block-card ok` (110410) | card **Blocked**, 9 sessions flagged | later authorize returns `Blocked`; CSMS `auth.reject Blocked` |
| Meter scale 0.5, enforcing | `power_gap` critical (50 % gap); `quarantine ok`, `change-availability` + `disconnect` x2, 24 sessions flagged | 110405 (quarantine), 110208 | station **quarantined**, chargers Inoperative | Prometheus `MeterUnderReporting` firing 4.9 min earlier; CSMS raised no `meter.anomaly` |
| No collateral | all 4 alerts in the window listed below | one `block-card`, no honest target | -- | **no honest card blocked, no honest station quarantined** |

## Step 1 -- clean slate (honest traffic, 17:40:57 restart, watch 17:41 to 17:47)

`kubectl rollout restart deploy/fraud` (still `FRAUD_DRY_RUN=true`), then five one-minute samples of
`GET /alerts?since=<restart>`, `fraud.action` log lines and the registry quarantine list:

```
17:42:41 alerts_since_restart=0  fraud.action_lines=0  quarantine count 0
17:43:42 .. 17:46:44  same (0 / 0 / 0)
```

Caveat found: the alerts and per-card session history live in the fraud database, not in the pod, so a restart
does not drop them. The pre-fix residue stays in the 6 h window and is re-evaluated when the card's next
session arrives. One such alert appeared later (17:57:31, below); it is alert-only by construction and the
residue is gone from the alert range by about 18:10.

## Step 2 -- enter enforcing

`kubectl -n evcharge set env deploy/fraud FRAUD_DRY_RUN=false` at 17:47:03; rollout complete; the new pod logs
`service.start ... "dry_run":false` (17:47:15). `k8s/fraud.yaml` stays `true`. Billing blocked-card set captured
before (14 cards, earlier history): `billing-blocked-cards-before.txt`.

## Step 3 -- cloned card, enforcing

```
EVCHARGE_BASELINE_DONE=1 poc/fraud/clone_card.sh --run --phase after --card E3EE5BE2ED4120 --duration 720
```

Card `E3EE5BE2ED4120` (home Porto, driver-0001); the script put `--clone-card` on `pt.aveiro.st-001` and
`pt.lisboa.st-001` at about 17:47:55. Expected: `impossible_travel` high confidence -> block -> Wazuh 110210 ->
`block-card`; billing Blocked + sessions flagged; authorize refused. Observed (`clone-card/`):

```
17:48:38.445 csms   auth.accept E3EE5BE2ED4120 pt.lisboa.st-001.cp-2          (the clone, second city)
17:48:38.471 csms   session.start tx 4400
17:48:38.472 fraud  fraud.alert impossible_travel: porto, then 26.3 min later lisboa: 276 km at 629 km/h
                    (alert above 200, block at 400 or overlap)  auto_action=block_card dry_run=false
17:48:38.546 fraud  fraud.action block_card E3EE5BE2ED4120: ok ; flag_session x9 ok (17:48:38.551-.614)
17:48:39.346 wazuh  110209 billing.card_blocked / session_flagged x5
17:48:39.366 wazuh  110210 "Fraud service: high-confidence cloned card ... block"  (level 12)
17:48:39.566 wazuh  ar.action block-card ok: card blocked  (rule_id 110210, idempotent after the fraud service)
17:48:39.586 wazuh  110410 Active response block-card done
17:51:40.765 csms   auth.reject result=Blocked  pt.lisboa.st-001.cp-1   (the clone's next attempt)
```

Billing (`billing-card-blocked.json`): `status blocked`, reason with the speed, `blocked_at 17:48:38.537`.
`billing-sessions.json`: 61 sessions of the card, 9 `flagged` (3963 to 4400). `billing-authorize-blocked.json`:
`POST /authorize` -> `{"status":"Blocked"}`. Alert: `alerts/impossible_travel-cloned-card.json` (`distance_km`
275.5, `speed_kmh` 629.1, `high_confidence true`, `concurrent false`, transactions 4359 and 4400).

Timeline: clone sessions start (+0) -> detect +1 ms -> billing block +66 ms -> Wazuh 110210 and `block-card` +0.9 s.
About 43 s from the `sim.env` change to the first cloned authorization. Only the Lisboa leg authorized before the
block; the Aveiro leg was not needed.

Note (same card, later): after the script was stopped I unblocked the card at 17:54; at 18:12:41 the real
holder's own Porto session (`pt.porto.st-005`) came 24 min after the injected Lisboa clone session, so the
detector blocked the same card again (694 km/h, rule 110210, 18:12:43). Same target, not a new card; the block
is correct on the data (the card was in Lisboa 24 min earlier). It is the documented collateral on the real holder.
The final unblock was done only after returning to dry-run (see Restore).

## Step 4 -- meter tamper (scale 0.5), enforcing

```
EVCHARGE_BASELINE_DONE=1 poc/fraud/tamper_meter.sh --run --station pt.lisboa.st-003 --mode scale --factor 0.5 \
    --extra-args "--session-minutes 3 --arrival-rate 30" --duration 1500      # 17:54:40, stopped 18:12:15
```

Expected: no CSMS `meter.anomaly` (readings stay consistent); `MeterUnderReporting`; `power_gap` critical ->
`quarantine-charger` + billing flag. Observed (`tamper-scale/`):

```
17:54:40  tamper on (sim.env, evsim restarted)
18:01:07  first tampered session (tx 4420, cp-2); feeder 46.7 kW, gap 23.3 kW (50 %) from 18:01:53
18:02:00  MeterUnderReporting pending (Prometheus)
18:07:00  MeterUnderReporting firing                                   (+12.3 min after the tamper began)
18:11:58.857 fraud  fraud.alert power_gap: feeder exceeds reported by 24337 W (50 % of 48674 W) in 21 of 26
                    charging samples over 15 min  auto_action=quarantine dry_run=false
18:11:58.887 fraud  fraud.action quarantine pt.lisboa.st-003: ok ; change-availability + disconnect cp-1 and cp-2: ok
18:11:58.9   csms   command change-availability Inoperative (connector 1 Unavailable) ; disconnect
18:11:59.0-.25 fraud flag_session x24 ok
18:11:59.110 wazuh  110405 registry.quarantine pt.lisboa.st-003 (source fraud) ; 110208 power_gap alert
```

CSMS logged **no** `meter.anomaly` for the station (counted in both CSMS pods). Registry (`registry-quarantine.json`):
exactly one node quarantined, `pt.lisboa.st-003`, `state_source fraud`, reason the `power_gap` sentence. The CSMS
refused the station's next handshake (`ident.quarantined`, 403). Prometheus data for the feeder-versus-reported
plot: `prom-station_grid_power_watts-lisboa-st-003.json` (feeder), `prom-station_power_gap_watts-lisboa-st-003.json`
(gap; reported = feeder - gap), `prom-MeterUnderReporting-ALERTS-range.json`.

When and why `power_gap` fired at +17.3 min, not earlier: it needs at least 10 charging samples (30 s poll) and 80 % of
them gapped in the 15 min window. Between 17:57 and 17:59 the feeder series held a stale 10.3 kW with gap 0 (five
samples) before the first tampered session at 18:01; those five counted as charging but not gapped and kept the
fraction at 75 % until they fell out of the window, so it crossed 80 % at 21 of 26 (81 %). The sessions are also
sparse on this station: part of the Lisboa card pool is blocked in billing from earlier rounds (8 of 20) and the
simulator draws them, so about 40 % of arrivals there are refused at the charger. With a steady tamper the rule
would fire about 5 to 6 min after the first tampered charging sample. `MeterUnderReporting` corroborated 4.9 min
earlier. I did not change any threshold.

Billing "flag for review": 24 sessions of `pt.lisboa.st-003` got `flag_reason` "fraud rule power_gap" (list in
`billing-sessions-flagged-power_gap.txt`). Only 2 of them (4420 and 4431, started after the tamper began) belong to the tamper;
the rest are the station's honest sessions from the 6 h window (12:28 onward). Nothing is reversed; it is a review
flag, but it is over-inclusive (see the proposal below).

## Step 5 -- no collateral on honest cards or stations

Audited at 18:13, at the end of the enforcing window (17:47 to 18:13):

- **Blocked cards**: billing `cards.status='blocked'` before = 14, after = 15; the diff is one card,
  `E3EE5BE2ED4120` (`billing-blocked-cards-before.txt`, `-after-enforcing.txt`). `blocked` table rows in the window:
  two, both for this card (17:48:39 and 18:12:42) -- `billing-blocked-rows-enforcing-window.txt`.
- **Quarantined stations**: registry holds only `pt.lisboa.st-003` (the tampered one). No other node was
  quarantined at any sample.
- **Fraud alerts** in the window (`alerts/fraud-service-alerts-enforcing-window.json`, 4 in 26 min): the clone
  (17:48:38), a single alert-only honest one (17:57:31, card 4AB5EE3CD4AA84, 226 km/h, `auto_action=none`: pre-fix
  Lisboa session tx 4310 from 16:46 against a new session in its own city; below 400 km/h and not an overlap, so no
  action and no block), `power_gap` on st-003 (18:11:58), and the clone card's second block (18:12:41).
- **Actions** (`fraud.action` non-flag lines): `block_card E3EE5BE2ED4120` x2, and the `quarantine` /
  `change-availability` / `disconnect` set for `pt.lisboa.st-003` only. Wazuh `ar.action`: `block-card` for
  `E3EE5BE2ED4120` only.
- **Honest traffic**: apart from the one alert-only residue, no honest card produced an `impossible_travel`
  alert in 26 min of enforcing; stations other than st-003 raised no `power_gap` (it exists only for the tampered
  station) and the Prometheus gap is 0 W on honest stations.

Result: the automatic responses touched exactly the two deliberate targets. The hardening held in enforcing mode.

## Restore (verified)

- `FRAUD_DRY_RUN=true` again at 18:13:36 (deployment env and the pod's `service.start dry_run:true`); `k8s/fraud.yaml`
  was never changed.
- Card `E3EE5BE2ED4120` unblocked (billing `/cards/.../unblock`, status `active`), done after the return to dry-run so
  the stale Lisboa clone session cannot block the holder again.
- Station `pt.lisboa.st-003`: registry release, then CSMS `change-availability Operative` on cp-1 and cp-2
  (Accepted once reconnected); quarantine count 0.
- `sim.env` restored by the scripts on `pt.aveiro.st-001`, `pt.lisboa.st-001`, `pt.lisboa.st-003`
  (`SIM_EXTRA_ARGS=` empty, `evsim` active); 30 of 30 chargers connected; no PoC process left.

## Findings and proposals (docs/ not edited)

1. **Card and alert history is in the database**: a pod restart does not clear the window. The "clean slate" for this
   demo therefore relied on the pre-fix residue being old enough; one alert-only residue still appeared. To drop
   residue deliberately, prune `card_events` and `fraud_alerts` older than the simulator fix. Proposed wording for 4.4:
   "the fraud database survives restarts; the 6 h window is recomputed from it".
2. **`power_gap` flags the whole 6 h of the station's sessions**, honest ones included. Proposed 4.4 wording for the
   quarantine action: "flag the station's sessions that started since the first gapped sample of the window"
   (keeps the review queue to the sessions that were actually tampered).
3. **Stale feeder samples dilute the fraction.** Gap-less samples with feeder at least `FRAUD_POWER_GAP_W` count as
   charging; a stale last value on an idle station adds a few. Either count only samples with a live session at the
   station, or state the fraction against the samples of the last window in 4.4.
4. **Unblocking a cloned card invites an immediate re-block** while the clone's session is inside the window (here
   the holder's next home session, 24 min later, tripped the 400 km/h bound). Recovery procedure to state: unblock
   after the 6 h window of the clone session, or after the dry-run switch; reissuing the card is the real fix.
5. Banner and README wording in `poc/fraud/` said `grid_gap` for the scale tamper; changed to `power_gap` (the rule
   that now quarantines). `selfcheck.sh` 7 ok.

## Evidence index

```
evidence/fraud/enforcing-redemo/
  README.md
  billing-blocked-cards-before.txt, billing-blocked-cards-after-enforcing.txt, billing-blocked-rows-enforcing-window.txt
  alerts/                  impossible_travel-cloned-card.json, power_gap-tampered-lisboa-st-003.json,
                           fraud-service-alerts-enforcing-window.json
  clone-card/              fraud-logs, csms, wazuh-alerts, ar (jsonl), poc-run.log, billing-card-blocked.json,
                           billing-authorize-blocked.json, billing-sessions.json
  tamper-scale/            fraud-logs, csms, wazuh-alerts, ar (jsonl), csms-quarantine-and-release.jsonl, poc-run.log,
                           registry-quarantine.json, billing-sessions-flagged-power_gap.txt,
                           prom-station_grid_power_watts-lisboa-st-003.json, prom-station_power_gap_watts-lisboa-st-003.json,
                           prom-MeterUnderReporting-ALERTS-range.json
```
