# Demo runbook

Live demo of the EV charging network (SSLE Project 1), about 11 minutes plus questions.
Presenter: Gonçalo Almeida. The report (`report/report.pdf`) is the story; this file is the
sequence of commands and what to say. Everything runs on the presenter's laptop, from the
repository root.

Contents: 0 conventions, 1 pre-flight, 2 the script, 3 reset, 4 failure modes,
5 validation runs, 6 rehearsal plan.

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
attacker terminal (T2/T3 below).

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
| Leftovers from earlier runs | `incus list -f csv -c n \| grep -E 'rogue\|c2\|mitm'` ; `ls /tmp/evcharge-botnet-compromise.state` | both empty / no such file |
| Every station `sim.env` clean | see 3.2 step 2 (the loop) | no line printed |
| Certificate revocation list fresh | `incus exec porto-edge -- openssl crl -in /etc/evcharge/pki/crl.pem -noout -nextupdate` | a date at least 3 days in the future. The CRL lives 7 days; an expired CRL makes both TLS cities refuse every charger (see 4.6) |
| Clocks | `date -u; incus exec k3s-master -- date -u; incus exec k3s-node1 -- date -u; incus exec k3s-master -- chronyc tracking \| head -4` | the three times agree within 2 s |
| DNS from a city network (needed by the rogue launch, 2.4) | `incus exec porto-edge -- getent hosts deb.debian.org` | an address. If the laptop has no uplink, this fails; see 4.7 |

Allow at least 15 minutes after a laptop boot or resume before the final check: K3s pods restart,
the CSMS autoscaler spikes (1.5) and chargers reconnect.

### 1.2 State toggles for the demo

Set these once the checks above pass. Section 3 reverses every one of them.

| Toggle | Normal state (today) | Demo state | Why | Set | Reverse |
|---|---|---|---|---|---|
| Fraud `FRAUD_DRY_RUN` | `true` | `false` | the cloned-card block must be visible (card Blocked, authorise refused) | `kc -n evcharge set env deploy/fraud FRAUD_DRY_RUN=false` | `kc -n evcharge set env deploy/fraud FRAUD_DRY_RUN=true` |
| City egress ACLs | applied on all three cities, default-drop | removed | the botnet beacon must reach the C2 on the central bridge (stated concession, report 5.3), and the rogue container must reach the Porto edge | `EVCHARGE_BASELINE_DONE=1 infra/08_acls.sh remove --run` | `EVCHARGE_BASELINE_DONE=1 infra/08_acls.sh apply --run` then `infra/08_acls.sh check` |
| `CSMS_ENFORCE_STATION_IP` | `true` | `true` (unchanged) | the stolen certificate is refused with 403; do not change it | none | none |
| Aveiro transport | plain `ws://` on port 80 | plain (unchanged) | the legacy city of the hijacking story | none | none |

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
Honest-traffic false positives were fixed (report 5.2), and the enforcing window is only about 10 minutes.

### 1.3 Browser tabs (open in this order, Grafana kiosk off, time range last 30 minutes, refresh 10 s)

1. `http://$N:30300/d/evcharge-map`: Geomap, all 30 stations green, three clusters, firing-alerts table empty.
2. `http://$N:30300/d/evcharge-platform`: CSMS replicas, latency, pod CPU.
3. `http://$N:30300/d/evcharge-city?var-city=lisboa`: charger table, city load against the baseline limit.
4. `http://$N:30300/d/evcharge-bandwidth`: set the absolute range 2026-11-05 23:40 to 2026-11-06 00:45 UTC (browser time zone matters): federated about 46 KiB/min against direct about 6 MiB/min, step at 00:02. Check the number on screen before the demo.
5. Wazuh dashboard `https://10.255.255.170`: Threat Hunting (Explore, Threat Hunting, Events), filter `rule.groups:evcharge`, time range last 15 minutes, auto refresh 5 s, columns `rule.id`, `rule.level`, `agent.name`, `rule.description`. Log in and open this tab before the demo; the dashboard is the slowest component (4.1).
6. `http://$N:30093` Alertmanager (for the inhibition story and for alerts during the botnet).
7. A text viewer with `evidence/` ready for fallbacks: `evidence/phase4/screenshots/*.png`, `evidence/correlator/incidents/*.json`.

### 1.4 Terminals (preamble from section 0 in each; large font)

