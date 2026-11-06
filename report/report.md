# Threat detection, monitoring and prevention in a regional EV charging network

Gonçalo Almeida — Security in Large-Scale Systems (SSLE), University of Aveiro — Project 1 — November 2026

## 1 Introduction

### 1.1 The system
We built and secured a regional operator's EV charging network. Chargers speak OCPP 1.6J (JSON over WebSocket) to a central charging-station management system (CSMS) on K3s. The chargers sit in three cities (Porto, Lisboa, Aveiro), each on its own Incus network behind an nginx edge. Around the CSMS run a registry (inventory and delegation), billing, fraud scoring and a correlator. Prometheus and Alertmanager provide metrics and alerting; Wazuh provides host and log detection and active response. All components share one hierarchical naming scheme, `pt.<city>.<station>.<charger>` (interfaces §1).

### 1.2 Why it matters
A charging network is critical infrastructure on three counts. It draws load from the grid, so coordinated behaviour by many chargers is a grid-stability issue. It handles money, so metering and card authorisation must be trustworthy. Drivers depend on its availability. In the syllabus it is a large, geographically distributed system whose monitoring cost, not only its security, has to scale.

### 1.3 Scope: three threats
All three are on the course threat list: (i) hijacking of a charger identity or its signals, (ii) fraud against accounting (cloned cards, tampered meters), (iii) a botnet of compromised chargers (C2, floods, synchronised load). Section 5 treats each.

### 1.4 Contributions
- One identity end to end: the same string is the certificate name, OCPP path, registry key, Prometheus/Wazuh labels and map label.
- Correlation to one located incident: Prometheus, Wazuh and fraud-service signals are joined by a correlator and located via the registry.
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
The central platform runs on K3s (two nodes in Incus VMs): CSMS with 2 to 4 replicas under a horizontal autoscaler, registry, billing, fraud, correlator, a PostgreSQL StatefulSet with a persistent volume, central Prometheus, Alertmanager and Grafana (interfaces §2.2). Wazuh runs in its own container with agents on edges and stations. Each city is an Incus network containing an nginx edge, a city Prometheus on that edge and simulated stations (Figure A).

> Figure A — Architecture. Central K3s, Wazuh, three city networks (edge, city Prometheus, stations), attacker containers, federation link, wss+mTLS links; caption states the physical/logical split. [source: ASCII diagram in .ai/HANDOFF.md §3, to be redrawn by the author]

### 3.2 Hierarchical naming
Identifiers are `pt.<city>.<station>.<charger>` (e.g. `pt.porto.st-003.cp-2`); cities are `pt.<city>`, edges `pt.<city>.edge`, central nodes `pt.central.<node>` (interfaces §1.1). The same string is used everywhere (interfaces §1.2): certificate CN, OCPP URL path `/ocpp/<hier-id>`, registry key, Prometheus labels (`hier_id`, `city`, `station`, `charger`), Wazuh agent name or log field, DNS name and certificate SAN (`st-003.porto.pt.lab`), Incus key `user.hier-id`, and the map label. An alert carrying a `hier_id` is therefore located by one registry lookup, and prefix matching on dot boundaries yields a city or station subtree.

> Figure B — Naming scheme. The hier-id tree and the same string in certificate, OCPP path, registry, Prometheus, Wazuh, DNS. [source: table in interfaces §1.2; draw or typeset]

### 3.3 Physical versus logical naming
Central services are logical: Kubernetes pods addressed by service names. Cities are physical sites, each on its own Incus network and DNS zone. A charger has no address of its own and is reached through its station's IP. The IP locates the station; the hier-id and certificate locate the charger (interfaces §1.2). The registry stores logical identity and physical attributes (host, IP, network, edge, coordinates), which `/locate` returns.

### 3.4 Delegation
Each city operator is authoritative for `pt.<city>.*`, enforced by per-city bearer tokens: the token of city X may create, change, delete, quarantine and release only elements strictly below `pt.X`; the NOC (admin token) owns `pt`, the city nodes and `pt.central.*` (interfaces §1.3, §4.1). `/locate` returns the element, its subtree and its `operator` (city or `noc`). Edges register their own subtree with the city token, and Prometheus service discovery is registry-driven (interfaces §14.1), so a station registered by its operator is monitored without editing Prometheus. Wazuh active responses use separate responder tokens that can only isolate, never administer (interfaces §3.2, §4.1).

