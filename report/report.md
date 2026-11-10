# Threat detection, monitoring and prevention in a regional EV charging network

Gonçalo Almeida — Security in Large-Scale Systems (SSLE), University of Aveiro — Project 1 — November 2026

## 1 Introduction

### 1.1 The system
We built and secured a regional operator's EV charging network. Chargers speak OCPP 1.6J (JSON over WebSocket) to a central charging-station management system (CSMS) on K3s. The chargers sit in three cities (Porto, Lisboa, Aveiro), each on its own Incus network behind an nginx edge. Around the CSMS run a registry (inventory and delegation), billing, fraud scoring and a correlator. The correlator joins Prometheus, Wazuh and fraud-service signals into one incident per root cause (section 4.3). Prometheus and Alertmanager provide metrics and alerting; Wazuh provides host and log detection and active response. All components share one hierarchical naming scheme, `pt.<city>.<station>.<charger>` (interfaces §1).

### 1.2 Why it matters
A charging network is critical infrastructure on three counts. It draws load from the grid, so coordinated behaviour by many chargers is a grid-stability issue. It handles money, so metering and card authorisation must be trustworthy. Drivers depend on its availability. In the syllabus it is a large, geographically distributed system whose monitoring cost, not only its security, has to scale.

### 1.3 Scope: three threats
All three are on the course threat list: (i) hijacking of a charger identity or its signals, (ii) fraud against accounting (cloned cards, tampered meters), (iii) a botnet of compromised chargers (C2, floods, synchronised load). Section 5 treats each.

### 1.4 Contributions
- One identity end to end: the same string is the certificate name, OCPP path, registry key, Prometheus/Wazuh labels and map label.
- Correlation to one located incident: Prometheus, Wazuh and fraud-service alerts are joined by a correlator and located via the registry, in 0.1 to 3.6 s on replayed recordings (section 6).
- Honest post-mortems of three live PoC threats, including the detection gaps they exposed and the fixes (section 5).
- Measured federated monitoring: a two-tier Prometheus design cut inter-site monitoring traffic by 99.3% in a controlled comparison (section 4.5).
- Baselined thresholds computed from recorded data rather than chosen ad hoc (section 4.4).
- Quantitative scalability (section 6) and honest limitations (section 7).

### 1.5 Roadmap
Section 2 reviews tools and EV-security context, 3 the architecture, 4 risk and monitoring, 5 the threats, 6 the evaluation, 7 the conclusions.

## 2 Relevant state of the art

### 2.1 Monitoring stacks
We compared Prometheus, Zabbix and Datadog. Zabbix is server-centric with agents and templates, suited to inventory-style polling but with a heavier central database and a less natural per-label time-series model. Datadog is hosted SaaS: it removes operations work but requires every site to ship data to an external provider and carries per-host cost, which does not fit a lab that must measure and minimise inter-site traffic. Prometheus is self-hosted, pull-based, with a label-based query language (PromQL), recording rules and federation, so a site can expose only pre-aggregated series to a parent. Federation is the mechanism that keeps per-charger series inside each city, which is why we chose it (interfaces §14; measured in 4.5).

### 2.2 Detection and SIEM
Wazuh descends from OSSEC and adds a manager/indexer/dashboard, custom decoders and rules, active response and a MITRE ATT&CK mapping; OSSEC alone lacks the indexer and dashboard. Falco detects at syscall level (eBPF), strong for container runtime behaviour but with no application-log correlation, so it is complementary rather than a substitute. Splunk is a capable commercial SIEM but heavy and licensed. We chose Wazuh because it parses our structured JSON logs with custom decoders and rules, ships built-in active responses, and was used in the course. We do not use a network IDS (Snort, Suricata) because OCPP is encrypted (wss with mTLS), so payload signatures would see nothing useful.

### 2.3 EV-charging security context
OCPP 1.6J, the deployed version, has no built-in security profile; transport protection is left to the deployment. OCPP 2.0.1 adds Security Profiles (including TLS with client certificates), and the Open Charge Alliance security whitepaper for 1.6 describes similar hardening for older fleets. ISO 15118 defines Plug&Charge with contract certificates. Typical attack classes on public charging are identity spoofing of a charge-point id, meter and transaction fraud, and compromised firmware enrolled in a botnet. These motivate our mTLS edge, the binding of the certificate subject to the hier-id (interfaces §5.2, §12), and a deliberately unprotected legacy city (Aveiro, plain ws) to show hijacking before and after hardening.

### 2.4 Frameworks
Detection rules are mapped to MITRE ATT&CK (Enterprise) and ATT&CK for ICS. The operational workflow follows FCAPS, concentrating on Fault (availability alerts, inhibition) and Accounting (billing, fraud). Risk is structured with an attack-defense tree (Kordy et al.), which extends Schneier's attack trees with countermeasure nodes (4.2).

## 3 Service architecture used

### 3.1 Overview
The central platform runs on K3s (two nodes in Incus VMs): CSMS with 2 to 4 replicas under a horizontal autoscaler, registry, billing, fraud, correlator, a PostgreSQL StatefulSet with a persistent volume, central Prometheus, Alertmanager and Grafana (interfaces §2.2). The correlator receives alerts from three sources — Alertmanager (webhook with a bearer token), a Wazuh `custom-correlator` integration (level 7 and above plus selected response rule ids) and the fraud service — and exposes `/incidents` and its own metrics (NodePort 30804). Wazuh runs in its own container with agents on edges and stations. Each city is an Incus network containing an nginx edge, a city Prometheus on that edge and simulated stations (Figure A).

