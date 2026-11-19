# Demo runbook

Live demo of the EV charging network (SSLE Project 1), about 11 minutes plus questions.
Presenter: Gonçalo Almeida. The report (`report/report.pdf`) is the story; this file is the
sequence of commands and what to say. Everything runs on the presenter's laptop, from the
repository root.

Contents: 0 conventions, 1 pre-flight, 2 the script, 3 reset, 4 failure modes,
5 validation runs (done), 6 rehearsal plan.

## 0. Conventions

Every terminal starts with the same preamble (secrets are read from `secrets/platform.env`;
never print them, never put them on a slide):

```bash
cd ~/Desktop/ssle/project
set -a; . secrets/platform.env; set +a
N=$(. infra/lib.sh; ipof k3s-master)          # K3s master address (10.255.255.80 in the current lab)
kc() { incus exec k3s-master -- kubectl "$@"; }
```

Endpoints (all reachable from the host browser or `curl` at `$N`; `10.255.255.188` is the second node):

| What | Address | Auth (variable in `secrets/platform.env`) |
|---|---|---|
| Grafana | `http://$N:30300` (user `admin`) | `GRAFANA_ADMIN_PASSWORD` |
| Central Prometheus | `http://$N:30090` | none |
| Alertmanager | `http://$N:30093` | none |
| Registry (`/locate`, `/quarantine`) | `http://$N:30801` | reads: none; writes `REGISTRY_ADMIN_TOKEN` |
| CSMS API (`/connections`, `/commands`) | `http://$N:30800` | `CSMS_API_TOKEN` |
| Billing (`/cards`) | `http://$N:30802` | `BILLING_API_TOKEN` |
| Correlator (`/incidents`) | `http://$N:30804` | `CORRELATOR_API_TOKEN` |
| Wazuh dashboard | `https://10.255.255.170` (self-signed certificate, accept the warning) | dashboard `admin` account |

Helper functions used below (paste once per terminal):

```bash
H()  { curl -s -m10 -H "Authorization: Bearer $1" "${@:2}"; }
conn()  { H "$CSMS_API_TOKEN" "http://$N:30800/connections?prefix=${1:-pt}" | jq -c '{count, tls: ([.items[].tls]|unique)}'; }
quar()  { curl -s "http://$N:30801/quarantine?prefix=pt" | jq -c '{count, ids: [.items[].hier_id]}'; }
inc()   { H "$CORRELATOR_API_TOKEN" "http://$N:30804/incidents?since=${1}&limit=30" \
          | jq -c '.items[] | {kind, status, root: .root_cause.key, first: .t_first_signal, located: .t_located, mitigated: .t_mitigated}'; }
walerts() { incus exec wazuh-container -- tail -n 0 -F /var/ossec/logs/alerts/alerts.json \
          | jq --unbuffered -c 'select((.rule.id|startswith("110")) and .rule.level>=5) | {t:.timestamp[11:19], id:.rule.id, lvl:.rule.level, agent:.agent.name, d:.rule.description[0:80]}'; }
```

PoC scripts refuse a live run unless `EVCHARGE_BASELINE_DONE=1` is exported; export it only in the
attacker terminals (T2/T3 below). When reading pod logs by label, add `--tail=-1`:
`kc -n evcharge logs -l app.kubernetes.io/name=csms --tail=-1 --since=10m` (without it `kubectl` shows only 10 lines per pod).

## 1. Pre-flight checklist (start 30 minutes before)

### 1.1 Lab is up

| Check | Command | Expected |
|---|---|---|
| Host RAM | `free -g` | at least 8 GiB available (`eval/scale.sh health` prints `available_mb`) |
| 23 instances running | `incus list -f csv -c ns \| grep -vc RUNNING` | `0` (3 edges, 15 stations, `docker`, `k3s-master`, `k3s-node1`, `lab-ca`, `wazuh-container`) |
| Lab health script (read-only) | `infra/00_status.sh` | every line `ok`, both nodes `Ready` |
| Pods | `kc get pods -A \| grep -v -E 'Running\|Completed'` | no output; 18 pods Running (`evcharge`: billing, correlator, csms x2, fraud, postgres, registry; `monitoring`: alertmanager, grafana, prometheus; `wazuh`: 2 agents) |
| Platform summary | `eval/scale.sh health` | `connected_all=30`, `connected_aveiro=10`, `quarantined=0`, no `NOT RUNNING` lines |
| Service health | `for p in 30801 30800 30802 30804; do curl -s http://$N:$p/health; echo; done` | each `{"status":"ok", ...}` |
| Grafana, Alertmanager | `curl -s http://$N:30300/api/health; curl -s http://$N:30093/-/ready` | `"database": "ok"`; `OK` |
| Wazuh services | `for s in wazuh-manager wazuh-indexer wazuh-dashboard; do incus exec wazuh-container -- systemctl is-active $s; done` | `active` x3 |
| Wazuh dashboard answers | `curl -sk -o /dev/null -w '%{http_code}\n' https://10.255.255.170/` | `302` |
| 30/30 chargers, per city | `conn pt.porto; conn pt.lisboa; conn pt.aveiro` | `count` 10 each; Aveiro `tls:[false]`, Porto and Lisboa `tls:[true]` |
| 0 quarantined | `quar` | `{"count":0,...}` |
| No firing alerts | `curl -s http://$N:30090/api/v1/query --data-urlencode 'query=ALERTS{alertstate="firing"}' \| jq '.data.result \| length'` | `0` (a few minutes after any reboot, wait until it is) |
| Leftovers from earlier runs | `incus list -f csv -c n \| grep -E 'rogue\|c2'` ; `ls /tmp/evcharge-botnet-compromise.state` | both empty / no such file |
| Every station `sim.env` clean | see 3.2 step 2 (the loop) | no line printed |
| Certificate revocation list fresh | `incus exec porto-edge -- openssl crl -in /etc/evcharge/pki/crl.pem -noout -nextupdate` | a date at least 3 days in the future. The CRL lives 7 days; an expired CRL makes both TLS cities refuse every charger (see 4.6) |
| Clocks | `date -u; incus exec k3s-master -- date -u; incus exec k3s-node1 -- date -u; incus exec k3s-master -- chronyc tracking \| head -4` | the three times agree within 2 s; if not, `chronyc makestep` in both VMs (a VM that does not move needs `systemctl restart chrony` first), then restart csms (see 4.5) |
| DNS from a city network (needed by the rogue launch, 2.c1) | `incus exec porto-edge -- getent hosts deb.debian.org` | an address. If the laptop has no uplink, this fails; see 4.7 |

