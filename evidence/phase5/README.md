# Phase 5 evidence: Wazuh rules and active responses live

Run of 2026-11-06 (UTC, 11:08 to 12:29). Manager `wazuh-container` (Wazuh 4.14.9), 20 project agents
plus the manager. Addresses are resolved by name at run time and appear only inside the raw alert
excerpts. Raw excerpts: `alerts/*.json` (trimmed Wazuh alerts, one per line) and `ar/*` (response
audit lines written by the scripts).

## Agents and rules

| Group | Agents | Names |
|---|---|---|
| central | 2 | `pt.central.k3s-master`, `pt.central.k3s-node1` (DaemonSet pods) |
| edge | 3 | `pt.porto.edge`, `pt.lisboa.edge`, `pt.aveiro.edge` |
| station | 15 | `pt.<city>.st-001` .. `st-005` for the three cities |
| manager | 1 | `wazuh-container` (agent 000) |

`agent_control -l`: 21 Active (the old course agent `...node01-apache` stays Disconnected, its
container is stopped). Rules 110000 to 110499: 61 rules (base 7, hijack 23, fraud 9, botnet 9,
infra 13, catch-alls counted in infra). `wazuh/tests/run_logtest.sh`: 360 cases, 360 passed, 0 failed.

## Scenario windows (UTC, 2026-11-06)

Everything below disturbed the platform and must be excluded from a baseline.

| What | Window | Effect on the platform |
|---|---|---|
| Billing and CSMS rollout (responder tokens) | 11:08:12 to 11:08:42 | one CSMS pod at a time; chargers reconnected, 30 of 30 after 30 s |
| Manager restarts | 11:12, 11:20, 11:52, 11:53, about 12:07 | alerts delayed for about 1 minute each |
| Card 0942D7BDC322D7 blocked by the unfixed 110203 | 11:13:44 to about 11:19 | one simulated card refused |
| Agent group and station restarts for the fixes | 11:15, 12:00:50 to 12:02:10 (all 15 stations) | agents restarted, evsim untouched |
| Scenario 1, bad certificates, `porto-rogue` | 11:50:58 to 11:51:24; drop until 12:01:08 | one rogue address dropped on `porto-edge`; probe of an unknown id 11:50:18 to 11:50:30 |
| Scenario 6, rate guard | 11:52:20 to 11:53:46 | 5 test ids quarantined (not connected), then released and deleted |
| Scenario 5, C2 beacon | 11:54:15 to 12:03:04 | `lisboa-st-002` ran `--compromised` against a temporary `c2` |
| Scenario 2, unknown-id scan | 12:03:19 to 12:03:28 (no response, see below); 12:05:10 to 12:05:35 (drop until 12:15:23); 12:16:22 to 12:16:43 (drop until 12:26:30) | rogue address dropped on `porto-edge` |
| Scenario 4, cloned card | 12:09:41 to 12:16:12 | `lisboa-st-001` and `porto-st-004` restarted with clone options; card blocked 12:12:22 to 12:16 |
| Scenario 3, stolen certificate | 12:27:03 to 12:28:20 | `pt.porto.st-003.cp-1` quarantined 12:27:04, certificate revoked 12:27:38 (CRL push reloaded the Porto and Lisboa edge nginx), re-issued and reconnected 12:28:20 |

## Quiet hour and false-positive tuning

Quiet hour: 11:20:24 to 11:50:01, normal load (30 of 30 connected). After tuning there were
**0 alerts of level 7 or higher** from the project groups; the only project alerts were level 3
(`session.reject`, 17, `ConcurrentTx` refusals of normal traffic) and level 4 (3 refusals of
cards that were seeded as blocked, rule 110200).

Before tuning (06:00 to 11:20) three rules produced alerts on normal load:

| Rule | Observed | Cause | Fix |
|---|---|---|---|
| 110305 level 7 | about 250 per hour, every station | `check_diff` on the raw `ss` output: queue sizes, pids and ephemeral ports change between two runs | the command is `netwatch.py --sockets`, which prints only destinations outside the allow file; the output is constant on a quiet station |
| 110206 level 10 + `block-card` | about 5 per hour | simulated drivers use a card of another city for 20 % of their sessions (home bias 0.8); two cities within 30 minutes is normal here | window 120 s (a live clone is caught by 110203 and 110207) |
| 110203 level 10 + `block-card` | 3 in 12 h (one blocked a real card at 11:13:44 once the tokens were valid) | a `ConcurrentTx` refusal in two cities within 10 minutes | window 120 s |

The card blocked at 11:13:44 was unblocked with the admin token. Earlier occurrences were
`block-card error: http 401` (the responder tokens were not yet in `ar.env`), so no card was
blocked before the fix. The 110404 alert at 11:09 is the responder token smoke test
(`registry` write refused for the responder, expected).

## Scenarios

Timings are event (log line) to alert to response, on the clocks of the manager and the agents
(they agree to about 1 s).

### 1. Bad certificates from one host: `firewall-drop` at the edge