| Terminal | Role | What runs in it |
|---|---|---|
| T1 | stage | `curl`s on registry, correlator, billing; `conn`, `quar`, `inc` |
| T2 | attacker A | botnet C2 (`deploy_c2.sh`), then the fraud clone. `export EVCHARGE_BASELINE_DONE=1` |
| T3 | attacker B | botnet compromise, then the hijack rogue. `export EVCHARGE_BASELINE_DONE=1` |
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
(30 becomes 140), so do it only outside the demo and never before the fraud segment.

## 2. The demo script (target 11 minutes, hard stop 12)

Timeline (T is minutes:seconds from the start; parallel actions are started by one command and left running):

| Segment | T start | Length | Tab / terminal |
|---|---|---|---|
| (a) Architecture and naming | 0:00 | 1:15 | Geomap, T1 |
| (b) Monitoring, with the botnet starting in the background | 1:15 | 2:15 | Geomap, bandwidth, T2/T3 |
| (c1) Botnet | 3:30 | 2:00 | T4, Wazuh dashboard, city dashboard |
| (c2) Fraud, cloned card | 5:30 | 1:45 | T2, T4 |
| (c3) Hijack, stolen certificate | 7:15 | 1:30 | T3, T4 |
| (d) Correlation | 8:45 | 1:30 | T1 |
| (e) Scalability and limitations | 10:15 | 0:45 | report table |
| Questions | 11:00 | | |

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

### (b) Monitoring (1:15 to 3:30)

First, at 1:15, start the botnet in the background so its 4-minute schedule runs while you talk
(details in c1; ACLs must already be removed, 1.2):

```bash
# T2 (export EVCHARGE_BASELINE_DONE=1 first); runs in the foreground for the whole schedule (240 s) and prints "c2 address: http://c2.incus:8099"
poc/botnet/deploy_c2.sh --run --duration 250
```

About 25 s later, in T3:

```bash
poc/botnet/compromise.sh --run --stations 6 --cities porto,lisboa,aveiro --duration 60
```

Say (Geomap tab, then bandwidth tab):
- Two tiers: each city has its own Prometheus on the edge, scraping every station every 5 s; the central Prometheus federates only the `city:*` recording rules every 30 s. Per-charger series never cross the inter-site link.
- Measured, 30 minutes each window, same load: 47 KB/min federated against 6.4 MB/min with direct scraping, a 99.3% reduction (99.5% gzip); samples per minute 324 against 55,427; central CPU 5 times higher with direct scraping.
- Thresholds are computed, not guessed: mean plus 3 sigma per city from a 12 hour baseline (for example Porto grid 288,519 W).

Do (live, 10 s): on the Geomap, zoom to Lisboa, then in T1

```bash
incus exec lisboa-st-003 -- systemctl stop evsim      # marker turns grey within about 6 s (worst case 15 s)
# ... say: StationDown comes after 83 s, ChargerOffline after 130 s (evidence/phase4)
incus exec lisboa-st-003 -- systemctl start evsim     # marker green again in about 15 s
```

Do not leave it stopped for more than 20 s: Lisboa stations are also used by the botnet and clone segments.

Edge failure and inhibition: not run live (it takes 101 s to fire and removes a city). Show
`evidence/phase4/screenshots/map-grey-station-red-ring.png` and say: stopping the Aveiro edge raised one
`EdgeDown` after 101 s and suppressed the five dependent `ChargerOffline` alerts
(`evidence/phase4/alerts/inhibition-aveiro-edge-down.json`, state `suppressed`, `inhibitedBy` the EdgeDown);
30/30 chargers were back in about 30 s to 2 min. If there is slack, the live version is
`incus stop aveiro-edge` and `incus start aveiro-edge` about 3 minutes later, but it conflicts with the
Aveiro botnet stations and the clone segment, so only do it in a separate rehearsal.

Fallback if the grey marker does not appear: `evidence/phase4/screenshots/map-tooltip-grey-station.png`.

### (c1) Botnet (3:30 to 5:30)

Timeline of the C2 schedule (clock starts when `deploy_c2.sh` prints the address, about 1:40):
idle 30 s, flood 60 s at 20 Hz, surge 120 s, stop 30 s. Beacons every 15 s from each of the six stations
(`st-001` and `st-002` of each city).

Say (T4 on screen):
- Six stations are compromised: they beacon to a C2 and then flood the CSMS and start full-power sessions at the same moment.
- Per-station detection: netwatch sees a new outbound connection, rule 110303, six of them. The fleet-level rule 110304 ("three different stations contacting the same address") is the one that names the botnet.
- Be upfront: in the first live run 110304 and `CityLoadSpike`/110308 did not fire, because Wazuh counts frequency per agent and each station is its own agent. The fix, `global_frequency`, is proven in `wazuh-logtest`; today's run is the live test. (After validation run 1, section 5.1, state the outcome that was actually observed.)