> Figure A — Architecture. Central K3s, Wazuh, three city networks (edge, city Prometheus, stations), attacker containers, the correlator pod with its three inbound signal arrows (Alertmanager, Wazuh, fraud), the federation link and the wss+mTLS links; the caption states the physical/logical split. (To be drawn by the author.)

### 3.2 Hierarchical naming
Identifiers are `pt.<city>.<station>.<charger>` (e.g. `pt.porto.st-003.cp-2`); cities are `pt.<city>`, edges `pt.<city>.edge`, central nodes `pt.central.<node>` (interfaces §1.1). The same string is used everywhere (interfaces §1.2): certificate CN, OCPP URL path `/ocpp/<hier-id>`, registry key, Prometheus labels (`hier_id`, `city`, `station`, `charger`), Wazuh agent name or log field, DNS name and certificate SAN (`st-003.porto.pt.lab`), Incus key `user.hier-id`, and the map label. An alert carrying a `hier_id` is therefore located by one registry lookup, and prefix matching on dot boundaries yields a city or station subtree.

> Figure B — Naming scheme. The hier-id tree and the same string in certificate, OCPP path, registry, Prometheus, Wazuh, DNS. (To be drawn or typeset by the author.)

### 3.3 Physical versus logical naming
Central services are logical: Kubernetes pods addressed by service names. Cities are physical sites, each on its own Incus network and DNS zone. A charger has no address of its own and is reached through its station's IP. The IP locates the station; the hier-id and certificate locate the charger (interfaces §1.2). The registry stores logical identity and physical attributes (host, IP, network, edge, coordinates), which `/locate` returns.

### 3.4 Delegation
Each city operator is authoritative for `pt.<city>.*`, enforced by per-city bearer tokens: the token of city X may create, change, delete, quarantine and release only elements strictly below `pt.X`; the NOC (admin token) owns `pt`, the city nodes and `pt.central.*` (interfaces §1.3, §4.1). `/locate` returns the element, its subtree and its `operator` (city or `noc`). Edges register their own subtree with the city token, and Prometheus service discovery is registry-driven (interfaces §14.1), so a station registered by its operator is monitored without editing Prometheus. Wazuh active responses use separate responder tokens that can only isolate, never administer (interfaces §3.2, §4.1).

### 3.5 Transport and identity
In TLS cities the edge terminates wss on 443, requests a client certificate (`ssl_verify_client optional`), checks CA and CRL, and forwards the certificate DN, serial and verify result in headers nginx always overwrites (interfaces §11.1, §5.2). Before the WebSocket upgrade the CSMS applies ten ordered checks (interfaces §5.2): trusted proxy; well-formed id; known in registry; not quarantined; edge city equals charger city; certificate verified; certificate CN equals id; certificate serial equals registered serial; source IP equals registered station IP (logged, enforced only with `CSMS_ENFORCE_STATION_IP`); duplicate-id guard that pings an existing connection before refusing or replacing it. Each refusal increments `evcharge_csms_identity_rejections_total{reason}`. Aveiro is deliberately plain `ws://` on port 80 with empty certificate headers, to be migrated to mTLS in the hijacking demonstration. A per-connection token bucket in the CSMS limits message rates, since edge `limit_req` sees only handshakes (interfaces §5.5). mTLS proves key possession, not device location: a key copied to another host in the same city passes checks 5-8, hence the IP check and duplicate-id guard.

### 3.6 PKI and secrets
A single-level lab CA lives in its own container `lab-ca`. Keys are EC P-256; charger certificates have `CN=<hier-id>` and 397-day validity; a CRL (7-day) is pushed to the edges; the CA key never leaves the container and leaf keys are copied straight into the target instance (interfaces §12.1-12.3). Revocation is part of the hijacking response: registry quarantine, CSMS disconnect, then certificate revocation (interfaces §12.5). Secrets have no defaults (services refuse to start without them), Postgres has one role per service, active responses use isolate-only tokens, and secret files are gitignored with an example template (interfaces §3, §8.1).

### 3.7 Data and APIs
Registry, CSMS, billing, fraud and correlator each own their tables and an authenticated HTTP API (interfaces §4, §8). Billing holds cards, tariffs, sessions and costs, covering FCAPS Accounting. The correlator keeps open incidents in memory and closed history in SQLite in the pod, and uses the registry `/locate` to place an incident on a charger, station, city or edge. Full contract: docs/interfaces.md.

## 4 Risk management and mitigation

### 4.1 Attack surfaces
Table 4.1 (columns: Surface | Attack | Defence built | Contract):
- OCPP WebSocket | identity spoofing, id reuse, floods | mTLS with CN = hier-id and serial check; ten handshake checks; duplicate-id guard; per-connection rate limit; edge limit_req | §5.2, §5.5, §11
- Plain ws legacy city (Aveiro) | MitM, rewriting meter values | migration to mTLS; physics-based detection of inconsistent meter values | §11.1, §15.4
- RFID card path | cloned card across cities | fraud service (impossible travel) + Wazuh fraud rules; block via rule 110210 (honours dry-run) | §4.3, §4.4, §15.5
- Meter/accounting path | under-reported energy (scale, rollback, freeze) | power_gap vs feeder meter (real time); cumulative grid_gap is warning only; meter-anomaly alert | §4.4, §14.3
- Station host | firmware compromise, C2, synchronised load | Wazuh network watch of new outbound connections + cross-station correlation; city load alert vs baseline; quarantine-charger | §15.4, §15.5, §14.7
- Registry delegation | token abuse | per-city subtree tokens; isolate-only responder tokens | §1.3, §4.1
- City to central network | station reaching NodePorts, peers, other cities | Incus network ACLs, egress allow list | §14.8, §16