`porto-rogue` (temporary, from image `evcharge-city`) presented a self-signed certificate
5 times to `wss://edge.porto.pt.lab/ocpp/pt.porto.st-005.cp-1`.

| Time | Event |
|---|---|
| 11:50:59 to 11:51:05 | edge answers 400, `cert_verify=FAILED:unable to get certificate CRL` (the foreign CA) |
| 11:51:00.046 to 11:51:06.009 | rule 110120 (level 8, T1552.004) per attempt |
| 11:51:06.011 | rule 110121 (level 11): 5 rejected certificates within 120 s |
| 11:51:06 | `firewall-drop` runs on `pt.porto.edge`: `INPUT` and `FORWARD` DROP for the rogue address |
| 11:51:07 | the next handshake times out (and the following ones) |
| 11:51:08 | rule 651 "Host Blocked by firewall-drop" |
| 12:01:07 / 12:01:08 | delete after 600 s, rule 652 "Host Unblocked"; `iptables -S` shows 0 DROP |

Files: `alerts/s1-bad-certs.json`, `alerts/s1-firewall-drop-expiry.json`,
`ar/s1-porto-edge-firewall-drop.txt`. Lisboa and Aveiro edges: 0 DROP throughout.
A first try with a certificate of the lab CA that is not in the edge CRL (`pt.porto.st-005.cp-9`,
revoked in the CA but never pushed) passed the edge and was refused by the CSMS as an unknown id
(`ident.unknown`, 7 times, 110108), as it should.

### 2. Unknown-id scan: `evcharge-edge-drop`

Twelve random ids (`pt.porto.st-9xx.cp-N`) through `porto-edge` with a valid certificate that
belongs to no registered charger.

| Run | Result |
|---|---|
| 12:03:19 (rule as written: 110110 = 10 within 60 s) | 12 x 110108, **no 110110, no drop**: Wazuh counts a frequency rule per agent and the edge spread the ids over two CSMS pods (two agents), 6 each |
| 12:05:10 (same rule, 24 ids) | 110110 on each pod at the 10th event of that pod; `evcharge-edge-drop` and, because the edge itself counted 20 unknown paths, 110123 + `firewall-drop` both fired (two DROP pairs on `porto-edge`) |
| 12:16:22 (rule fixed: 110110 = 5 within 60 s) | the 9th id: 110110 at 12:16:29.220; `evcharge-edge-drop` on `pt.porto.edge` at 12:16:29.485 (`drop_added`); the 10th to 12th handshakes time out; no 110123 (12 requests < 20) |

Removal: `drop_removed` at 12:26:30.194 (600 s), `iptables -S` 0 DROP. The agents of `lisboa-edge`,
`aveiro-edge` and all 15 stations received the command and did nothing: no DROP rule and no
`/var/log/evcharge/ar-edge.json` (the script is silent when `role` is not `edge` or the address is
outside the city network, so there is no log line to show for the no-op).
Files: `alerts/s2-unknown-scan.json`, `alerts/s2-first-run-per-agent-count.json`, `ar/s2-porto-edge-drop.json`.

### 3. Stolen certificate: `quarantine-charger`

Real `pt.porto.st-003.cp-1` connected; the certificate and key copied from `lab-ca` into `porto-rogue`.

| Time | Event |
|---|---|
| 12:27:03 | rogue handshake, CSMS answers 409 |
| 12:27:03.625 / .626 | 110104 (`ident.ip_mismatch`, level 7) then 110131 (level 12, IP mismatch then duplicate) |
| 12:27:03.737 | `ar.evidence` snapshots (connection, node) before any change |
| 12:27:04.530 | `ar.action quarantine-charger ok` (rule 110410): registry `quarantined`, remote-stop, `Inoperative`, disconnect, revocation request |
| 12:27:05.339 | the real charger reconnects and is refused: 110111 `ident.quarantined` |
| 12:27:25 | `GET /connections/pt.porto.st-003.cp-1` 404; `pki/process_revocations.sh --list` shows 1 request |
| 12:27:38 | `pki/process_revocations.sh --yes`: certificate revoked, CRL pushed to Porto and Lisboa (Aveiro plain, skipped) |
| 12:27:50 | the rogue retries with the revoked certificate: edge 400 (110120) |

Event to response: 0.9 s. 110131 runs `quarantine-charger` only; 110130 (the other order) also
runs `evcharge-edge-drop` and did not occur in this run. Files: `alerts/s3-stolen-cert.json`,
`alerts/s3-after-revocation.json`, `ar/s3-quarantine-charger.json`.
Restore (12:27:51 to 12:28:20): `pki/issue.sh --force`, registry `PATCH cert_serial`, `pki/export.sh`
to `porto-st-003`, registry release, `change-availability Operative`, `systemctl restart evsim`;
connected again with the new serial, `status Available`.

### 4. Cloned card: `block-card`

`lisboa-st-001` restarted with `--clone-card 0942D7BDC322D7 --clone-sessions 2` and `porto-st-004` with
`--clone-sessions 8` for the same card (a Porto card).