Allow at least 15 minutes after a laptop boot or resume before the final check: K3s pods restart,
the CSMS autoscaler spikes (1.5) and chargers reconnect.

### 1.2 State toggles for the demo

Set these once the checks above pass. Section 3 reverses every one of them.

| Toggle | Normal state (today) | Demo state | Why | Set | Reverse |
|---|---|---|---|---|---|
| Fraud `FRAUD_DRY_RUN` | `true` | `false` | the cloned-card block must be visible (card Blocked, authorise refused) | `kc -n evcharge set env deploy/fraud FRAUD_DRY_RUN=false` | `kc -n evcharge set env deploy/fraud FRAUD_DRY_RUN=true` |
| City egress ACLs | applied on all three cities, default-drop | removed | the botnet beacon must reach the C2 on the central bridge (stated concession, report 5.3), and the hijack rogue container must reach the Porto edge | `EVCHARGE_BASELINE_DONE=1 infra/08_acls.sh remove --run` | `EVCHARGE_BASELINE_DONE=1 infra/08_acls.sh apply --run` then `infra/08_acls.sh check` |
| `CSMS_ENFORCE_STATION_IP` | `true` | `true` (unchanged) | the stolen certificate is refused with 403; do not change it | none | none |
| Aveiro transport | plain `ws://` on port 80 | plain (unchanged) | the legacy city; the fraud clone uses `aveiro-st-001` | none | none |

Verify the toggles:

```bash
kc -n evcharge get deploy fraud -o jsonpath='{range .spec.template.spec.containers[0].env[?(@.name=="FRAUD_DRY_RUN")]}{.value}{"\n"}{end}'
infra/08_acls.sh status | head -4          # after remove: no ACL attached to the city networks
kc -n evcharge get cm csms-edge -o jsonpath='{.data.CSMS_ENFORCE_STATION_IP}{"\n"}'     # true
```

Do not run `infra/05_deploy.sh` during the session: it re-applies `k8s/fraud.yaml` and puts
`FRAUD_DRY_RUN` back to `true`.

Whether to run the fraud segment enforcing: yes. In dry-run the same attack is detected and alerted
but nothing is blocked, which is the "before" half already documented in
`evidence/fraud/clone-card/before-dry-run/`. The enforcing run is the one that shows the response.
Honest-traffic false positives were fixed (report 5.2), and the enforcing window is only about 10 minutes (it is a toggle: set it just before the demo, 1.2).

### 1.3 Browser tabs (open in this order, Grafana kiosk off, time range last 30 minutes, refresh 10 s)

1. `http://$N:30300/d/evcharge-map`: Geomap, all 30 stations green, three clusters, firing-alerts table empty.
2. `http://$N:30300/d/evcharge-platform`: CSMS replicas, latency, pod CPU.
3. `http://$N:30300/d/evcharge-city?var-city=lisboa`: charger table, city load against the baseline limit.
4. `http://$N:30300/d/evcharge-bandwidth`: set the absolute range 2026-11-05 23:40 to 2026-11-06 00:45 UTC (browser time zone matters): federated about 46 KiB/min against direct about 6 MiB/min, step at 00:02. Check the number on screen before the demo.
5. Wazuh dashboard `https://10.255.255.170`: Threat Hunting (Explore, Threat Hunting, Events), filter `rule.groups:evcharge`, time range last 15 minutes, auto refresh 5 s, columns `rule.id`, `rule.level`, `agent.name`, `rule.description`. Log in and open this tab before the demo; the dashboard is the slowest component (4.1).
6. `http://$N:30093` Alertmanager (for the inhibition story and for the side-effect alerts after the botnet quarantine).
7. A text viewer with `evidence/` ready for fallbacks: `evidence/phase4/screenshots/*.png`, `evidence/{hijack,fraud,botnet}/live-20261117/`, `evidence/correlator/incidents/*.json`.

### 1.4 Terminals (preamble from section 0 in each; large font)

| Terminal | Role | What runs in it |
|---|---|---|
| T1 | stage | `curl`s on registry, correlator, billing; `conn`, `quar`, `inc` |
| T2 | attacker A | the fraud clone (started 3:00), then the botnet (`poc/botnet/run.sh --run`, started about 6:15). `export EVCHARGE_BASELINE_DONE=1` |
| T3 | attacker B | the hijack (`poc/hijack/run.sh --run`, started 2:15). `export EVCHARGE_BASELINE_DONE=1` |
| T4 | alert feed | `walerts` (Wazuh alert tail, rule ids 110xxx at level 5 and above), running for the whole demo; a second pane with `watch -n5 'conn; quar'` |

Record the demo start for the correlator filter: in T1 run `T0=$(date -u +%Y-%m-%dT%H:%M:%SZ); echo $T0`
immediately before segment (a). Incidents from earlier days are then filtered out with `inc $T0`.

### 1.5 CSMS autoscaler (HPA) state