Do:
1. T4 shows `110303` x6, `110305`, then `110300`/`110301` (flood), and `110304` if the fix holds. Wazuh dashboard tab: Threat Hunting, same filter.
2. Geomap and city dashboard (`evcharge-city`): city grid power climbing during the surge. Reference peaks of the recorded run: Lisboa 207 kW, Aveiro 166 kW, Porto 156 kW, 5 to 8 times baseline. Alertmanager tab: `MsgRateAnomaly`, `ChargerMsgFlood`.
3. Response, at about 4:30 (about 2 minutes into the C2 schedule): bulk quarantine of the six stations. There is no automatic Wazuh response for botnet rules, so this is the operator step (helper in 3.2 step 3 does the same calls as the `quarantine-charger` response):

```bash
for s in pt.porto.st-001 pt.porto.st-002 pt.lisboa.st-001 pt.lisboa.st-002 pt.aveiro.st-001 pt.aveiro.st-002; do
  for cp in cp-1 cp-2; do id=$s.$cp
    H "$REGISTRY_ADMIN_TOKEN" -X POST -H 'Content-Type: application/json' -d '{"reason":"botnet C2 beacon","source":"operator"}' "http://$N:30801/quarantine/$id" >/dev/null
    H "$CSMS_API_TOKEN" -X POST -H 'Content-Type: application/json' -d '{"reason":"botnet C2 beacon","source":"operator","connector_id":0,"type":"Inoperative"}' "http://$N:30800/commands/$id/change-availability" >/dev/null
    H "$CSMS_API_TOKEN" -X POST -H 'Content-Type: application/json' -d '{"reason":"botnet C2 beacon","source":"operator"}' "http://$N:30800/commands/$id/disconnect" >/dev/null
  done
done
quar          # 12 chargers
```

4. Say the cost honestly: in the recorded run the bulk quarantine forced 21 CSMS session stops (counter 85 to 106) and took Porto's charging power to 0 W; every driver mid-session on those stations pays for a per-charger compromise. A graduated response (rate-limit first, quarantine on a second signal, drain sessions) is the stated future work.
5. Leave the fleet quarantined until the C2 schedule ends (about 5:30), then restore in the order given in 3.2 (compromise `--restore`, release, Operative). Do the restore while starting the fraud segment; it takes under a minute.

Expected timing: 110303 about 25 s after the compromise, flood alerts about 1 to 2 minutes, `MsgRateAnomaly` needs `for: 2m`.

Fallback if nothing fires on Wazuh: `evidence/botnet/alerts/wazuh-alerts.jsonl` (110303 x6, 110305 x6, 110300 x6, 110301 x2, 110405 x6) and `evidence/botnet/prom/city_grid_power_watts_sum-range.json`; say the live run was not reproduced and show the recorded one. If the C2 never starts (4.3), skip to (c2) and cover the botnet from the evidence folder.

### (c2) Fraud, cloned card (5:30 to 7:15)

Say:
- Two independent detectors on the same logs: the fraud service (impossible travel) and Wazuh rules 110200 to 110210.
- Card `E3EE5BE2ED4120`, home city Porto, is presented in Aveiro and Lisboa within minutes. In the recorded enforcing run the second-city session was detected 23 ms later and the card blocked at +58 ms; Wazuh 110210 found it already blocked at +133 ms (idempotent), five sessions flagged within 0.8 s.
- Auto-block only on overlapping sessions or at least 400 km/h; alerts fire above 200 km/h. This hardening came from 24 false alerts on honest cards in the first run.
- The meter tamper is not shown live: the real-time `power_gap` rule needs 10 charging samples, and `MeterUnderReporting` fired 23 minutes in; the cumulative `grid_gap` took 76 minutes. See `evidence/fraud/enforcing-redemo/README.md` (quarantine of `pt.lisboa.st-003`, CSMS raised nothing).

Do (T2; the botnet restore must have finished, because this uses `aveiro-st-001` and `lisboa-st-001`):

```bash
export EVCHARGE_BASELINE_DONE=1
poc/fraud/clone_card.sh --run --phase after --card E3EE5BE2ED4120 --duration 100
```

The script prints the two target stations and waits; `sim.env` of both is restored on exit. In the evidence
the first cloned authorisation came about 43 s after the change. Meanwhile T4 shows `110207`, `110209`,
`110210`, `110410`. In T1:

```bash
curl -s -H "Authorization: Bearer $BILLING_API_TOKEN" http://$N:30802/cards/E3EE5BE2ED4120 | jq '{status, blocked_reason, blocked_at}'
```