| Time | Event |
|---|---|
| 12:10:36 | the card starts a session at Porto |
| 12:12:21.214 | Lisboa presents the card while the Porto session is open: CSMS refuses `ConcurrentTx`; rule 110203 (level 10, T1078) |
| 12:12:21.288 | `ar.action block-card ok` (rule 110410), evidence `GET /sessions?card=` first |
| 12:12:23 | billing audit `billing.card_blocked` (110209); card `status=blocked`, reason names rule 110203 |
| 12:12:45 | later authorize of that card: `Blocked` (110200) |

The authorize check refuses a card that is open elsewhere before the second `session.start` exists,
so rule 110206 (two cities) did not fire live and no session was flagged (that step needs a
`transaction_id`). 110206 is covered by logtest only. Files: `alerts/s4-cloned-card.json`,
`ar/s4-block-card.json`. Restore: `sim.env` restored on both stations, evsim restarted, card unblocked
with the admin token (`active`).

### 5. C2 beacon on a station: `host.new_outbound`

Temporary `c2` on the central bridge running `sim/tests/stub_c2.py --task idle` (wrapped so the
HTTP connection stays open); `lisboa-st-002` with `--compromised --c2-url http://c2.incus:<port>`.

| Time | Event |
|---|---|
| 11:56:20 | netwatch writes `host.new_outbound` for the C2 address |
| 11:56:27 | rule 110305 (level 7, socket table changed) |
| 12:02:11 | rule 110303 (level 8, T1071) after the log collector read the file (see problems) |

The stock stub answers with HTTP/1.0 and closes each beacon, so the short connection never
shows in a 10 s `ss` snapshot. Files: `alerts/s5-c2-beacon.json`. Restore: `c2` deleted, `sim.env` restored.

### 6. Rate guard of `quarantine-charger`

Six unconnected test ids `pt.porto.st-005.cp-3` .. `cp-8` (Porto token), six synthetic
`ident.cert_mismatch` lines in a temporary localfile of the manager, 3 s apart (11:52:35 to 11:52:51).
Result: 110101 six times; `quarantine-charger ok` for cp-3 to cp-7 (110410); the sixth
`ar.action result=refused reason=rate_guard` (110411, level 10) at 11:52:53.657. Files:
`alerts/s6-rate-guard.json`, `ar/s6-rate-guard.json`. Restore: released, deleted, localfile and its
file removed, manager restarted, the 5 revocation requests of the test ids removed from the queue.

### 7. Correlator stub

The correlator is not deployed yet, so every alert of level 7 or more from `evcharge` is appended to
`/var/ossec/logs/evcharge-correlator-outbox.json`: during the run rules 110101, 110110, 110120,
110121, 110123, 110203, 110204, 110303, 110305 and 110411 are in it (`alerts/s7-correlator-outbox.json`).
The file also holds about 1 900 110305 lines from before the tuning.

## Collateral damage seen

- Scenario 3 isolated the real `pt.porto.st-003.cp-1` together with the thief (about 1 minute of
  lost service for one charger; the revocation forced a new certificate).
- Scenario 4 blocked the card of the real owner (`driver-0010`) for 4 minutes.
- The unfixed 110203 had already blocked one real card once (11:13:44); the unfixed 110206 would
  have blocked about five per hour.
- The edge drops hit a whole address; 600 s expiry, gateways, edges, nodes and the manager are never dropped.

## Problems found and fixed

- `evcharge-edge-drop`, `/etc/evcharge/role` and `city-net` were not installed on the edges
  (`wazuh/agents/install_native.sh`); the script is now also installed on the stations.
- `netwatch.json` did not exist when the stations' log collectors started and was never read;
  it is created empty at install.
- 110305 on raw `ss` output (250 alerts per hour); 110203 and 110206 on normal traffic; 110110
  across two agents (see above).

## Memory

| Item | Value |
|---|---|
| Wazuh agent on a station | about 57 MiB RSS (agentd, logcollector, modulesd, syscheckd, execd) |
| Wazuh agent on an edge | about 47 MiB RSS |
| DaemonSet pod | 32 MiB each (`kubectl top`) |
| Manager container | 3.9 GiB in use of 6 GiB; `wazuh-analysisd` 1.4 GiB RSS; 2.5 GiB available |
| Station container (`incus list`, includes page cache) | 190 to 212 MiB of 224 MiB |
| Edge container | 235 to 278 MiB |
| Host | 5.3 GiB available at the end (23.8 GiB total) |

## Final state

30 of 30 chargers connected, 0 quarantined, 0 DROP rules on the three edges, no `*-rogue` or `c2`
instance, `sim.env` restored on `lisboa-st-001`, `lisboa-st-002`, `porto-st-004`, no pending
revocation requests. The certificate `pt.porto.st-005.cp-9` stays revoked in the CA (test identity).

## Screenshots to take (Wazuh dashboard)

1. Threat Hunting, events filtered on `rule.groups:evcharge`, time range 11:50 to 12:30 on 2026-11-06.
2. The MITRE ATT&CK view for the same range (T1552.004, T1595, T1078, T1036, T1071).
3. The agent list (21 Active).
4. The alert of rule 110131 and of rule 110121 opened to show the JSON.