Read-only checks: `kc describe hpa csms -n evcharge`, `kc top pods -n evcharge`, `kc get hpa -n evcharge`.
Expected at rest: `cpu: ~10%/70%`, `Min replicas 2`, `Max replicas 4`, `2 current / 2 desired`.

If it shows 4 replicas at about 10% CPU, it will go back to 2 by itself within about 5 to 6 minutes:
the autoscaler has no custom `behavior`, so the default scale-down stabilisation window of 300 s applies
(`k8s/csms.yaml` sets only `minReplicas: 2`, `maxReplicas: 4`, CPU target 70% of a 100 m request).
Cause seen after a reboot: the metrics API is briefly unavailable, pods restart with a CPU burst, the
autoscaler scales to 4, then drops to 2 after the window (events `SuccessfulRescale ... New size: 4`
then `New size: 2 ... All metrics below target`). Nothing to fix: just wait and re-check.

To show scaling live is not recommended (about 5 minutes up, 5 more down, and it needs 140 connections);
show the measured table (report 6.2, `evidence/eval/README.md`) and the Grafana `evcharge-platform` panel
"CSMS replicas up" for the past runs. If scaling must be shown, `eval/scale.sh up 22 1` (22 extra
chargers on each of the 5 Aveiro stations, heartbeat 1 s) reached 4 replicas at 70% in the evaluation;
undo with `eval/scale.sh down`, then allow 5 minutes for the scale-down. It changes the connected count
(30 becomes 140), so do it only outside the demo and never before the demo.

## 2. The demo script (target 11 minutes, hard stop 12)

The three attack commands run longer than their talking slots (hijack about 2 minutes, fraud about 3,
botnet about 5). So each one is started by a single command at a fixed time and left running while you
talk; the segments below only say when to look at the result. Order: hijack, then fraud, then botnet
last. The botnet's automatic quarantine takes Lisboa `st-001` to `st-003` offline and the fraud clone
uses `lisboa-st-001`; the hijack uses Porto only.

Timeline (T is minutes:seconds from the start; "start" means the one command that is then left running):

| Segment | T start | Length | Tab / terminal | Command started |
|---|---|---|---|---|
| (a) Architecture and naming | 0:00 | 1:15 | Geomap, T1 | |
| (b) Monitoring | 1:15 | 1:45 | Geomap, bandwidth, T1 | hijack in T3 at 2:15 |
| (c1) Hijack, stolen certificate | 3:00 | 1:15 | T4, T1 | fraud in T2 at 3:00 |
| (c2) Fraud, cloned card | 4:15 | 2:00 | T4, T1 | botnet in T2 at about 6:00, once the fraud script has exited |
| (c3) Botnet | 6:15 | 2:30 | T4, Wazuh dashboard, city dashboard | |
| (d) Correlation | 8:45 | 1:30 | T1 | |
| (e) Scalability and limitations | 10:15 | 0:45 | report table | |
| Questions | 11:00 | | | the botnet script finishes its own restore meanwhile |

Run lengths behind this plan (rehearsal figures, section 6 records the real ones): hijack about 2:05 in
total, first refusal about 30 s after the start and rule 110132 about 31 s after that; fraud about 2:30 to
3:30 (it waits for the Aveiro session to start and stop, then for the Lisboa start); botnet about 5 minutes
(first 110303 about 1 minute after the start, an estimate that the rehearsal must confirm). The two
attacks overlap in time (hijack and fraud from 3:00, fraud and botnet never), which was not rehearsed
live as a pair: they touch different stations (Porto against Aveiro and Lisboa `st-001`), but check it in
rehearsal 1. If the pair misbehaves, start the fraud command when the hijack run ends (about 4:20) and
accept a longer fraud segment.

Rule of thumb during the demo: never wait silently for an alert. Every wait below has something to say.

### (a) Architecture and naming (0:00 to 1:15)

Say:
- Regional EV charging operator: chargers in Porto, Lisboa, Aveiro speak OCPP 1.6J over wss with mTLS to a CSMS on K3s; registry, billing, fraud and a correlator around it; Prometheus per city plus a central one; Wazuh as SIEM.
- One identity string everywhere: `pt.<city>.<station>.<charger>` is the certificate name, the OCPP path, the registry key, the Prometheus label and the map label. That is why any alert can be located with one registry lookup.
- Aveiro is the deliberately legacy city (plain `ws://`); Porto and Lisboa are mTLS.

Do: in T1 (`T0=...` first):

```bash
curl -s http://$N:30801/locate/pt.lisboa.st-003 | jq '.matches[0] | {hier_id, path, operator, edge, host, ip, network, dns, lat, lon}'
```

Audience sees: the station, its path `["pt.lisboa","pt.lisboa.st-003"]`, operator `lisboa`, host `lisboa-st-003`, DNS `st-003.lisboa.pt.lab`, coordinates; point at the same station on the Geomap. Optional second line: `curl -s http://$N:30801/locate/pt.porto | jq '.count'` (the whole Porto subtree).

Fallback: report Figure A/B, or `evidence/phase4/README.md` "Locate" (0.013 s).

### (b) Monitoring (1:15 to 3:00)

Say (Geomap tab, then bandwidth tab):
- Two tiers: each city has its own Prometheus on the edge, scraping every station every 5 s; the central Prometheus federates only the `city:*` recording rules every 30 s. Per-charger series never cross the inter-site link.
- Measured, 30 minutes each window, same load: 47 KB/min federated against 6.4 MB/min with direct scraping, a 99.3% reduction (99.5% gzip); samples per minute 324 against 55,427; central CPU 5 times higher with direct scraping.
- Thresholds are computed, not guessed: mean plus 3 sigma per city from a 12 hour baseline (for example Porto grid 288,519 W).

Do (live, 10 s, at about 1:45): on the Geomap, zoom to Lisboa, then in T1

