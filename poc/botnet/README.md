# Botnet PoC (Phase 8, threat 3: botnet)

Design background: the Phase 8 botnet design note. Interfaces: `docs/interfaces.md` v6
sections 9.4 (compromised mode, C2 protocol), 13.3 (`sim.env`), 15.1 (netwatch),
14.3/7.2/7.3 (metrics), 16 (city ACLs).

Every script here defaults to `--dry-run`: it prints the exact commands and target names
it would use and changes nothing. A live run additionally needs `--run` *and* the
environment variable `EVCHARGE_BASELINE_DONE=1` (set in the environment,
once the baseline recording is captured). All scripts are time-boxed (`--duration`,
default 300 s) and restore every `sim.env` they changed and delete the `c2` container on
exit and on `INT`/`TERM`.

## Stages

### 1. `deploy_c2.sh` -- the C2 container

A throwaway `c2` container (image `evcharge-city`, same base as the stations, so it has
a Python 3.13 venv) on the central bridge -- no `--network` flag, so it lands where the
K3s nodes and the Wazuh manager already are. `sim/tests/stub_c2.py` is pushed into it
unchanged, next to this PoC's thin scheduler wrapper, `c2_schedule.py`, which restarts
the stub with the next `--task` at each phase boundary (the stub itself only answers one
fixed task for its whole lifetime, per 9.4) -- it does not re-implement the beacon
protocol, only sequences the existing stub through it:

```
idle <idle-s> -> flood <flood-s> @ <flood-rate-hz> Hz -> surge <surge-s> -> stop <stop-s>
```

Prints the address bots are given (`http://c2.incus:<port>`) and deletes the container on
exit.

```
deploy_c2.sh [--port 8099] [--idle-s 30] [--flood-s 60] [--flood-rate-hz 20]
             [--surge-s 120] [--stop-s 30] [--run] [--duration N]
```

### 2. `compromise.sh` -- enrol N stations into the fleet

Sets `SIM_EXTRA_ARGS="--compromised --c2-url http://c2.incus:<port> --beacon-interval
<N>"` on `--stations N` stations, spread round-robin across `--cities` (**3 Lisboa
stations, `st-001..003`, by default**), and restarts `evsim` on each (13.3). The original
`sim.env` of every touched station is saved to a state file (`--state-file`, default under
`/tmp`); `--restore` reads it back, writes the original content, restarts `evsim`, and
deletes the file. The compromise is deliberately **not** undone when `compromise.sh`
itself exits -- it must outlive this one invocation while the C2 schedule drives flood and
surge and the fleet is detected and quarantined. Only `--restore` (called by `run.sh` at
the end, or by the operator directly) undoes it; if this script is interrupted mid-loop,
the stations it had already touched *this* invocation are rolled back immediately.

Expected detections and response, printed by the script:

- **per station**: `host.new_outbound` to the C2 address -> netwatch **110303** -> the
  **`quarantine-charger` active response fires automatically** on that station
  (station-level, bound to 110303). This is the automatic response: a station seen
  beaconing to a C2 is quarantined without waiting for an operator.
- **cross-station**: **110304** (freq 3/300, `same_field dstip`, `different_field
  hier_id`) -- the three stations beaconing to the *same* new address, the "locate the
  fleet" signal and the botnet's signature (a fleet alert, no separate response).
- **once the C2 schedule reaches flood/surge**: `ocpp.rate_limited`/`conn.kicked` ->
  **110300/110301**, edge `limit_req` refusals **110306/110307**; a `session.start` burst
  per city -> **110308**, joined with `CityLoadSpike`. The correlator folds these and the
  110303/110304 beacons into **one incident keyed on the city** (`lisboa`).

```
compromise.sh [--stations N] [--cities lisboa] [--port 8099]
              [--beacon-interval 15] [--restore] [--run] [--duration N]
```

### 3. `measure_collateral.sh` -- the collateral-damage measurement

Read-only against the lab (Prometheus queries only, issued through `incus exec` so the
host needs nothing but `incus`). `--phase before` snapshots, per city:

- `sum(evcharge_sim_sessions_total{result="interrupted"})` (city Prometheus, per_charger
  scope, 7.2) -- a session a station controller ends without a `StopTransaction` because
  its connection was torn down from outside (9.3 point 9): exactly what `quarantine-charger`
  does (`remote-stop`, `change-availability Inoperative`, `disconnect`), so this counter's
  delta across the quarantine step *is* the number of real driver sessions the response
  cut short.
- the average charging power at that moment (`sum(evcharge_sim_power_watts) /
  sum(evcharge_sim_charger_status == bool 2)`, both per_charger in the city Prometheus).
- the CSMS's `evcharge_csms_connected_chargers` and `evcharge_csms_sessions_stopped_total`
  totals (central Prometheus), for the "chargers taken off line" and the stopped-session
  side of the same story.