### 4.2 Attack-defense tree
Figure C has one root goal: "Disrupt or defraud the regional EV charging network." Three attacker sub-goals form the branches.
1. Hijack a charger identity or falsify its signals. Leaves: reuse an existing charge-point id from a rogue device; present a stolen client certificate from another host; man-in-the-middle the plain-ws legacy city and rewrite MeterValues.
2. Defraud accounting. Leaves: use a cloned RFID card across cities (impossible travel); tamper the meter to under-report energy (scale, rollback, freeze).
3. Enlist chargers into a botnet. Leaves: beacon to an external C2; flood the CSMS with OCPP messages or handshakes; synchronise a full-power grid surge.

Green countermeasure nodes attach to the leaves they address: mTLS with CN = hier-id and serial check; the ten CSMS handshake checks and duplicate-id guard (composite 110131); station-IP enforcement; Aveiro plain-to-mTLS migration; per-connection rate limit and edge limit_req; Incus network ACLs (egress allow list); fraud-service rules (impossible travel; real-time power_gap vs feeder meter as the quarantine trigger, cumulative grid_gap warning-only) and the Wazuh fraud rules; block via 110210; quarantine-charger; netwatch C2 detection with cross-station correlation (110303/110304); the city-load alert for the synchronised surge; bulk quarantine; the correlator (one located incident); baselined thresholds. Each is annotated detecting or preventing, and standing (always on: rate limits, ACLs) or triggered (active responses). The tree is kept simple: root, three branches, leaves, defence boxes.

> Figure C — Attack-defense tree. To be drawn by the author in Excalidraw following the description above.

### 4.3 Large-scale monitoring design
Two tiers (interfaces §14). A city Prometheus runs on each edge, scrapes its stations and edge exporters every 5 s, sees every charger, and computes `city:*` recording rules. The central Prometheus federates only `{__name__=~"city:.*"}` every 30 s, so per-charger series never cross the inter-site link (§14.1, §14.2). When an edge dies the central side also loses that city's federation, which is the `EdgeDown` condition. Alertmanager inhibition suppresses a city's dependent alerts while its `EdgeDown` is active, so a dead edge raises one alert instead of one per station and charger (§14.4).

In the Phase 4 run (evidence/phase4/README.md): stopping the simulator on one station turned its map marker grey after 6 s (worst case about 15 s, the CSMS scrape interval), raised `StationDown` after 83 s and `ChargerOffline` after 130 s. Stopping the Aveiro edge raised `EdgeDown{city="aveiro"}` after 101 s; the Aveiro `ChargerOffline` alerts (131 s) were shown as suppressed, inhibited by `EdgeDown`; all 30 chargers reconnected within about 2 min of the edge restart. Locating `StationDown` for `pt.lisboa.st-003` via registry `/locate` took 0.013 s (host, IP, network, edge, DNS name, coordinates). City-level alerts locate to the city subtree; the correlator narrows them by joining them with Wazuh and fraud signals sharing a hier-id, card or city key within a 120 s window plus 3 s skew tolerance (section 6.4). Alertmanager inhibition keeps dependent alerts away from the correlator as well: with `EdgeDown` active, `ChargerOffline` alerts are suppressed before the webhook.

> Figure D — Geomap: 30 stations in three green clusters; optionally grey-marker and red-ring views. (To be re-shot by the author from the evcharge-map dashboard with the basemap; capture steps in evidence/phase4/README.md.)

### 4.4 Baseline and thresholds
Thresholds are computed (interfaces §14.7). Per city we record load (`city:grid_power_watts:sum`), OCPP message rate and the 10-minute failed card authorisations in the central Prometheus, and set upper = mean + 3 sigma from one-minute samples, capping load at 90% of installed capacity. The simulator load is stationary, so hour-of-day bands would add nothing. The window actually used is 12 hours (2026-11-05T23:01Z to 2026-11-06T11:01Z, 721 samples per series, basis `12h-20261106`): above the contract minimum of 6 h, below the 24 h target, so the thresholds are a 12 h baseline. The 90% cap did not apply in any city.

Table 4.4 (upper bounds; source monitoring/rules/baseline.yml):
- Porto: grid 288,519 W; msgs/s 6.43; failed auth/10 min 0.55
- Lisboa: grid 245,403 W; msgs/s 1.30; failed auth/10 min 2.82
- Aveiro: grid 251,375 W; msgs/s 1.33; failed auth/10 min 0.00

The Porto message-rate bound is wide (mean 0.95, sigma 1.83) because that series is more variable in this window. The Phase 4 README records the thresholds as provisional at the time of the run. The cross-agent frequency rules (110304, 110308) use Wazuh `global_frequency`, because per-agent counting missed the fleet in the first live botnet run (section 5.3).

### 4.5 Bandwidth of per-site monitoring
We compared federation only (window A) with direct scraping of every station by the central Prometheus (window B), 30 minutes each, same load, 15 stations (interfaces §14.6; evidence/phase4/README.md, bandwidth.txt). Federation reduced traffic by 99.3% uncompressed (47.2 KB/min vs 6.38 MB/min total) and 99.5% with gzip (4.4 KB/min vs 963 KB/min). Per city: about 15.5 KB/min federated vs about 2.1 MB/min direct. Samples fell from 55,427 to 324 per minute. On the central side, direct scraping cost more: Prometheus RSS +12% and CPU 5x.