Audience sees: `status: "blocked"`, reason "fraud rule impossible_travel ... km at ... km/h". Optional: `curl -s -X POST -H "Authorization: Bearer $BILLING_API_TOKEN" -H 'Content-Type: application/json' http://$N:30802/authorize ...` is not scripted; instead show the `auth.reject` result in `evidence/fraud/clone-card/after-enforcing/csms.jsonl`.

Say the collateral: the real holder is blocked too; unblocking is operator-only (responders cannot do it).

Fallback if the block does not appear within 90 s: check `FRAUD_DRY_RUN` is `false` (1.2), then show
`evidence/fraud/clone-card/after-enforcing/` (`billing-card-blocked.json`, `wazuh-alerts.jsonl`, `poc-run.log`).

### (c3) Hijack, stolen certificate (7:15 to 8:45)

Say:
- mTLS proves possession of a key, not location. A copy of the certificate of `pt.porto.st-004.cp-1` on another host passes the certificate checks; the CSMS compares the source address with the registry (check 9).
- Standing mitigation, applied after the first run: `CSMS_ENFORCE_STATION_IP=true` turns the logged warning into a 403 at the handshake. Wazuh still raises 110104 (`ident.ip_mismatch`).
- The other half is in the evidence: with the duplicate-id guard, 110104 then 110131 (level 12) triggers the quarantine response, at the cost of locking out the genuine station too (two 403 lines for the real station's own address in `evidence/hijack/scenario1-rogue-dupe/`).
- Aveiro (plain ws) can be rewritten in transit: 46 rewritten MeterValues produced 48 `meter.anomaly` (110116); mTLS and the ACLs each defeat it.

Do (T3; the real station is stopped by the script for the duration and restarted on exit):

```bash
export EVCHARGE_BASELINE_DONE=1
poc/hijack/stolen_cert.sh --run --phase after --city porto --station pt.porto.st-004 --duration 70
```

The script launches a throwaway `porto-rogue` container, copies the certificate straight from `lab-ca`,
and connects; evsim retries once a minute, so there are two refusals. Expected, in T4: `110104`
(level 7). In the edge access log the status is `403`:

```bash
incus exec porto-edge -- grep -F 'pt.porto.st-004.cp-1' /var/log/evcharge/edge-pt.porto.edge.json | tail -n 2 | jq -c '{ts, hier_id, srcip, http_status, cert_verify}'
```

Optional verification afterwards, using the script's own read-only check:
`poc/hijack/verify.sh <start_iso> <end_iso> --charger pt.porto.st-004.cp-1 --city porto` (needs `REGISTRY_ADMIN_TOKEN`, already loaded).

Audience sees: the rogue's two attempts refused with 403 and 110104 in the feed; no `conn.open` for the rogue.

Fallback: `evidence/hijack/scenario2-stolen-cert/after/timeline.md` (the same run, two 403s) and
`evidence/hijack/scenario1-rogue-dupe/timeline.md` for 110104, 110131 and the quarantine chain.
Not live: the rogue-duplicate quarantine path (it needs `CSMS_ENFORCE_STATION_IP=false`, a CSMS restart, and locks out a real station).

### (d) Correlation (8:45 to 10:15)

Say:
- Three sources, one incident per root cause: Alertmanager (webhook), Wazuh (integration, level 7 and above plus response ids) and the fraud service. Signals join on `hier_id`, source address or card within 120 s plus 3 s skew, and the root cause is the lowest common element in the registry path.
- Recorded results: hijack located to the charger `pt.porto.st-001.cp-1` in 3.55 s, fraud to the card in 0.10 s, botnet to the city in about 1 s, `EdgeDown` in 10 s (Alertmanager `group_wait`). Mitigation followed in 1.65 s and 0.07 s.
- Those numbers come from replayed recordings. What you are looking at now is live. (State which parts of it are live after validation run 2, section 5.2.)

Do (T1):

```bash
inc "$T0"                                        # one line per incident since the demo started
H "$CORRELATOR_API_TOKEN" "http://$N:30804/incidents?since=$T0&limit=30" | jq '.items[0] | {kind, status, severity, summary, root_cause, t_first_signal, t_located, t_mitigated}'
curl -s http://$N:30804/metrics | grep -E 'evcharge_correlator_(incidents_total|incidents_open|time_to_locate_seconds_count)'
```

Audience sees: one incident for the botnet fleet (root cause the city), one for the card (root cause key type
`card`), one for the hijack attempt (root cause the charger; `t_mitigated` empty because prevention happened at
the handshake, not a response). `t_located` minus `t_first_signal` is the time to locate; say it in seconds.

Fallback: `jq` over the recorded incidents, which hold the headline numbers:

```bash
for f in evidence/correlator/incidents/*.json; do jq -c '{kind, root: .root_cause.key, first: .t_first_signal, located: .t_located, mitigated: .t_mitigated}' $f; done
```

### (e) Scalability and limitations (10:15 to 11:00)

Say (report table 6.2 on screen, or the `evcharge-platform` tab):
- One laptop, synthetic OCPP load on Aveiro: 30 to 140 connected chargers, latency flat (p95 8 to 9 ms, p99 24 ms at the top), about 110 msg/s sustained and 170 peak; every stop was a configuration limit (edge `limit_conn` 24 per source, HPA maximum 4, the per-charger limiter), not RAM. Prometheus series stayed at about 4,500 because monitoring is per station and per city.
- 110 msg/s corresponds to about 1,500 real chargers at 0.07 msg/s each: an extrapolation, not a measurement.
- Limits to state: correlator results replayed, not fresh (unless 5.2 was done); bulk quarantine cuts drivers off; cloned-card and meter thresholds are tuned to simulated data; ACLs do not stop same-bridge traffic (no `br_netfilter`); the heartbeat generator is lighter than real traffic.

## 3. Reset between rehearsals and after the demo

There is no single reset script. The PoC scripts undo their own `sim.env` edits and temporary containers on exit,
but the botnet compromise and every state change in 1.2 are manual. Expected time: about 5 minutes
(the slowest parts are evsim restarts and the chargers' reconnect backoff; the registry and card steps take seconds).
Do the steps in this order.

### 3.1 Quick check of what is left

```bash
incus list -f csv -c n | grep -E 'rogue|c2|mitm'; incus network list -f csv | grep -i -E 'mitm|rogue'
ls /tmp/evcharge-botnet-compromise.state
quar; conn pt
```

### 3.2 Ordered commands

1. Stop attacker containers (the scripts delete their own on exit; this covers an interrupted run):
   `for i in $(incus list -f csv -c n | grep -E '^(porto|lisboa|aveiro)-rogue$|^c2$|mitm'); do incus delete -f $i; done`
2. Restore every station `sim.env` and restart where changed:
   - botnet: `EVCHARGE_BASELINE_DONE=1 poc/botnet/compromise.sh --restore --run` (it detects the compromise flag on the instances themselves and cleans every affected station, so it works after a reboot and is safe to run when nothing is compromised).
   - verify all 15 stations (must print nothing):
     ```bash
     for c in porto lisboa aveiro; do for n in 1 2 3 4 5; do
       incus exec $c-st-00$n -- grep -H 'SIM_EXTRA_ARGS=.' /etc/evcharge/sim.env | sed "s/^/$c-st-00$n /"; done; done
     ```
   - for a station that still shows arguments (or whose state file was lost), write the clean file and restart (`<city>` is `porto`, `lisboa` or `aveiro`; the URL is `wss://edge.<city>.pt.lab/ocpp`, and `ws://edge.aveiro.pt.lab/ocpp` for Aveiro):
     `incus exec <city>-st-00N -- sh -c 'printf "SIM_CSMS_URL=%s\nSIM_EXTRA_ARGS=\n" "<url>" > /etc/evcharge/sim.env' && incus exec <city>-st-00N -- systemctl restart evsim`
3. Release quarantines and set chargers Operative (the CSMS keeps a released charger Unavailable until told otherwise):
   ```bash
   quar                                              # list
   for id in $(curl -s "http://$N:30801/quarantine?prefix=pt" | jq -r '.items[].hier_id'); do
     H "$REGISTRY_ADMIN_TOKEN" -X POST -H 'Content-Type: application/json' -d '{"reason":"demo reset","source":"operator"}' "http://$N:30801/quarantine/$id/release" | jq -c '{hier_id,state}'
   done
   sleep 20      # the chargers reconnect within their backoff
   for s in pt.porto.st-001 pt.porto.st-002 pt.lisboa.st-001 pt.lisboa.st-002 pt.aveiro.st-001 pt.aveiro.st-002; do for cp in cp-1 cp-2; do
     H "$CSMS_API_TOKEN" -X POST -H 'Content-Type: application/json' -d '{"reason":"demo reset","source":"operator","connector_id":0,"type":"Operative"}' "http://$N:30800/commands/$s.$cp/change-availability" | jq -c '{hier_id,status}'
   done; done
   ```
   (`status` `NotConnected` means the charger has not reconnected yet; repeat after 20 s.) The quarantine of a whole station by the fraud service is released the same way, on the station id.
4. Return fraud to dry-run first, then unblock the card (in this order, otherwise the stale clone session can block the holder again; see `evidence/fraud/enforcing-redemo/README.md`, Restore):
   ```bash
   kc -n evcharge set env deploy/fraud FRAUD_DRY_RUN=true
   curl -s -X POST -H "Authorization: Bearer $BILLING_API_TOKEN" -H 'Content-Type: application/json' \
        -d '{"reason":"demo reset","source":"operator"}' http://$N:30802/cards/E3EE5BE2ED4120/unblock | jq -c '{card,status}'
   ```
5. Flush stale edge firewall drops. The hijack response (`evcharge-edge-drop` on 110132) and `firewall-drop` add `iptables DROP` rules on a city edge; the response removes them after 600 s, but an interrupted run leaves them, and a leftover rule for a station address blocks that whole station from the edge (seen in practice: all of Porto and Lisboa cut off). Clear them on every edge and confirm none remain:
   ```bash
   for e in porto-edge lisboa-edge aveiro-edge; do for ch in INPUT FORWARD; do
     while incus exec $e -- iptables -S $ch | grep -q -- '-j DROP'; do
       incus exec $e -- sh -c "iptables \$(iptables -S $ch | grep -- '-j DROP' | head -1 | sed 's/^-A /-D /')"
     done; done; done
   for e in porto-edge lisboa-edge aveiro-edge; do printf '%s ' $e; incus exec $e -- sh -c 'iptables -S | grep -c -- "-j DROP"'; done   # each must print 0
   ```
6. Certificate revocation list fresh. It is now valid 30 days, but check it has not lapsed: `incus exec porto-edge -- openssl crl -in /etc/evcharge/pki/crl.pem -noout -nextupdate`. If expired or close, `pki/crl.sh && gateway/push_crl.sh`, then restart evsim on the Porto and Lisboa stations so they reconnect without waiting out the backoff.
7. Re-apply the city ACLs and check them: `EVCHARGE_BASELINE_DONE=1 infra/08_acls.sh apply --run` then `infra/08_acls.sh check` (it builds the station list from the current addresses; run it after every container re-creation).
8. Aveiro back to plain ws: nothing is changed by the demo, so just verify `conn pt.aveiro` shows `tls:[false]`, 10 connected. If Aveiro was ever moved to mTLS in a rehearsal, revert with `gateway/deploy_edge.sh aveiro --plain`, re-apply the Aveiro ACL, and restore `SIM_CSMS_URL=ws://edge.aveiro.pt.lab/ocpp` in each Aveiro `sim.env`.
9. The hijack script restarts the real station's `evsim` itself; check `incus exec porto-st-004 -- systemctl is-active evsim`.
10. Check the end state: `eval/scale.sh health` must show `connected_all=30`, `quarantined=0`; `conn pt`, `quar`; no firing alert after a few minutes; `incus list -f csv -c n | grep -E 'rogue|c2'` empty.
11. Correlator noise: open incidents close themselves after 600 s of quiet (`CORRELATOR_CLOSE_S`); closed history stays. Filter with `inc $T0` rather than deleting anything. Rule 110300 (level 6) does not reach the correlator, so flood alerts never appear there.
12. Wazuh guards that persist across rehearsals: `block-card` allows 10 distinct cards per hour and `quarantine-charger` 5 distinct chargers per 10 minutes. The demo uses one card and the botnet quarantine is done through the API, so neither limit is reached; do not rehearse more than twice in an hour with extra cards.

### 3.3 What to leave for the demo itself

After a full reset, wait 5 minutes and run section 1.1 again before the next run.

## 4. Failure modes during the demo

| # | Symptom | Do |
|---|---|---|
| 4.1 | Wazuh dashboard slow or blank | Do not wait on it. T4 (`walerts`) reads `alerts.json` directly and is the primary view; the dashboard is secondary. If the indexer is behind, say "the dashboard indexes a few seconds later" and keep going. If `wazuh-indexer` is down, `incus exec wazuh-container -- systemctl start wazuh-indexer wazuh-manager wazuh-dashboard` before the demo only. |
| 4.2 | An alert does not fire | Say what it should have shown, show the rule id and the recorded alert from `evidence/<threat>/`, move on within 15 s. Segment fallbacks are listed in section 2. Check `incus exec wazuh-container -- tail -n 20 /var/ossec/logs/ossec.log` afterwards, not during. |
| 4.3 | C2 or compromise fails (ACLs still applied, no `c2` instance, DNS on the central bridge) | Check `incus list c2`, `infra/08_acls.sh status`. If not fixed in 30 s, drop the live botnet, present it from `evidence/botnet/`, and run reset 3.2 after the demo. |
| 4.4 | Laptop under memory or CPU pressure (slow Grafana, evsim restarts lag) | Close extra browser tabs first. Keep the Grafana refresh at 10 s, not 5 s. Do not run the scalability tooling. `free -g`; below 4 GiB available, skip the botnet live. |
| 4.5 | Clock skew on the K3s VMs (seen once: 2 h 34 min behind the host) | Read-only check: `date -u; incus exec k3s-master -- date -u; incus exec k3s-node1 -- date -u; incus exec k3s-master -- timedatectl; incus exec k3s-master -- chronyc tracking`. Healthy: `System clock synchronized: yes`, chrony `System time` within milliseconds. Today chrony is active on the VMs. After a laptop suspend or resume the VMs can lag until chrony steps them; restarting chrony is a repair, not a check: `incus exec k3s-master -- systemctl restart chrony` (and `k3s-node1`), do it before the demo, never during. Symptom if skewed: correlator `t_located` negative or hours, CSMS log timestamps off from the edge. |
| 4.6 | Porto and Lisboa chargers refused (HTTP 400 in the edge log, count of connected chargers stuck at 10) | The CRL expired (7 days). `incus exec porto-edge -- openssl crl -in /etc/evcharge/pki/crl.pem -noout -nextupdate`. Repair before the demo: `pki/crl.sh && gateway/push_crl.sh`, then wait for the chargers to reconnect (about 1 to 2 minutes). Not a mid-demo fix. |
| 4.7 | Rogue container never becomes ready (no uplink, `wait_ready` waits for DNS) | The check is `getent hosts deb.debian.org` inside the new container. With no network it fails after about 2 minutes and the script aborts. Plug in the uplink or a phone hotspot before the demo; otherwise present the hijack from `evidence/hijack/`. Test once with the laptop on the demo network. |
| 4.8 | Aveiro in a mid-state (some chargers connected, `tls` mixed, or an edge stopped) | `conn pt.aveiro`, `incus list aveiro-edge`, `incus exec aveiro-edge -- systemctl status nginx`. A stopped edge: `incus start aveiro-edge`, all 10 reconnect within about 30 s to 2 min. A mixed `tls` means a rehearsal switched it to mTLS: follow 3.2 step 6. |
| 4.9 | A script hangs | Ctrl-C once: the PoC scripts run their cleanup on `INT` and `TERM`. Then 3.1 to see what is left. |

## 5. Two validation runs before the demo day

These close the two gaps the report states (section 5.3, 6.4, 6.5, 7.2). Do them on a rehearsal day, not the
demo day, with the lab in the 1.2 demo state. Capture evidence into new folders so nothing recorded is overwritten.

### 5.1 Live botnet with `global_frequency`: 110304 and 110308

Pre-state: ACLs removed, 30/30 connected, 0 quarantined, no `c2`, clean `sim.env` (1.1). Wazuh has
`global_frequency` loaded: `incus exec wazuh-container -- grep -c global_frequency /var/ossec/etc/rules/evcharge_botnet.xml` prints `5`
today (110302 and 110308 among them, plus 110304); and `analysisd.rule_matching_threads=1` is set in `local_internal_options.conf`.

Steps:
1. `T0=$(date -u +%Y-%m-%dT%H:%M:%SZ)`; in T4 run `walerts | tee /tmp/botnet-live-alerts.txt`.
2. T2: `export EVCHARGE_BASELINE_DONE=1; poc/botnet/deploy_c2.sh --run --surge-s 300 --duration 480`. The longer surge is deliberate: the Prometheus `CityLoadSpike` alert has `for: 2m` on a 30 s federation, so the default 120 s surge may end first.
3. T3: `poc/botnet/measure_collateral.sh --run --phase before`, then `poc/botnet/compromise.sh --run --stations 6 --cities porto,lisboa,aveiro --duration 60`.
4. Do not quarantine. Wait for the whole schedule (about 8 minutes); the earlier run's `CityLoadSpike` never fired because the response cut the surge.
5. Expected: `110303` x6, then `110304` (level 12, "3 stations contacting ..."), `110300`/`110301`, `110308` (10 sessions in 60 s per city), Prometheus `CityLoadSpike` firing (Alertmanager tab).
6. After the schedule: `poc/botnet/measure_collateral.sh --run --phase after`, then reset 3.2 steps 1 to 3 (no quarantine happened, so only the `sim.env` restore matters).

Evidence to capture into `evidence/botnet/live-global-frequency/`: the filtered Wazuh alerts for the window
(`incus exec wazuh-container -- cat /var/ossec/logs/alerts/alerts.json | jq -c --arg t "$T0" 'select(.timestamp >= $t and (.rule.id|startswith("1103")))' > .../wazuh-alerts.jsonl`), the Alertmanager alerts (`curl -s http://$N:30093/api/v2/alerts > .../alertmanager.json`), the city power range query (as in `evidence/botnet/prom/`), the dashboard screenshots (110304 alert opened to its JSON, the Geomap during the surge), and the timestamps of the first 110303 and of 110304.

Result handling: if 110304 and 110308 fire, the sentences "proven in `wazuh-logtest` but not yet re-proven in a live two-node run" (5.3 finding 1, 6.4 Botnet, 7.2 first bullet, 7.3 first bullet) can be upgraded to live-proven with the observed times. If 110304 does not fire, keep the wording and record why (agents, thread setting).

### 5.2 Live (non-replay) correlator run with Wazuh forwarding

Goal: show an incident whose Wazuh signal arrived through the `custom-correlator` integration while the correlator was already up (the report states that case was not exercised).

Pre-state as in 1.1. Wazuh forwards to `http://k3s-master.incus:30804/signals/wazuh` (level 7 and above, plus response ids 110405, 110410, 110209).

Steps:
1. `T0=$(date -u +%Y-%m-%dT%H:%M:%SZ)`; note the correlator count: `curl -s http://$N:30804/metrics | grep evcharge_correlator_signals_total` (record the `source="wazuh"` value). Clear the outbox check: `incus exec wazuh-container -- wc -c /var/ossec/logs/evcharge-correlator-outbox.json` (record the size).
2. Run the live fraud clone (2.c2) and the live stolen-certificate hijack (2.c3) in sequence, and optionally the botnet (5.1).
3. After each, `inc $T0` and `curl -s http://$N:30804/metrics | grep evcharge_correlator_signals_total`: the `wazuh` count must rise during the run, with the outbox size unchanged (so the signals were delivered live, not replayed from the outbox). `incus exec wazuh-container -- tail -n 20 /var/ossec/logs/integrations.log` shows the integration posts.
4. Confirm in the incident JSON (`GET /incidents/{id}`) that the signals list holds `source: wazuh` entries with rule ids 110104 (hijack) and 110210 plus `billing.card_blocked` (fraud), and that `t_located - t_first_signal` is recorded.

Evidence to capture into `evidence/correlator/live/`: the incident JSON files (`curl -s -H "Authorization: Bearer $CORRELATOR_API_TOKEN" http://$N:30804/incidents/<id> > ...`), the signals-total before and after, the `integrations.log` excerpt, and the Alertmanager-sourced incident if one arose.

Result handling: if it works, 6.5 ("Replay versus live") and 7.2 can say which incidents were live, with their locate times; the "Live Wazuh forwarding ... was not exercised" sentence in `evidence/correlator/README.md` can be corrected. Otherwise keep the replay wording and record the failing hop.

## 6. Rehearsal plan

Rehearse twice from a full reset (section 3, then 1.1 to 1.4), with a timer, speaking the talking points aloud.
The third pass is the real demo. Rehearse once with the laptop on the demo network or hotspot (4.7) and once
with Wi-Fi off, to see what depends on an uplink.

Fill in the actual times (mm:ss), measured with a stopwatch from the start of each segment:

| Segment | Target | Run 1 | Run 2 | Notes (what fired late, what to cut) |
|---|---|---|---|---|
| (a) Architecture and naming | 1:15 | | | |
| (b) Monitoring, grey marker, botnet start | 2:15 | | | |
| (c1) Botnet: first 110303 at | about 2:10 | | | |
| (c1) Botnet: 110304 at | about 2:15 | | | |
| (c1) Botnet segment total | 2:00 | | | |
| (c2) Fraud: block visible at | about 6:30 | | | |
| (c2) Fraud segment total | 1:45 | | | |
| (c3) Hijack: first 403 at | about 8:00 | | | |
| (c3) Hijack segment total | 1:30 | | | |
| (d) Correlation | 1:30 | | | |
| (e) Scalability and limits | 0:45 | | | |
| Total | 11:00 | | | |
| Reset duration (3.2) | 5:00 | | | |

After each run: write down what to cut or move, check 3.2 step 8, and check the three dashboards for stuck alerts.
If the total exceeds 12:00, cut in this order: the optional lines in (a) and (c3), the Wazuh dashboard
(use T4 only), then the live hijack (use the evidence), keeping botnet, fraud and correlation as the core.