`--phase after` re-snapshots and prints the delta: interrupted-session count caused by
the response, and an estimate of the **kWh not delivered** -- `interrupted sessions x
average charging power x half the mean session length` (`--session-minutes`, default 8,
the sim's own default, 9.2). Half the mean length is the stated, simple assumption: a
session stopped at a uniformly random point in its run loses, on average, half of what it
would otherwise have drawn. This is an estimate, not a meter reading, and is reported as
one; the exact figure is for the live run, not this build.

```
measure_collateral.sh --phase before|after [--cities porto,lisboa,aveiro]
                       [--session-minutes 8] [--state-file PATH] [--run] [--duration N]
```

### `run.sh` -- orchestrates the whole scenario

```
run.sh [--run] [--stations N] [--cities lisboa] [--duration N]
```

1. `deploy_c2.sh` (backgrounded, so its schedule runs alongside the rest).
2. `measure_collateral.sh --phase before` -- the healthy baseline, taken **before** the
   compromise: the automatic quarantine fires on the first beacon, so a snapshot taken
   after the compromise would race it.
3. `compromise.sh --stations 3 --cities lisboa`: each station beacons -> 110303 ->
   `quarantine-charger` fires **automatically** on that station; 110304 names the fleet.
4. wait while the C2 schedule drives idle -> flood -> surge -> stop and the automatic
   quarantine takes the named stations off line (confirm the AR fired from
   `evcharge-ar.json`).
5. `measure_collateral.sh --phase after` -- the collateral of that automatic quarantine;
   then `compromise.sh --restore` and the C2 is stopped (its own trap deletes the
   container).

### `selfcheck.sh`

Runs every script above on its `--dry-run` path, checks each exits 0 and prints a plan,
and checks that `--run` is refused without `EVCHARGE_BASELINE_DONE=1`. Exits non-zero if
anything failed. Creates and changes nothing -- no instance, no `sim.env` edit, no incus
write call.

## Why the automatic quarantine is the most disruptive response in the project

Flood traffic from one address gets `firewall-drop`/`evcharge-edge-drop`, a 600 s,
self-expiring edge rule -- no legitimate session is touched. The botnet's actual harms
(the message flood, the grid surge) come from many *otherwise legitimate* stations acting
in concert, so the response that stops them is to take each beaconing station off line:
`quarantine-charger`, bound to **110303** and fired **automatically per station** when it
is seen contacting the C2, which remote-stops and disables that charger. Every real driver
plugged into a quarantined charger is interrupted mid-session -- that is the whole point of
`measure_collateral.sh`: to put a number (and a kWh figure) on that cost and weigh it
against the alternative of letting the surge trip the feeder (a wider, uncontrolled
outage). It is bound to 110303 (the high-confidence beacon), *not* to the flood/surge
rules, precisely because it is so disruptive: a station only beacons to a C2 if it is
genuinely compromised. Nothing auto-reverts a quarantine; an operator releases the stations
once the firmware is confirmed clean (15.5).

## The ACL concession

Section 16: with the botnet allow list in place, a station may reach only its own city
edge and the Wazuh manager (plus 80/443 while `--allow-updates` is set); it cannot reach
an arbitrary external C2. The lab's `c2` container sits on the central bridge, which a
station is **not** on that allow list for -- it is reachable in this PoC only because the
test explicitly permits it, a stated concession the run makes plain (it is also
why the "before" ACL state matters: with the ACLs applied as normal, a real compromised
station would never reach this C2 at all, and the beacon would show up as a *blocked*
egress attempt instead, which is itself a weaker but earlier signal).

## Before/after steps for the run

1. **Baseline**, nothing from this PoC has run yet; `EVCHARGE_BASELINE_DONE` unset.
2. **ACL before/after**: capture the beacon with the city ACLs *not* applied (the
   concession above, PoC as built). Then, separately (outside this PoC's owned scripts,
   per section 16), apply `infra/08_acls.sh apply` and show the same beacon attempt
   blocked at the network instead of reaching `c2.incus` -- the egress-filtering argument
   in the post-mortem.
3. **Rate-limit before/after**: run the flood phase once with the edge `limit_req` and
   the CSMS token bucket at their normal settings (110300/110306/110307 fire, few
   requests get through), and once with that standing control disabled (a config toggle
   in the PoC's own lab config, not the live default) to quantify what it absorbs -- the
   "standing control, no response firing" layer DESIGN-botnet.md calls out.
4. Run `run.sh --run` (or the individual scripts) for the full scenario; during stage 4
   of `run.sh`, the `quarantine-charger` active response fires automatically on each
   station's 110303 -- confirm it from `evcharge-ar.json` and capture the evidence listed
   in `evidence/botnet/` (beacons, the 110303 quarantine `ar.action` lines, the 110304
   fleet alert, the flood and surge indicators, the single city-keyed correlator incident,
   the collateral-damage table, the detect -> locate -> mitigate timeline).
5. Process any queued revocation (`pki/process_revocations.sh`) and release the
   quarantined stations once the operator is done, per `DESIGN-botnet.md`.

## Deferred to the operator

- Any live `incus launch`/`incus exec`/`systemctl restart evsim` call -- everything here
  ran `--dry-run` only.
- Confirming the automatic `quarantine-charger` active response fired (from `evcharge-ar.json`)
  and releasing the quarantined stations afterwards.
- Toggling the city ACLs and the edge/CSMS rate limiting for the before/after comparisons.
- Capturing the evidence in `evidence/botnet/`.