```bash
incus exec lisboa-st-003 -- systemctl stop evsim      # marker turns grey within about 6 s (worst case 15 s)
# ... say: StationDown comes after 83 s, ChargerOffline after 130 s (evidence/phase4)
incus exec lisboa-st-003 -- systemctl start evsim     # marker green again in about 15 s
```

Do not leave it stopped for more than 20 s: the botnet segment compromises Lisboa `st-001` to `st-003` later.

At 2:15, in T3, start the hijack (details in c1; ACLs must already be removed, 1.2). Say it in one line:
"Now I start the first attack, a stolen certificate in Porto; I come back to it in a minute."

```bash
# T3
export EVCHARGE_BASELINE_DONE=1
T1S=$(date -u +%Y-%m-%dT%H:%M:%SZ)       # window start for verify.sh
poc/hijack/run.sh --run                  # default time-box 120 s, about 2:05 in total
```

Edge failure and inhibition: not run live (it takes 101 s to fire and removes a city). Show
`evidence/phase4/screenshots/map-grey-station-red-ring.png` and say: stopping the Aveiro edge raised one
`EdgeDown` after 101 s and suppressed the five dependent `ChargerOffline` alerts
(`evidence/phase4/alerts/inhibition-aveiro-edge-down.json`, state `suppressed`, `inhibitedBy` the EdgeDown);
30/30 chargers were back in about 30 s to 2 min. Do not stop `aveiro-edge` during a rehearsal that is
also running the fraud clone (it uses `aveiro-st-001`).

Fallback if the grey marker does not appear: `evidence/phase4/screenshots/map-tooltip-grey-station.png`.

### (c1) Hijack, stolen certificate (3:00 to 4:15)

The command was started at 2:15. At 3:00 start the fraud command in T2 (see c2) before you begin talking.

Say:
- mTLS proves possession of a key, not location. A copy of the certificate of `pt.porto.st-001.cp-1` in a throwaway container on the Porto network passes the certificate checks: it is validly signed and not revoked, so the edge lets it through to the CSMS.
- The CSMS compares the source address with the registry record for that charger (check 9) and refuses with a 403, `ident.ip_mismatch`. Wazuh raises 110104 (level 7) for each refusal. The genuine station stays connected and keeps charging: nothing was stopped or quarantined.
- The rogue retries every 15 s. Three refusals from one address in 60 s raise 110132 (level 12, counted across both CSMS pods) and the `evcharge-edge-drop` response adds an iptables DROP for the rogue address on `porto-edge`. It removes itself after 600 s.
- Standing mitigation: `CSMS_ENFORCE_STATION_IP=true` (the lab default). Without it the rogue would be accepted and only logged.

Do (T4 shows the alerts as they arrive; about 3:00 to 3:30 the 110104 lines, 110132 about 30 s after the first):

```bash
incus exec porto-edge -- iptables -S | grep DROP        # the rogue's address, added by the response
```

Optional, after the run ends (about 4:20), using the script's own read-only check:
`poc/hijack/verify.sh $T1S $(date -u +%Y-%m-%dT%H:%M:%SZ) --charger pt.porto.st-001.cp-1 --city porto` (needs `REGISTRY_ADMIN_TOKEN`, already loaded). If the CSMS logs are read directly with `kubectl logs -l ...`, add `--tail=-1`: with a label selector `kubectl` shows only 10 lines per pod otherwise.

Audience sees: repeated 110104 (rogue refused, 403), then one 110132 at level 12, then the DROP rule on `porto-edge`; no `conn.open` for the rogue, and the genuine `pt.porto.st-001.cp-1` still connected (`conn pt.porto` shows 10).

Fallback: `evidence/hijack/live-20261117/` (`run.log`, `alerts.jsonl`, `verify.txt`). Older recorded runs are in `evidence/hijack/scenario2-stolen-cert/`.
If the rogue never becomes ready (no uplink), see 4.7.

### (c2) Fraud, cloned card (4:15 to 6:15)

The command was started at 3:00 in T2 (ACLs removed and `FRAUD_DRY_RUN=false`, 1.2):

```bash
# T2
export EVCHARGE_BASELINE_DONE=1
poc/fraud/clone_card.sh --run --phase after --card E3EE5BE2ED4120
```

Say:
- Two independent detectors on the same logs: the fraud service (impossible travel) and Wazuh rules 110200 to 110210.
- Card `E3EE5BE2ED4120`, home city Porto, is used in Aveiro and then in Lisboa. The sessions run one after the other, not at once: the CSMS refuses a second concurrent transaction for one card (`ConcurrentTx`), so an overlapping clone would never reach the fraud service. The script waits for the Aveiro session to start and stop, then starts Lisboa.
- Rehearsal figures: second session 1.1 minutes after the first, 218 km, so 11,492 km/h. The fraud service blocks the card at 400 km/h or more (alerts above 200 km/h). That threshold came from 24 false alerts on honest cards in the first run.
- The meter tamper is not shown live: the real-time `power_gap` rule needs 10 charging samples, and `MeterUnderReporting` fired 23 minutes in; the cumulative `grid_gap` took 76 minutes. See `evidence/fraud/enforcing-redemo/README.md`.

Do: until the block appears (expected about 5:30 to 6:30), T2 prints the stations and waits; talk through the
points above. When the Lisboa session starts, T4 shows `110207`, `110209` (billing audit: card blocked and
sessions flagged), `110210` (level 12) and the `block-card` response `110410` about 1.6 s later. The script
prints the gap between the two starts and the card state itself. For the audience, in T1:

```bash
curl -s -H "Authorization: Bearer $BILLING_API_TOKEN" http://$N:30802/cards/E3EE5BE2ED4120 | jq '{status, blocked_reason, blocked_at}'
```