Table 4.5 / Figure E (Federated A | Direct B | Saving):
- Bytes/min uncompressed: 47,234 | 6,383,567 | 99.3%
- Bytes/min gzip: 4,372 | 962,688 | 99.5%
- Samples/min: 324 | 55,427 | 99.4%
- Series per scrape per city: 54 | about 1,540
- Central Prometheus RSS (30 min avg): 131.8 MiB | 148.1 MiB | direct +12%
- Central Prometheus CPU (cores): 0.007 | 0.037 | direct 5x

Scaling: direct grows with chargers and stations (every station target at 5 s); federation grows only with `city:*` series (about 54 per city), independent of station count. TCP/HTTP overhead is ignored and the measurement was at 10 chargers per city, so the scaling statement is an extrapolation of the mechanism, tested at larger sizes in section 6 where the host permits.

### 4.6 Network containment
City networks are restricted with Incus network ACLs applied by infra/08_acls.sh (interfaces §16). Egress allow list per role: a station reaches only its own edge (443, or 80 in a plain city) and the Wazuh manager; an edge reaches the K3s nodes (30900, 30801) and Wazuh; the central Prometheus reaches the edges on 9090, 9100, 9113 (flows of §14.8). Stations cannot reach NodePorts, other cities, other stations, the CA or central monitoring NodePorts. ACLs are Incus-native, so they behave the same on the laptop and the Debian VM, and the script applies and removes them so the hijacking demo shows a rogue station's bypass attempt before (reaches the NodePort, refused by the CSMS at `ident.untrusted_peer`) and after (stopped at the network). ACLs are L3/L4 only: a fully controlled station can still abuse its legitimate OCPP path, which identity checks and rate limits address. Before/after evidence is in section 5.

## 5 Approach to the three threats

Each threat follows the same pattern: define it, say why it matters here, show how it is exploited, list the detection and mitigation signals, then the action responses, and finally a post-mortem of the proof-of-concept (PoC) runs. All runs are lab-only, time-boxed and recorded under `evidence/<threat>/`. Alert ids are Wazuh rule ids unless stated. All three threats were driven against the live platform (30 chargers; Aveiro plain `ws://`, Porto and Lisboa mTLS).

### 5.1 Hijacking

**Threat.** An attacker takes over a charger's identity or tampers with the signals it sends: reusing a charge-point id from a rogue device, presenting a stolen client certificate from another host, or sitting on the plain-ws link and rewriting `MeterValues` (MITRE T1036, T0849, T1557, T1565.002).

**Why it matters here.** The hier-id is the key for authorisation, billing and location (section 3.2). A device that holds `pt.porto.st-001.cp-1` can book energy to it, shadow the real charger and poison the operator's view of the grid. mTLS proves possession of a key, not location (section 3.5), and Aveiro is deliberately unprotected.

**Exploitation (three scenarios, `poc/hijack/`).**
1. Rogue duplicate id (Porto): a container on the city network holding a copy of the certificate of `pt.porto.st-001.cp-1` connects while the real station stays up.
2. Stolen certificate from another host (Porto): the same certificate is used after the real connection is stopped, to isolate identity check 9 (source IP) from the duplicate-id guard.
3. Routed man-in-the-middle (Aveiro): an interceptor that is the station's only gateway rewrites `Energy.Active.Import.Register` by a factor of 5 in live frames.

**Monitoring and mitigation metrics.** The CSMS logs `ident.ip_mismatch` (110104) and `charger.duplicate_id` (109100); a correlation window raises composite **110131** ("a second host holds the identity", level 12). `evcharge_csms_identity_rejections_total{reason}` counts refusals, and the Prometheus `DuplicateChargerID` alert is city-level. Physics checks on meter values (`over_energy`, `stop_mismatch`) feed **meter.anomaly 110116**. The CSMS never sees the man-in-the-middle directly; it infers it from impossible readings.

**Action responses.** Wazuh's active response on the CSMS runs `remote-stop`, `change-availability` and `disconnect`, then registry `quarantine-charger` on the id. Three standing mitigations were applied: (1) `CSMS_ENFORCE_STATION_IP=true`, which turns the stolen-certificate case from a logged warning into a 403 at the handshake; (2) default-drop city network ACLs, which close the direct-NodePort shortcut used by the rogue container; (3) Aveiro migrated from plain ws to mTLS. The response chain ends with revocation: registry quarantine, CSMS disconnect, certificate revocation with a pushed CRL and re-issue (section 3.6).

**PoC post-mortem.**

| Scenario | Before | After |
|---|---|---|
| 1 Rogue duplicate id | `ip_mismatch` 110104, then 110131; `remote-stop` rejected (nothing charging), `change-availability` and `disconnect` accepted, then quarantine | Quarantine applied |
| 2 Stolen certificate | Rogue accepted from a foreign address, `ip_mismatch` logged only | Same rogue refused 403; two attempts, no `conn.open` for the rogue's address |
| 3 MitM on Aveiro | 46 rewritten `MeterValues`; `meter.anomaly` 110116 fired 48 times across both chargers | Defeated by two independent layers (ACL egress denial; mTLS certificate pinning) |