### 3.5 Transport and identity
In TLS cities the edge terminates wss on 443, requests a client certificate (`ssl_verify_client optional`), checks CA and CRL, and forwards the certificate DN, serial and verify result in headers nginx always overwrites (interfaces §11.1, §5.2). Before the WebSocket upgrade the CSMS applies ten ordered checks (interfaces §5.2): trusted proxy; well-formed id; known in registry; not quarantined; edge city equals charger city; certificate verified; certificate CN equals id; certificate serial equals registered serial; source IP equals registered station IP (logged, enforced only with `CSMS_ENFORCE_STATION_IP`); duplicate-id guard that pings an existing connection before refusing or replacing it. Each refusal increments `evcharge_csms_identity_rejections_total{reason}`. Aveiro is deliberately plain `ws://` on port 80 with empty certificate headers, to be migrated to mTLS in the hijacking demonstration. A per-connection token bucket in the CSMS limits message rates, since edge `limit_req` sees only handshakes (interfaces §5.5). mTLS proves key possession, not device location: a key copied to another host in the same city passes checks 5-8, hence the IP check and duplicate-id guard.

### 3.6 PKI and secrets
A single-level lab CA lives in its own container `lab-ca`. Keys are EC P-256; charger certificates have `CN=<hier-id>` and 397-day validity; a CRL (7-day) is pushed to the edges; the CA key never leaves the container and leaf keys are copied straight into the target instance (interfaces §12.1-12.3). Revocation is part of the hijacking response: registry quarantine, CSMS disconnect, then certificate revocation (interfaces §12.5). Secrets have no defaults (services refuse to start without them), Postgres has one role per service, active responses use isolate-only tokens, and secret files are gitignored with an example template (interfaces §3, §8.1).

### 3.7 Data and APIs
Registry, CSMS, billing, fraud and correlator each own their tables and an authenticated HTTP API (interfaces §4, §8). Billing holds cards, tariffs, sessions and costs, covering FCAPS Accounting. Full contract: docs/interfaces.md.

## 4 Risk management and mitigation

### 4.1 Attack surfaces
Table 4.1 (columns: Surface | Attack | Defence built | Contract):
- OCPP WebSocket | identity spoofing, id reuse, floods | mTLS with CN = hier-id and serial check; ten handshake checks; duplicate-id guard; per-connection rate limit; edge limit_req | §5.2, §5.5, §11
- Plain ws legacy city (Aveiro) | MitM, rewriting meter values | migration to mTLS; physics-based detection of inconsistent meter values | §11.1, §15.4
- RFID card path | cloned card across cities | fraud service (impossible travel) + Wazuh fraud rules; block-card | §4.3, §4.4, §15.5
- Meter/accounting path | under-reported energy (scale, rollback, freeze) | grid_gap vs feeder meter; meter-anomaly alert | §4.4, §14.3
- Station host | firmware compromise, C2, synchronised load | Wazuh network watch of new outbound connections + cross-station correlation; city load alert vs baseline; quarantine-charger | §15.4, §15.5, §14.7
- Registry delegation | token abuse | per-city subtree tokens; isolate-only responder tokens | §1.3, §4.1
- City to central network | station reaching NodePorts, peers, other cities | Incus network ACLs, egress allow list | §14.8, §16

### 4.2 Attack-defense tree
Figure C has one root goal: "Disrupt or defraud the regional EV charging network." Three attacker sub-goals form the branches.
1. Hijack a charger identity or falsify its signals. Leaves: reuse an existing charge-point id from a rogue device; present a stolen client certificate from another host; man-in-the-middle the plain-ws legacy city and rewrite MeterValues.
2. Defraud accounting. Leaves: use a cloned RFID card across cities (impossible travel); tamper the meter to under-report energy (scale, rollback, freeze).
3. Enlist chargers into a botnet. Leaves: beacon to an external C2; flood the CSMS with OCPP messages or handshakes; synchronise a full-power grid surge.

Green countermeasure nodes attach to the leaves they address: mTLS with CN = hier-id and serial check; the ten CSMS handshake checks and duplicate-id guard; station-IP enforcement; Aveiro plain-to-mTLS migration; per-connection rate limit and edge limit_req; Incus network ACLs (egress allow list); fraud-service rules (impossible travel, grid_gap vs feeder meter) and the Wazuh fraud rules; block-card; quarantine-charger; netwatch C2 detection with cross-station correlation; the correlator (one located incident); baselined thresholds. Each is annotated detecting or preventing, and standing (always on: rate limits, ACLs) or triggered (active responses). The tree is kept simple: root, three branches, leaves, defence boxes.