Audience sees: `status: "blocked"`, reason "fraud rule impossible_travel ... km at ... km/h".

Say the collateral: the real holder is blocked too; unblocking is operator-only (responders cannot do it).

As soon as T2 has its prompt back (the script restored both `sim.env` files), start the botnet there (c3).
Do not start it earlier: the compromise would race the fraud restore on `lisboa-st-001`.

Fallback if the block does not appear within 120 s after the Lisboa session should have started: check `FRAUD_DRY_RUN` is `false` (1.2), then show
`evidence/fraud/live-20261117/` (`clone3-summary.txt`, `clone3-alerts.jsonl`, `run3-clone.log`) and
`evidence/fraud/clone-card/after-enforcing/`. If the card never blocks, press Ctrl-C once (cleanup restores `sim.env`) and start the botnet.

### (c3) Botnet (6:15 to 8:45)

Start (T2, about 6:00 to 6:15; ACLs removed, 1.2):

```bash
# T2, after the fraud script has exited
poc/botnet/run.sh --run
```

One command does everything: deploys the C2 container, takes the collateral baseline, compromises three Lisboa
stations (`st-001` to `st-003`), waits for the C2 schedule (idle 30 s, flood 60 s at 20 Hz, surge 120 s, stop 30 s;
the default wait is 300 s), measures the collateral, restores `sim.env` and deletes the C2. Beacons every 15 s.
The baseline is taken before the compromise because the automatic quarantine fires on the first beacon.

Say (T4 on screen):
- Three stations are compromised: they beacon to a C2 and would then flood the CSMS and start full-power sessions at the same moment.
- Per-station detection: netwatch sees a new outbound connection, rule 110303 (level 8), three of them. Each one fires the `quarantine-charger` response automatically, within 1 to 2 seconds: Wazuh 110410 ("Active response quarantine-charger done") and the registry audit 110405.
- The fleet rule 110304 (level 12, "3 stations contacting <C2> within 5 minutes") names the botnet, about 5 to 7 s after the first 110303. It uses `global_frequency`, because Wazuh counts frequency per agent and each station is its own agent. It fired live on 2026-11-17. The quarantine is also bound to 110304, so all three stations end up quarantined.
- Because the quarantine acts on the first beacon, the flood and surge phases are pre-empted: no flood alerts are expected.

Do:
1. T4 shows `110303` x3 with `110410` and `110405` right after each, then `110304`. Wazuh dashboard tab: same filter.
2. In T1: `quar` (expected: 3 stations, `pt.lisboa.st-001` to `st-003`) and `conn pt.lisboa` (connected chargers drop; 30 connected overall becomes 24).
3. Expected noise, say it: quarantined chargers keep retrying and raise repeated `110101` (identity refused: quarantined). The correlator opens one botnet incident for `pt.lisboa` plus side-effect incidents (a hijack-kind one from the 110101 lines and `ChargerOffline`).
4. Say the cost honestly: the quarantine cuts live sessions (a few CSMS forced session stops; `measure_collateral.sh` prints the count and an estimate of kWh not delivered when the run ends, after the segment). Every driver mid-session on those stations pays for a per-charger compromise. A graduated response (rate-limit first, quarantine on a second signal, drain sessions) is the stated future work. There is no manual bulk quarantine step.
5. Leave the run going through (d) and (e); its restore and C2 cleanup happen on their own (about 11:00). If the run is still going when questions start, say so.

Expected timing: first 110303 about 1 minute after the start (estimate), 110304 5 to 7 s later.

Fallback if nothing fires: `evidence/botnet/live-20261117/` (`alerts.jsonl`, `incidents.json`, `run.log`) and, for the earlier run, `evidence/botnet/alerts/wazuh-alerts.jsonl`; say the live run was not reproduced and show the recorded one. If the C2 never starts (4.3), cover the botnet from the evidence folder and go to (d).

### (d) Correlation (8:45 to 10:15)

Say:
- Three sources, one incident per root cause: Alertmanager (webhook), Wazuh (integration, level 7 and above plus response ids) and the fraud service. Signals join on `hier_id`, source address or card within 120 s plus 3 s skew, and the root cause is the lowest common element in the registry path.
- Live figures from 2026-11-17, with the Wazuh signals forwarded live to the correlator: fraud located to the card in 0.18 s, hijack in 1.8 s (located to the city `pt.porto` because a city-level Prometheus signal joins), botnet in 2.1 s (`pt.lisboa`).
- Replay figures, recorded earlier: hijack 3.55 s, fraud 0.10 s, botnet about 1 s, `EdgeDown` 10 s (Alertmanager `group_wait`).

Do (T1):

```bash
inc "$T0"                                        # one line per incident since the demo started
H "$CORRELATOR_API_TOKEN" "http://$N:30804/incidents?since=$T0&limit=30" | jq '.items[0] | {kind, status, severity, summary, root_cause, t_first_signal, t_located, t_mitigated}'
curl -s http://$N:30804/metrics | grep -E 'evcharge_correlator_(incidents_total|incidents_open|time_to_locate_seconds_count)'
```

Audience sees: one incident for the hijack, one for the card (root cause key type `card`), one for the botnet fleet (root cause `pt.lisboa`), plus the side-effect incidents from the quarantine (hijack-kind from 110101, `ChargerOffline`); say they are consequences, not new attacks. `t_located` minus `t_first_signal` is the time to locate; say it in seconds.

Fallback: `jq` over the recorded incidents, and the live ones in `evidence/botnet/live-20261117/incidents.json`:

```bash
for f in evidence/correlator/incidents/*.json; do jq -c '{kind, root: .root_cause.key, first: .t_first_signal, located: .t_located, mitigated: .t_mitigated}' $f; done
```

### (e) Scalability and limitations (10:15 to 11:00)