What the runs exposed:
- Quarantine is per charger id, so the genuine station is locked out together with the thief (two 403 lines for the real station's own address in scenario 1) until an operator releases it. This is a design choice, observed and not hidden.
- The ACLs stop routed traffic but not same-bridge traffic between two stations in one city, because the host lacks `br_netfilter` (loading it would affect every bridge, including K3s's). The ACL file contains no peer-to-peer allow rule, but that does not prove the traffic is blocked on the wire, and here it is not.
- Because both the ACL and mTLS defeat scenario 3, the demonstration could not isolate "mTLS alone". mTLS pinning is confirmed from the `mitmproxy` and `evsim` sources, not re-staged live (that would mean turning a mitigation off on purpose). Aveiro was then returned to plain ws so the final state still shows the legacy city; the one-time proof stands.
- Station-IP enforcement needs accurate registry data. Certificate re-issue changes the serial, so the registry `cert_serial` must be updated too (check 8), or the new legitimate certificate is refused like the compromised one. The health gate caught this during the revocation exercise.
- The K3s VMs drifted about 2 h 34 min behind the host mid-session, so the log timestamps of the scenario-2 "after" capture are on a skewed clock (noted next to the files).

### 5.2 Fraud

**Threat.** Fraud against accounting: a cloned RFID card used in two cities, and a tampered meter that under-reports energy (scale, rollback or freeze).

**Why it matters here.** Billing is what the operator is paid by (FCAPS Accounting). A cloned card is billed to an innocent holder, and under-reporting is invisible to the CSMS because the readings stay internally consistent. Only an independent reference, the feeder meter, exposes it.

**Exploitation.** `poc/fraud/clone_card.sh` presents card `E3EE5BE2ED4120` (home city Porto) at `pt.aveiro.st-001` and `pt.lisboa.st-001`. `poc/fraud/tamper_meter.sh` makes a station apply rollback (a fixed 500 Wh once per session) or a scale factor (0.5, then 0.1) to its meter values.

**Monitoring and mitigation metrics.** Two detectors run side by side on purpose. The fraud service has `impossible_travel` (distance and implied speed against `FRAUD_MAX_KMH`=200), `meter_backwards`, `peer_deviation`, `grid_gap` and `power_gap`. Wazuh has rules 110200-110209 on CSMS and fraud logs. Prometheus has `city:station_power_gap_watts` (feeder minus reported power) and the alert `MeterUnderReporting` (> 1000 W for 5 min).

**Action responses.** `block-card` (fraud service, then Wazuh active response, idempotent), flagging of the card's recent sessions, and `quarantine-charger` with `change-availability` and `disconnect` for meter tampering. Unblocking is operator-only; the responders cannot do it.

**PoC post-mortem.**
- Cloned card, before (dry-run): `impossible_travel` at 975 km/h (274 km in 16.9 min). Two cloned sessions (2136 Wh and 1953 Wh) completed and were billed with `flag_reason null`; the card stayed active.
- Cloned card, after (enforcing): `impossible_travel` at 1312 km/h (218 km in 10 min) was detected 23 ms after the second-city `session.start`. The fraud service blocked the card at +58 ms; Wazuh 110207 plus `block-card` (110410) found it already blocked at +133 ms; five sessions were flagged by 110209 within 0.8 s; later `/authorize` returned `Blocked` and the CSMS issued `auth.reject`. Collateral: the real holder is blocked too and recovery is manual.
- Meter rollback: three `meter.anomaly backwards`, three `meter_backwards` alerts, 110204/110208/110209, three sessions flagged, about 2 min each. 110205 (three anomalies on one charger in 600 s) did not fire because the events split over two chargers; it is not claimed.
- Meter scale: the feeder gap reached about 27.8 kW and up to 34 kW and `MeterUnderReporting` fired 23 min after the tamper began, while the CSMS raised nothing. The cumulative `grid_gap` rule needed run 2 at factor 0.1 and fired 33 min into it (76 min after the first tamper), then quarantined both chargers within 0.15 s (110405). That latency is structural: energy is booked at session stop (settle 1400 s), the gap must have held 600 s, and the ratio is a 6 h cumulative one diluted by honest history.

What the runs exposed, and what was changed:
- **Honest-card false positives.** The simulator drew 20 % of sessions on a card of another city and had no travel time, so over 2.8 h there were 26 `impossible_travel` alerts, 24 on honest cards, and 10 distinct honest cards were blocked in one hour. Wazuh was blocking even in dry-run, because rule 110207 read the `fraud.alert` line, which is written in dry-run too.
- **Hardening.** Alerts now carry `dry_run` and `auto_action`; the block path is a single rule, **110210**, which requires `auto_action=block_card` and `dry_run=false`. Auto-block requires overlapping sessions in two cities or a speed of at least `FRAUD_BLOCK_KMH`=400; alerts still fire above 200 km/h. 110203 and 110206 became alert-only. The simulator now draws honest cards city-local, so honest cross-city reuse no longer occurs. This was verified under watch (16:58 to 17:11) with no block or quarantine on honest targets, but residual alert-only hits from old history still appeared.
- **`grid_gap` false quarantines.** `pt.porto.st-002` was quarantined twice (79 % and 71 %). The honest reported-to-feeder ratio spans 0.71 to 1.38 across 15 stations, and the tampered station ended at 0.71, so a 0.8 cumulative limit cannot separate a 10 % tamper from honest variance. `grid_gap` was demoted to a warning; the quarantine trigger is now the real-time `power_gap` (at least 1000 W and 30 % of feeder power in 80 % of at least 10 samples over 15 min), which is exactly 0 on honest stations. The `power_gap` change and the quarantine-flags-sessions step were covered by unit tests and the earlier captures, **not re-attacked live**.
- The billing flag after quarantine was promised in the design but not implemented at the time of the run; it was added afterwards.
- Residual: the rate guard on `block-card` (10 cards per hour) was exhausted by honest false positives, so the fraud service's own block caught the real clone. `power_gap` trusts the feeder meter and the reported-power metric and can miss a very short tamper. A slow clone (sequential, under 400 km/h) is alerted but not blocked. Enforcing mode (`FRAUD_DRY_RUN=false`) was switched back to `true` at the end of the runs; it should be re-enabled only after a supervised re-demo.

### 5.3 Botnet

**Threat.** A fleet of compromised chargers beacons to a command-and-control (C2) host and, on command, floods the CSMS with OCPP messages and starts full-power sessions at the same moment.

**Why it matters here.** Synchronised load is a grid-stability issue (section 1.2) and a flood is an availability attack on the CSMS. The attack is distributed across stations and cities, so single-charger rules see only fragments.

**Exploitation.** Six stations (`st-001` and `st-002` in each of the three cities) beacon every 15 s to the lab C2 `c2.incus:8099`. The schedule is idle 30 s, flood 60 s at 20 Hz, surge 120 s, stop 30 s. For the run the city egress ACLs were removed so the beacon could land; with ACLs applied the same beacon is dropped at egress, an earlier but weaker signal. They were re-applied afterwards.

**Monitoring and mitigation metrics.** Wazuh netwatch: 110303 (new outbound connection to the C2, per station), 110305 (established-connection diff), 110304 (the same C2 contacted from several stations), 110300 and 110301 (OCPP rate flood and token-bucket kick). Prometheus: `MsgRateAnomaly`, `ChargerMsgFlood`, and the city load `city:grid_power_watts:sum` against the baseline of section 4.4 (alert `CityLoadSpike`, 110308).

**Action responses.** Bulk registry quarantine of the compromised stations, which makes the CSMS set them Inoperative and disconnect them; each action is audited as 110405.

**PoC post-mortem.** Fired: 110303 x6, 110305 x6, 110300 x6, 110301 x2 and 110405 x6 (one quarantine per station); Prometheus `MsgRateAnomaly` and `ChargerMsgFlood` in Aveiro and Lisboa. The surge peaked at 207 kW (Lisboa), 166 kW (Aveiro) and 156 kW (Porto), 5 to 8 times the pre-attack baseline.

Three findings from the live run:
1. **110304 did not fire** despite six 110303 beacons to one C2. This was the main detection gap: the fleet-level signal was missing. The cause is that Wazuh frequency rules count per agent, while the six stations are six different agents. The fix applies `global_frequency` to the cross-agent frequency rules (including 110304 and 110308). It is **proven in `wazuh-logtest`** but **not yet re-proven in a live two-node run**, so sections 6 and 7 treat it as designed and unit-proven, not live-proven.
2. **110308 (`CityLoadSpike`) did not reach firing** because the quarantine cut the surge before the alert's sustained-dwell window elapsed. The response beat the alert; the per-charger flood signals carried the detection. The same Wazuh per-agent limitation applies to the cross-agent rules, so the fix covers it too.
3. **Collateral and its instrument gap.** Bulk quarantine forced **21 CSMS session stops (counter 85 to 106)** and drove Porto's charging power to 0 W, so every driver mid-session on a quarantined station pays for a per-charger compromise. The simulator's `interrupted` counter does not count CSMS-forced stops (it logs them as normal), so the valid collateral measures are the central CSMS stopped-session delta and the power collapse, not the simulator counter. This is the most disruptive response in the project.

## 6 Evaluation of the approaches and scalability

### 6.1 Method
The scalability test ran on one laptop (23 GiB RAM, 8 cores, about 8 GiB available) with Wazuh, K3s, the three city sites and all services running (evidence/eval/README.md). The cheapest valid lever was to add OCPP connections and message rate on the plain-ws Aveiro edge with a synthetic generator (`eval/ocpp_load.py`), placed as one `systemd-run` unit per Aveiro station so traffic follows the real path (station, nginx edge, CSMS, registry, Postgres). More real chargers were rejected because TLS cities need a per-charger certificate and re-using the same 20 cards would have triggered the fraud service. The generator sends BootNotification, StatusNotification and then Heartbeat every `hb` seconds; it starts no transaction. Each point is a 120 s window after settling. Latency is `evcharge_csms_ocpp_handle_seconds`, events per second (EPS) is the delta of Wazuh `events_processed`, and resources come from `kubectl top` and cgroups.

### 6.2 Results

Table 6.2 (N = connected chargers, 30 real plus synthetic):

| Point | N | Heartbeat | CSMS msg/s | p50 / p95 / p99 (ms) | Wazuh EPS | Prometheus head series | CSMS replicas (HPA CPU) |
|---|---|---|---|---|---|---|---|
| Baseline | 30 | real | 2.0 | 3.8 / 23.0 / 42.4 | 4.9 | 4500 | 2 (15 %) |
| 1 | 50 | 5 s | 6.8 | 2.6 / 9.5 / 22.0 | 6.8 | 4502 | 2 (23 %) |
| 2 | 80 | 5 s | 12.7 | 2.4 / 8.7 / 19.4 | 7.3 | 4502 | 2 (28 %) |
| 3 | 140 | 5 s | 24.0 | 2.2 / 8.2 / 13.6 | 5.2 | 4463 | 3 (34 %) |
| 4 | 140 | 1 s | 111 | 2.1 / 9.0 / 19.9 | 5.5 | 4548 | 4 (70 %) |
| 5 | 140 | 0.3 s | 169 handled (363 inbound) | 1.4 / 9.4 / 24.2 | 238 | 4622 | 4 (160 %) |

Latency did not degrade with load: p95 stayed at 8 to 9 ms and p99 reached 24 ms at the top point. Higher points are below the baseline p95 because the baseline includes real sessions with billing and fraud calls, which the generator does not make. Prometheus head series stayed at about 4500 to 4600, because monitoring is per station and per city: the federated series per city stayed at 115 to 172, with federation scrape duration of 1 to 11 ms. Series grow with station count (about 6 per station exporter), which this test did not vary.

### 6.3 Ceilings
Every stopping reason was a configuration limit, not host RAM (available memory never fell below 7.9 GiB):
1. **Connections.** The edge `limit_conn` is 24 per source address, so one station container carries at most 24 chargers (22 synthetic plus 2 real). Five stations gave 120 connections in Aveiro (140 in total). More needs more source addresses or a higher limit. Handshake `limit_req` (2 r/s, burst 20) is why the generator staggers connects.
2. **Message rate.** The per-charger rate limiter allows 1 msg/s (burst 20). At a 1 s heartbeat every charger sits at the limit and the HPA is at its maximum of 4 replicas (CPU 70 % of the 100 m request). At 0.3 s the limiter engages (`evcharge_csms_rate_limited_total` about 198/s against 169 msg/s handled) and the four replicas reach 160 % of request. The component that saturated is the CSMS tier at `maxReplicas=4`, protected by the limiter; throttled calls are answered cheaply, which is why latency stayed flat. Handled throughput is therefore about 110 msg/s sustained and about 170 msg/s peak.
3. **Wazuh.** Because rate-limit refusals are logged, EPS rose about 45 times (5 to 238) and alerts written went from 58 to 2160 in two minutes. `events_dropped` stayed 0 and analysisd stayed at about 1.4 GiB, so there was headroom; alert volume, not memory, would hurt first. An abusive client is loud by design, which helps detection and costs the manager.
4. **Prometheus** is not a limit (above).

At the real behaviour of 0.07 msg/s per charger (30 chargers produce 2.0 msg/s), 110 msg/s corresponds to roughly 1,500 real chargers. This is an extrapolation of one measurement, not a measured scale.

### 6.4 Detection quality
- **Hijacking.** 110104 then 110131 fired on the rogue duplicate; the stolen-certificate case moved from logged to refused; the MitM was inferred through 48 `meter.anomaly` alerts. No false positives were recorded for these rules in the runs. Missed in the sense of not prevented: same-bridge peer traffic (section 5.1).
- **Fraud.** The cloned card was detected in 23 ms and blocked in 58 ms. False positives were the main cost: 24 of 26 `impossible_travel` alerts on honest cards, and two `grid_gap` quarantines of `pt.porto.st-002`. After hardening, the signal that drives automatic action (110210, `power_gap`) is the high-confidence one. The rollback, the cloned card and the scale tamper were each caught, but the cumulative `grid_gap` caught the scale tamper only after 76 min; the instantaneous `MeterUnderReporting` after 23 min.
- **Botnet.** Per-station 110303 and the flood rules fired; the fleet-level 110304 and `CityLoadSpike` did not, until the `global_frequency` fix, which is proven in logtest only.
- **Correlator** (evidence/correlator/README.md). Recorded alerts of the three runs were replayed with their original spacing (Alertmanager, Wazuh and fraud posted to the same `/signals/*` endpoints). One incident per root cause resulted: the hijack incident (110104, 110131, `registry.quarantine`) was located to the charger `pt.porto.st-001.cp-1` in **3.55 s**; the fraud incident (`impossible_travel`, 110210, `billing.card_blocked`) to the card in **0.10 s**; the botnet incidents to their city (Lisboa 1.16 s, Aveiro 0.75 s, Porto 1.12 s). Mitigation followed in 1.65 s (hijack) and 0.07 s (fraud). The one infra incident (`EdgeDown`, 10.05 s, dominated by the Alertmanager `group_wait`) was the single incident of a live edge stop. De-duplication worked: three reposts of one 110104 alert left the incident count unchanged (signal `count` 1 to 4). Inhibition worked: with `aveiro-edge` stopped for about 3 min, 5 `ChargerOffline` alerts were suppressed by `EdgeDown` and never reached the correlator; 10 chargers dropped and 30/30 reconnected within about 30 s of the restart. The correlator metric `evcharge_correlator_time_to_locate_seconds` holds 7 observations; its p90 bucket of 300 s comes from one live `AuthFailureBurst`, which Prometheus reports 45 s after `startsAt`.

### 6.5 Limits of the method
- **One laptop.** The ceilings above are the host-configured limits of a single machine with shared CPU and RAM, not of the architecture. The generator and the system under test share cores, so absolute numbers cannot be transferred to a deployment.
- **Synthetic load.** The generator sends heartbeats only: no sessions, meter values, billing or fraud calls. Real traffic costs more CPU per message. Billing and fraud were measured at baseline only (dependency p95: billing 43 ms, fraud 91 ms). TLS handshake cost is not covered because only Aveiro was scaled.
- **Single run per point.** One 120 s window per load point, no repetition and no confidence intervals. Some 502/504 responses appeared in the edge log at the last point while replicas changed and were not analysed. `AuthFailureBurst` alerts for Porto and Lisboa were active and unrelated.
- **Replay versus live.** The correlator results come from recorded alerts replayed with shifted timestamps, not from fresh attacks, and only the `EdgeDown` and `AuthFailureBurst` signals arrived live. The locate time uses the correlator's clock, so a skew of up to 2 s can appear as a small negative offset (mitigated before located in the hijack incident). Live Wazuh forwarding after the correlator went up was not exercised.
- **Correlator heuristics.** A response arriving more than window plus skew (123 s) after the nearest incident member is dropped (the Porto botnet quarantine at 158 s). A fleet across cities is three incidents, since the city is the shared key and 110304 carries only one station. Open incidents are in memory and are lost on a pod restart. Rule 110300 (level 6) is below the forwarding threshold of the Wazuh integration (level 7 plus selected response ids), so the flood alerts do not reach the correlator while 110301 does.

## 7 Conclusions

### 7.1 Main findings
- One naming scheme across certificate, OCPP path, registry, metrics and logs makes every alert locatable by a single registry lookup, and this is what let the correlator join Prometheus, Wazuh and fraud signals into one incident per root cause with locate times of 0.1 to 3.6 s (replayed).
- Hijacking is cheap to prevent and hard to eradicate cleanly: mTLS plus station-IP enforcement and network ACLs stop the stolen-certificate and routed MitM cases, while duplicate-id detection (110131) and quarantine contain the rogue; identity correctness depends on the registry staying accurate.
- Fraud detection needs an independent reference. The CSMS cannot see an internally consistent under-reporting meter; the feeder-power gap can. Detecting sensitively and acting automatically only on high-confidence signals (110210, `power_gap`) removed the false blocks found in the live runs.
- Federated monitoring scales with the number of cities, not chargers, and the measured ceilings (edge `limit_conn`, HPA maximum, per-charger limiter) are configuration choices that protected the platform: latency stayed flat while the rate limiter absorbed excess load.

### 7.2 Why the solution is not perfect
- **Per-agent frequency blind spot, found late.** The cross-station botnet rules (110304, 110308) did not fire in the live run because Wazuh counts per agent. The `global_frequency` fix is proven in logtest only; the live two-node re-run was not done. Likewise the correlation of all three threats is replay-proven, not live-proven.
- **Bulk-quarantine collateral.** Quarantining six stations cost 21 forced session stops and zeroed Porto's charging power; quarantining a hijacked identity also locks out the genuine station; blocking a cloned card also blocks the real holder. All three recoveries are manual. Our instrument for the botnet collateral had a gap (the simulator counter) that had to be replaced by CSMS counters.
- **Fraud tuning.** The first thresholds produced 24 false `impossible_travel` alerts and two false quarantines; the fix (city-local honest cards, 110210, `power_gap`) was verified on normal traffic only and enforcing mode was left off. A slow clone or a short tamper remains uncaught, and the 400 km/h margin is tuned to a simulated data set.
- **Signals below the forwarding level.** Rule 110300 (level 6) never reaches the correlator; response signals were initially dropped for the same reason until a second integration block was added.
- **Containment gaps.** ACLs do not stop same-bridge peer traffic on this host (no `br_netfilter`); station-IP enforcement depends on registry accuracy.
- **Laptop ceiling and synthetic load.** The 140-charger, 170 msg/s result is the host and configuration of a single laptop and a heartbeat-only generator, not a validated capacity; the 1,500-charger figure is an extrapolation.
- **Correlator limits.** City-level join keys split a cross-city fleet into three incidents; open incidents are in memory.

### 7.3 Future work
- Re-run the botnet on two live Wazuh nodes with `global_frequency` and show 110304 and 110308 firing, and a live (non-replay) correlator run with Wazuh forwarding confirmed.
- Replace bulk quarantine with graduated responses: rate-limit or isolate the flood first, quarantine only after a second signal, and drain sessions before disconnecting.
- Persist open correlator incidents, add a cross-city join key for the fleet (the C2 address), and lower the forwarding threshold or add selected ids so 110300 reaches the correlator.
- Use `power_gap` with a per-station baseline and noise margins on real meters; add a per-card trip schedule with in-transit gaps; add a hold-before-block step for the card holder.
- Load `br_netfilter` (or an equivalent L2 filter) to close the same-bridge gap, and move station-IP enforcement to an automatic registry update path.
- Repeat the scalability test on dedicated hosts with an external load generator, a higher HPA maximum, Postgres pooling, multiple Wazuh managers, TLS cities scaled with certificate minting, and real sessions in the mix.

## References
1. Open Charge Alliance. OCPP 1.6 (JSON). 2015.
2. Open Charge Alliance. OCPP 2.0.1. 2020 (Security Profiles).
3. Open Charge Alliance. OCPP 1.6 Security Whitepaper.
4. ISO 15118, Road vehicles - Vehicle to grid communication interface.
5. MITRE. ATT&CK Enterprise and ATT&CK for ICS. attack.mitre.org. [verify: technique ids T1036, T0849, T1557, T1565.002 against the ATT&CK version cited.]
6. Prometheus documentation. prometheus.io.
7. Wazuh documentation. documentation.wazuh.com. [verify: the rule option `global_frequency` under ruleset/ruleset-xml-syntax, and the custom-integration mechanism, against the Wazuh version used.]
8. Zabbix and Datadog documentation.
9. Falco (CNCF), Splunk Enterprise Security, OSSEC documentation.
10. ITU-T M.3400 (FCAPS).
11. B. Schneier, "Attack trees", Dr. Dobb's Journal, 1999.
12. B. Kordy, S. Mauw, S. Radomirovic, P. Schweitzer, "Foundations of attack-defense trees", FAST 2010, LNCS 6561.
13. This project: docs/interfaces.md and evidence/phase4/.