> Figure C — Attack-defense tree. To be drawn by the author in Excalidraw following the description above. [source: this section]

### 4.3 Large-scale monitoring design
Two tiers (interfaces §14). A city Prometheus runs on each edge, scrapes its stations and edge exporters every 5 s, sees every charger, and computes `city:*` recording rules. The central Prometheus federates only `{__name__=~"city:.*"}` every 30 s, so per-charger series never cross the inter-site link (§14.1, §14.2). When an edge dies the central side also loses that city's federation, which is the `EdgeDown` condition. Alertmanager inhibition suppresses a city's dependent alerts while its `EdgeDown` is active, so a dead edge raises one alert instead of one per station and charger (§14.4).

In the Phase 4 run (evidence/phase4/README.md): stopping the simulator on one station turned its map marker grey after 6 s (worst case about 15 s, the CSMS scrape interval), raised `StationDown` after 83 s and `ChargerOffline` after 130 s. Stopping the Aveiro edge raised `EdgeDown{city="aveiro"}` after 101 s; the Aveiro `ChargerOffline` alerts (131 s) were shown as suppressed, inhibited by `EdgeDown`; all 30 chargers reconnected within about 2 min of the edge restart. Locating `StationDown` for `pt.lisboa.st-003` via registry `/locate` took 0.013 s (host, IP, network, edge, DNS name, coordinates). City-level alerts locate to the city subtree, not a station; narrowing them is the correlator's task (section 5).

> Figure D — Geomap: 30 stations in three green clusters; optionally grey-marker and red-ring views. [source: dashboard evcharge-map; capture steps in evidence/phase4/README.md; to be re-shot by the author with the basemap]

### 4.4 Baseline and thresholds
Thresholds are computed (interfaces §14.7). Per city we record load (`city:grid_power_watts:sum`), OCPP message rate and the 10-minute failed card authorisations in the central Prometheus, and set upper = mean + 3 sigma from one-minute samples, capping load at 90% of installed capacity. The simulator load is stationary, so hour-of-day bands would add nothing. The window actually used is 12 hours (2026-11-05T23:01Z to 2026-11-06T11:01Z, 721 samples per series, basis `12h-20261106`): above the contract minimum of 6 h, below the 24 h target, so the thresholds are a 12 h baseline. The 90% cap did not apply in any city.

Table 4.4 (upper bounds; source monitoring/rules/baseline.yml):
- Porto: grid 288,519 W; msgs/s 6.43; failed auth/10 min 0.55
- Lisboa: grid 245,403 W; msgs/s 1.30; failed auth/10 min 2.82
- Aveiro: grid 251,375 W; msgs/s 1.33; failed auth/10 min 0.00

The Porto message-rate bound is wide (mean 0.95, sigma 1.83) because that series is more variable in this window. The Phase 4 README records the thresholds as provisional at the time of the run.

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
*Pending the live threat runs (Phases 6 to 9).*
### 5.1 Hijacking
### 5.2 Fraud
### 5.3 Botnet

## 6 Evaluation of the approaches and scalability
*Pending the evaluation runs.*

## 7 Conclusions
*Pending the runs; to be completed after sections 5 and 6.* (TODO 7.2/7.3: actual scale reached and collateral-damage numbers not yet available.)

## References
1. Open Charge Alliance. OCPP 1.6 (JSON). 2015.
2. Open Charge Alliance. OCPP 2.0.1. 2020 (Security Profiles).
3. Open Charge Alliance. OCPP 1.6 Security Whitepaper.
4. ISO 15118, Road vehicles - Vehicle to grid communication interface.
5. MITRE. ATT&CK Enterprise and ATT&CK for ICS. attack.mitre.org.
6. Prometheus documentation. prometheus.io.
7. Wazuh documentation. documentation.wazuh.com.
8. Zabbix and Datadog documentation.
9. Falco (CNCF), Splunk Enterprise Security, OSSEC documentation.
10. ITU-T M.3400 (FCAPS).
11. B. Schneier, "Attack trees", Dr. Dobb's Journal, 1999.
12. B. Kordy, S. Mauw, S. Radomirovic, P. Schweitzer, "Foundations of attack-defense trees", FAST 2010, LNCS 6561.
13. This project: docs/interfaces.md and evidence/phase4/.