Say (report table 6.2 on screen, or the `evcharge-platform` tab):
- One laptop, synthetic OCPP load on Aveiro: 30 to 140 connected chargers, latency flat (p95 8 to 9 ms, p99 24 ms at the top), about 110 msg/s sustained and 170 peak; every stop was a configuration limit (edge `limit_conn` 24 per source, HPA maximum 4, the per-charger limiter), not RAM. Prometheus series stayed at about 4,500 because monitoring is per station and per city.
- 110 msg/s corresponds to about 1,500 real chargers at 0.07 msg/s each: an extrapolation, not a measurement.
- Limits to state: correlator figures are from one live run per attack plus replays; the botnet quarantine cuts drivers off; cloned-card and meter thresholds are tuned to simulated data; ACLs do not stop same-bridge traffic (no `br_netfilter`); the heartbeat generator is lighter than real traffic.

## 3. Reset between rehearsals and after the demo

There is no single reset script. The PoC scripts undo their own `sim.env` edits and temporary containers on exit,
but the botnet quarantine release and every state change in 1.2 are manual. Expected time: about 5 minutes
(the slowest parts are evsim restarts and the chargers' reconnect backoff; the registry and card steps take seconds).
Do the steps in this order.

### 3.1 Quick check of what is left

```bash
incus list -f csv -c n | grep -E 'rogue|c2'
ls /tmp/evcharge-botnet-compromise.state
quar; conn pt
```

### 3.2 Ordered commands

1. Stop attacker containers (the scripts delete their own on exit; this covers an interrupted run):
   `for i in $(incus list -f csv -c n | grep -E '^(porto|lisboa|aveiro)-rogue$|^c2$'); do incus delete -f $i; done`
2. Restore every station `sim.env` and restart where changed:
   - botnet: `poc/botnet/run.sh` restores its own `sim.env` files when it finishes; if it was interrupted or you are unsure, run `EVCHARGE_BASELINE_DONE=1 poc/botnet/compromise.sh --restore --run` (it detects the compromise flag on the instances themselves and cleans every affected station, so it works after a reboot and is safe to run when nothing is compromised).
   - verify all 15 stations (must print nothing):
     ```bash
     for c in porto lisboa aveiro; do for n in 1 2 3 4 5; do
       incus exec $c-st-00$n -- grep -H 'SIM_EXTRA_ARGS=.' /etc/evcharge/sim.env | sed "s/^/$c-st-00$n /"; done; done
     ```
   - for a station that still shows arguments (or whose state file was lost), write the clean file and restart (`<city>` is `porto`, `lisboa` or `aveiro`; the URL is `wss://edge.<city>.pt.lab/ocpp`, and `ws://edge.aveiro.pt.lab/ocpp` for Aveiro):
     `incus exec <city>-st-00N -- sh -c 'printf "SIM_CSMS_URL=%s\nSIM_EXTRA_ARGS=\n" "<url>" > /etc/evcharge/sim.env' && incus exec <city>-st-00N -- systemctl restart evsim`
3. Release the quarantined stations and set their chargers Operative. After the botnet, the registry quarantine is at station level: ids `pt.lisboa.st-001` to `pt.lisboa.st-003` (the CSMS keeps a released charger Unavailable until told otherwise). Run it after the botnet script has finished its restore, so the stations are clean before they reconnect:
   ```bash
   quar                                              # list: expect the 3 Lisboa stations
   for id in $(curl -s "http://$N:30801/quarantine?prefix=pt" | jq -r '.items[].hier_id'); do
     H "$REGISTRY_ADMIN_TOKEN" -X POST -H 'Content-Type: application/json' -d '{"reason":"demo reset","source":"operator"}' "http://$N:30801/quarantine/$id/release" | jq -c '{hier_id,state}'
   done
   sleep 20      # the chargers reconnect within their backoff
   for s in pt.lisboa.st-001 pt.lisboa.st-002 pt.lisboa.st-003; do for cp in cp-1 cp-2; do
     H "$CSMS_API_TOKEN" -X POST -H 'Content-Type: application/json' -d '{"reason":"demo reset","source":"operator","connector_id":0,"type":"Operative"}' "http://$N:30800/commands/$s.$cp/change-availability" | jq -c '{hier_id,status}'
   done; done
   ```
   (`status` `NotConnected` means the charger has not reconnected yet; repeat after 20 s. Also run the same `Operative` loop for any other station `quar` listed. The quarantine of a whole station by the fraud service is released the same way, on the station id.)
4. Return fraud to dry-run first, then unblock the card (in this order, otherwise the stale clone session can block the holder again; see `evidence/fraud/enforcing-redemo/README.md`, Restore):
   ```bash
   kc -n evcharge set env deploy/fraud FRAUD_DRY_RUN=true
   curl -s -X POST -H "Authorization: Bearer $BILLING_API_TOKEN" -H 'Content-Type: application/json' \
        -d '{"reason":"demo reset","source":"operator"}' http://$N:30802/cards/E3EE5BE2ED4120/unblock | jq -c '{card,status}'
   ```
5. Flush stale edge firewall drops. The hijack response (`evcharge-edge-drop` on 110132) adds `iptables DROP` rules on a city edge; the response removes them after 600 s, but an interrupted run leaves them, and a leftover rule for a station address blocks that whole station from the edge (seen in practice: all of Porto and Lisboa cut off). Clear them on every edge and confirm none remain:
   ```bash
   for e in porto-edge lisboa-edge aveiro-edge; do for ch in INPUT FORWARD; do
     while incus exec $e -- iptables -S $ch | grep -q -- '-j DROP'; do
       incus exec $e -- sh -c "iptables \$(iptables -S $ch | grep -- '-j DROP' | head -1 | sed 's/^-A /-D /')"
     done; done; done
   for e in porto-edge lisboa-edge aveiro-edge; do printf '%s ' $e; incus exec $e -- sh -c 'iptables -S | grep -c -- "-j DROP"'; done   # each must print 0
   ```
6. Certificate revocation list fresh. It is now valid 30 days, but check it has not lapsed: `incus exec porto-edge -- openssl crl -in /etc/evcharge/pki/crl.pem -noout -nextupdate`. If expired or close, `pki/crl.sh && gateway/push_crl.sh`, then restart evsim on the Porto and Lisboa stations so they reconnect without waiting out the backoff.
7. Re-apply the city ACLs and check them: `EVCHARGE_BASELINE_DONE=1 infra/08_acls.sh apply --run` then `infra/08_acls.sh check` (it builds the station list from the current addresses; run it after every container re-creation).
8. Aveiro stays plain ws and nothing in the demo changes it: just verify `conn pt.aveiro` shows `tls:[false]`, 10 connected.
9. The hijack never stops the real station; check it anyway: `incus exec porto-st-001 -- systemctl is-active evsim`.
10. Check the end state: `eval/scale.sh health` must show `connected_all=30`, `quarantined=0`; `conn pt`, `quar`; no firing alert after a few minutes; `incus list -f csv -c n | grep -E 'rogue|c2'` empty; `ls /tmp/evcharge-botnet-compromise.state` finds nothing.
11. Correlator noise: open incidents close themselves after 600 s of quiet (`CORRELATOR_CLOSE_S`); closed history stays. Filter with `inc $T0` rather than deleting anything. Rule 110300 (level 6) does not reach the correlator, so flood alerts never appear there. The botnet leaves side-effect incidents (hijack-kind from the quarantined chargers' repeated 110101, `ChargerOffline`); they close by themselves.
12. Wazuh guards that persist across rehearsals: `block-card` allows 10 distinct cards per hour and `quarantine-charger` 5 distinct chargers per 10 minutes. The demo uses one card and quarantines three Lisboa stations, which was within the limits on 2026-11-17 (not verified for two botnet runs in 10 minutes: leave 10 minutes between botnet rehearsals). Do not rehearse more than twice in an hour with extra cards.

### 3.3 What to leave for the demo itself

After a full reset, wait 5 minutes and run section 1.1 again before the next run.

## 4. Failure modes during the demo

| # | Symptom | Do |
|---|---|---|
| 4.1 | Wazuh dashboard slow or blank | Do not wait on it. T4 (`walerts`) reads `alerts.json` directly and is the primary view; the dashboard is secondary. If the indexer is behind, say "the dashboard indexes a few seconds later" and keep going. If `wazuh-indexer` is down, `incus exec wazuh-container -- systemctl start wazuh-indexer wazuh-manager wazuh-dashboard` before the demo only. |
| 4.2 | An alert does not fire | Say what it should have shown, show the rule id and the recorded alert from `evidence/<threat>/`, move on within 15 s. Segment fallbacks are listed in section 2. Check `incus exec wazuh-container -- tail -n 20 /var/ossec/logs/ossec.log` afterwards, not during. |
| 4.3 | C2 or compromise fails (ACLs still applied, no `c2` instance, DNS on the central bridge; `run.sh` prints an error in T2) | Check `incus list c2`, `infra/08_acls.sh status`. If not fixed in 30 s, drop the live botnet, present it from `evidence/botnet/live-20261117/`, and run reset 3.2 after the demo. |
| 4.4 | Laptop under memory or CPU pressure (slow Grafana, evsim restarts lag) | Close extra browser tabs first. Keep the Grafana refresh at 10 s, not 5 s. Do not run the scalability tooling. `free -g`; below 4 GiB available, skip the botnet live. Two attacks run at the same time from 3:00: if the lab struggles, start the fraud command only when the hijack run has ended. |
| 4.5 | Clock skew on the K3s VMs (seen three times: 2 h 34 min, 16 h 50 min and 27 h behind the host) | Read-only check: `date -u; incus exec k3s-master -- date -u; incus exec k3s-node1 -- date -u; incus exec k3s-master -- timedatectl; incus exec k3s-master -- chronyc tracking`. Healthy: `System clock synchronized: yes`, chrony `System time` within milliseconds. Today chrony is active on the VMs. After a laptop suspend or resume the VMs can lag, and chrony only slews. On 18 Nov, after an overnight suspend, both VMs were about 16 h 50 min behind and 16 s apart. The repair is a step, not a restart: `incus exec k3s-master -- chronyc makestep` and the same on `k3s-node1`. Check that both VMs moved (`date -u` in each). If one did not, chrony has marked every server `^x` (falseticker) because its old and new samples disagree by the size of the jump (seen on 19 Nov on `k3s-node1`, 27 h behind): run `incus exec k3s-node1 -- systemctl restart chrony`, wait until `chronyc sources` shows a `^*` server (a few seconds), then `chronyc makestep` again. Do it before the demo, never during. After the step (on both VMs; if one VM stepped later, restart csms after it) the CSMS connections table empties (rows judged stale), so also run `kc -n evcharge rollout restart deploy/csms` and wait until `/connections` counts 30 (about 1 min). Symptom if skewed: correlator `t_located` negative or hours, CSMS log timestamps off from the edge, `/connections` showing only one replica's 15 chargers (the other replica's heartbeat looks stale, `CSMS_POD_STALE_S=15`) and commands to chargers on the other replica returning `NotConnected`. |
| 4.6 | Porto and Lisboa chargers refused (HTTP 400 in the edge log, count of connected chargers stuck at 10) | The CRL expired (7 days). `incus exec porto-edge -- openssl crl -in /etc/evcharge/pki/crl.pem -noout -nextupdate`. Repair before the demo: `pki/crl.sh && gateway/push_crl.sh`, then wait for the chargers to reconnect (about 1 to 2 minutes). Not a mid-demo fix. |
| 4.7 | Rogue container never becomes ready (no uplink, `wait_ready` waits for DNS) | The check is `getent hosts deb.debian.org` inside the new container. With no network it fails after about 2 minutes and the script aborts. Plug in the uplink or a phone hotspot before the demo; otherwise present the hijack from `evidence/hijack/live-20261117/`. Test once with the laptop on the demo network. |
| 4.8 | Aveiro in a mid-state (some chargers connected, `tls` mixed, or an edge stopped) | `conn pt.aveiro`, `incus list aveiro-edge`, `incus exec aveiro-edge -- systemctl status nginx`. A stopped edge: `incus start aveiro-edge`, all 10 reconnect within about 30 s to 2 min. Aveiro must show `tls:[false]`; a mixed value means a chart or edge was changed by hand, so redeploy the Aveiro edge as plain ws before the demo. |
| 4.9 | A script hangs | Ctrl-C once: the PoC scripts run their cleanup on `INT` and `TERM`. Then 3.1 to see what is left. |
| 4.10 | Fraud card never blocks | Check `FRAUD_DRY_RUN=false` (1.2) and that the second city's session actually started (T2 output; the CSMS refuses a concurrent session for one card). Do not start the botnet until T2 has its prompt back. Show `evidence/fraud/live-20261117/`. |
| 4.11 | Hijack: no 403, or no DROP rule | Rogue not ready (4.7), or `CSMS_ENFORCE_STATION_IP` is `false` (1.2), or ACLs still applied (the rogue is dropped at the network). 110132 needs 3 refusals within 60 s, about 30 s after the first. Show `evidence/hijack/live-20261117/`. |

## 5. Validation runs (done 2026-11-17)

Both validation runs the report listed as open were done on 2026-11-17, with the lab in the 1.2 demo state.
Nothing here needs repeating before the demo.

- **Live botnet with `global_frequency`.** Rule 110304 fired live in a two-node run, after the three 110303
  alerts, and the automatic `quarantine-charger` response ran per station. The surge and flood phases are
  pre-empted by the quarantine, so 110308 and `CityLoadSpike` are not expected in the demo. Evidence:
  `evidence/botnet/live-20261117/` (`alerts.jsonl`, `incidents.json`, `run.log`, `window.txt`).
- **Live correlator with Wazuh forwarding.** The Wazuh integration delivered signals to the running correlator
  for all three attacks. Locate times: fraud 0.18 s (card), hijack 1.8 s (city `pt.porto`), botnet 2.1 s
  (`pt.lisboa`). Evidence: `evidence/hijack/live-20261117/`, `evidence/fraud/live-20261117/`,
  `evidence/botnet/live-20261117/`.

Not repeated live and still replay-only: the `EdgeDown` incident and the 10 s Alertmanager `group_wait` figure.

## 6. Rehearsal plan

Rehearse twice from a full reset (section 3, then 1.1 to 1.4), with a timer, speaking the talking points aloud.
The third pass is the real demo. Rehearse once with the laptop on the demo network or hotspot (4.7) and once
with Wi-Fi off, to see what depends on an uplink. In rehearsal 1, confirm the hijack and fraud commands
can run at the same time (section 2, timeline note) and measure how long after the start each alert
appears; the times below are estimates from the 2026-11-17 runs.

Why the plan overlaps commands: the three runs alone add up to about 10 minutes (hijack 2, fraud 3,
botnet 5), more than the 11-minute demo has room for. So the demo starts each run with one command, talks
over it, and looks at the result at the segment time. What to narrate during a wait: the say-lines of the
segment (they are written to fill the wait); for the fraud wait, the two-detector story and the
`ConcurrentTx` point; for the botnet, the cost of quarantine and the 110101 noise.

Fill in the actual times (mm:ss), measured with a stopwatch from the start of the demo (T):

| Event or segment | Target | Run 1 | Run 2 | Notes (what fired late, what to cut) |
|---|---|---|---|---|
| (a) Architecture and naming | ends 1:15 | | | |
| (b) Monitoring, grey marker | ends 3:00 | | | |
| Hijack command started | 2:15 | | | |
| (c1) Hijack: first 110104 at | about 2:50 | | | |
| (c1) Hijack: 110132 and DROP rule at | about 3:25 | | | |
| (c1) Hijack command finished | about 4:20 | | | |
| Fraud command started | 3:00 | | | |
| (c2) Fraud: block visible at | about 5:45 to 6:15 | | | |
| (c2) Fraud command finished (prompt back) | about 6:00 to 6:15 | | | |
| Botnet command started | 6:15 | | | |
| (c3) Botnet: first 110303 at | about 7:15 | | | |
| (c3) Botnet: 110304 at | about 7:20 | | | |
| (c3) Botnet: `quar` shows 3 stations at | about 7:25 | | | |
| (d) Correlation | 8:45 to 10:15 | | | |
| (e) Scalability and limits | 10:15 to 11:00 | | | |
| Botnet command finished (restore done) | about 11:15 | | | |
| Reset duration (3.2) | 5:00 | | | |

After each run: write down what to cut or move, check 3.2 step 8, and check the three dashboards for stuck alerts.
If the total exceeds 12:00, cut in this order: the optional lines in (a) and (c1), the Wazuh dashboard
(use T4 only), the grey-marker demo in (b), then the live hijack (start it from the evidence
`evidence/hijack/live-20261117/`). If the fraud block comes after 6:30, start the botnet anyway at 6:15
from a second terminal only if the fraud script has already restored `lisboa-st-001`; otherwise
cover the botnet from `evidence/botnet/live-20261117/`. Keep fraud, botnet and correlation as the core.
