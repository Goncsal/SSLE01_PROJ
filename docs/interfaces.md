# Interfaces and contracts

Contract version **6** (2026-11-06). This document is the single reference for every
name shared between components: identifiers, ports, environment variables, HTTP APIs,
the OCPP profile, log lines, metric names, database tables and the simulator.
The `common/` package (`evcommon`) implements the parts that are code.

Rules:
- A component implements its side of the contract exactly. If the contract is wrong or
  incomplete, the change is made here first, then in the code.
- Sections marked **stub** fix names and shapes now; their behaviour is finalised later
  (the phase is named).
- Everything is UTC. Timestamps are ISO 8601. Energy is in Wh (integers), power in W,
  rated power in kW.

## Changelog

Version 6 (2026-11-06), for Phases 6–9 (the three threats and the correlator):
- 4.4: the fraud service finalised: rules (impossible travel, energy > kW·t, meter
  backwards, peer deviation, grid vs reported, session rate), scoring, the actions it
  requests through the responder tokens, its metrics and env.
- 4.5: the correlator finalised: join keys and the ±`CORRELATOR_SKEW_S` clock tolerance,
  the root-cause walk over registry topology, the `Incident` schema and the
  `t_first_signal`/`t_located`/`t_mitigated` timeline, idempotency, de-duplication.
- 6: event `ar.evidence` (an active response's evidence snapshot); the audit rule split
  110405/110406 and the new 110412 (`ar.evidence`) are recorded in 15.4.
- 14.8 / new 16: Incus network ACLs for the `city-*` networks, with the allow list, applied
  and removed by `infra/08_acls.sh` (Phase 6 before/after).
- 5.2: `CSMS_ENFORCE_STATION_IP` before/after for the hijacking response, and how a
  station's registered `ip` is kept current.
- No code change to `common/` beyond the `ar.evidence` event.

Version 5 (2026-11-06), for Phase 5 (Wazuh), after the monitoring run:
- New section 15 (Wazuh): agent topology and names, groups and their `agent.conf`, log
  sources, decoders (verified on the manager: JSON `srcip` is Wazuh's static `srcip`;
  `same_source_ip`, `same_field` and `different_field` work), rule ID blocks
  **110000–110499** in our own files (the course-guide rules 100001–100301 stay), the full
  rule list for the three threats, MITRE mapping (Enterprise IDs in `<mitre>`, ICS IDs as
  groups: the manager's MITRE database has no ICS matrix), whitelists, four active
  responses with allow-lists, rate guards and timeouts, the correlator integration stub,
  credentials.
- 3.2, 4.1–4.3: **responder tokens** (`REGISTRY_RESPONDER_TOKEN`, `CSMS_RESPONDER_TOKEN`,
  `BILLING_RESPONDER_TOKEN`): isolate-only scopes for the active responses, never the
  admin token.
- 6: events `host.new_outbound` (station outbound-connection watcher) and `ar.action`
  (active-response audit); fields `dstip`, `dstport`, `process_name`, `ar_command`, `rule_id`,
  `alert_id`.
- 7.3: `city:station_power_gap_watts` and `city:edge_up:min` need `group_left ()`
  (found by the rule unit tests).
- 13.2: station memory limit 224 MiB (room for the Wazuh agent).
- 14.3: counters that appear at their first event are tested with the growth pattern
  `m - (m offset 5m or 0 * m)` instead of `increase()`; city-level alerts carry
  `hier_id="pt.<city>"`, central ones `city="central"`.

Version 4 (2026-11-06), for Phase 4 (monitoring backbone), after the city run:
- New section 14 (monitoring): city Prometheus on each edge, central Prometheus /
  Alertmanager / Grafana in namespace `monitoring`, federation of `city:*`, scrape jobs and
  labels, alert names and expressions, inhibition, dashboards and the Geomap query, the
  bandwidth measurement method, the baseline and how thresholds are derived, network flows
  and the firewall decision (Incus network ACLs, Phase 6).
- 7.3: recording-rule expressions fixed. `city:edge_requests:rate1m` has no `code_class`
  and `city:edge_rate_limited:rate1m` is dropped (the nginx stub_status exporter has no
  status codes; refusals are in the edge log); new `city:station_chargers:count`,
  `city:grid_power_watts:sum` and `city:edge_up:min`.
- 5.2: mTLS proves possession of a key, not where the device is; `CSMS_ENFORCE_STATION_IP`
  is the stricter setting.
- 9.2: a `--pki-dir` holding only `ca.crt` = verify the server, present no client certificate.
- 11.3: upstreams use `max_fails=0` (failover per handshake through
  `proxy_next_upstream`; a marked-down server never recovered with long-lived WebSockets).
- 13.2: edge memory limit 384 MiB (it also runs the city Prometheus).
- 6.1: clock note (city containers use the host clock; the K3s VMs can drift).

Version 3 (2026-11-06), for Phase 3 (city sites, edge gateways, PKI), after the K3s run:
- New sections 11 (edge gateway), 12 (PKI) and 13 (city sites and station runtime).
- 2.1 and 2.2: edges also reach the registry NodePort 30801; CSMS placement rule (HPA 2–4,
  one replica per node guaranteed, edge failover over both nodes).
- 3.2: `CSMS_TRUSTED_PROXIES` and `CSMS_ALLOW_DIRECT` come from the ConfigMap `csms-edge`;
  `CSMS_ALLOW_DIRECT=false` once the edges exist.
- 4.2: the replica that receives the API call logs `command.sent` / `command.result`
  (with `target` = the owner replica); `pod` in the result is the owner. `probe` commands
  write a `commands` row but no log line.
- 5.2: the edge uses `ssl_verify_client optional`: no certificate reaches the CSMS
  (`ident.no_cert`), a failed or revoked certificate is refused by nginx itself (HTTP 400,
  edge log). Certificate serials are compared normalised. A command for an id drops that
  id's registry cache entry on **every** replica (`NOTIFY csms_cache_drop`).
- 6: `edge.access` event and the fields `cert_verify`, `cert_dn`, `upstream`,
  `upstream_status`, `bytes_out`.
- 8.1: additive schema upgrade at startup (`evcommon.dbschema.add_missing_columns`);
  anything else needs a manual migration or a fresh database.

Version 2 (2026-11-06), after the Phase 1 local run:
- 4.0: fixed order of checks on every HTTP API (401, then 422, then 403, then 404/409);
  `source` is a closed list, anything else is 422.
- 4.1: `/locate/{id}` returns the element **and its subtree** (also for an exact id);
  `/sd/prometheus` has no default `kind`; central elements are counted with
  `city="central"` in the registry metrics.
- 4.2: a charger answering a command with a CallError gives status `Error` and HTTP
  **502**; status table completed. Release procedure after a quarantine (5.3).
- 4.4: `ConcurrentTx` rejections occur in normal traffic; fraud rules must not score them
  as attacks on their own.
- 5.2: a certificate that fails verification is refused in every city (also plain ones)
  with `ident.no_cert`; `ident.spoofed_header` is one line per forged header; unknown ids
  (registry 404) are cached too; with several replicas the duplicate guard is the
  `connections` primary key.
- 5.3: a StopTransaction for a transaction the CSMS refused is answered normally and is
  not an anomaly; open sessions of a charger are closed (`PowerLoss`) at its next
  BootNotification; a released charger stays `Unavailable` until an explicit
  `change-availability Operative`.
- 3.2 and 8.3: new `CSMS_METER_FLUSH_S`, `CSMS_POD_HEARTBEAT_S`, `CSMS_POD_STALE_S`;
  `sessions.last_meter_at`; startup cleans only the replica's own rows; open sessions are
  never closed at startup; a replica that does not hold a transaction in memory reloads it
  from `sessions`; `ConcurrentTx` is checked in the database.
- 8.1: Postgres has one role per service, each owning only its own database
  (`PG_PASSWORD_REGISTRY`, `PG_PASSWORD_CSMS`, `PG_PASSWORD_BILLING`).
- 9.3: a lost connection interrupts the charger's session (no StopTransaction is sent);
  demos use a shorter `--session-minutes`.
- 10: image names, Postgres image mirror, roster ConfigMap, K8s object names.

Contents:
1. [Naming: hier-ids](#1-naming-hier-ids)
2. [Components, ports and endpoints](#2-components-ports-and-endpoints)
3. [Configuration (environment variables)](#3-configuration-environment-variables)
4. [HTTP APIs](#4-http-apis)
5. [OCPP 1.6J profile](#5-ocpp-16j-profile)
6. [JSON log lines](#6-json-log-lines)
7. [Prometheus metrics](#7-prometheus-metrics)
8. [Storage](#8-storage)
9. [Simulator](#9-simulator)
10. [Packaging and run conventions](#10-packaging-and-run-conventions)
11. [Edge gateway](#11-edge-gateway)
12. [PKI](#12-pki)
13. [City sites and station runtime](#13-city-sites-and-station-runtime)
14. [Monitoring](#14-monitoring)
15. [Wazuh](#15-wazuh)
16. [City network ACLs](#16-city-network-acls-phase-6)

---

## 1. Naming: hier-ids

### 1.1 Grammar

| Kind | Form | Example |
|---|---|---|
| city | `pt.<city>` | `pt.porto` |
| station | `pt.<city>.<station>` | `pt.porto.st-003` |
| charger | `pt.<city>.<station>.<charger>` | `pt.porto.st-003.cp-2` |
| edge | `pt.<city>.edge` | `pt.porto.edge` |
| central | `pt.central.<node>` | `pt.central.k3s-master`, `pt.central.wazuh` |

```
city     = [a-z][a-z0-9]*(-[a-z0-9]+)*     max 24 chars, not "central", "edge" or "lab"
station  = st-NNN                          001..999
charger  = cp-N                            1..99
node     = [a-z][a-z0-9]*(-[a-z0-9]+)*     max 40 chars
```

Single regex (a superset: the reserved names and `st-000` are rejected only by
`evcommon.hierid.parse`, which is authoritative):

```
^pt\.(?:central\.[a-z][a-z0-9]*(?:-[a-z0-9]+)*|[a-z][a-z0-9]*(?:-[a-z0-9]+)*(?:\.edge|\.st-[0-9]{3}(?:\.cp-[1-9][0-9]?)?)?)$
```

`pt` and `pt.central` are valid **prefixes** (for `/locate` and delegation) but not
element ids. Prefix matching is on dot boundaries: `pt.porto` covers
`pt.porto.st-003.cp-2` but not `pt.portoalegre`.

### 1.2 One string everywhere

| Where | Station `pt.porto.st-003` | Charger `pt.porto.st-003.cp-2` | Edge `pt.porto.edge` | Central `pt.central.k3s-master` |
|---|---|---|---|---|
| Registry key | `pt.porto.st-003` | `pt.porto.st-003.cp-2` | `pt.porto.edge` | `pt.central.k3s-master` |
| DNS name | `st-003.porto.pt.lab` | `cp-2.st-003.porto.pt.lab` (cert SAN only, resolves nowhere) | `edge.porto.pt.lab` | `k3s-master.incus` |
| Incus instance | `porto-st-003` | (runs inside `porto-st-003`) | `porto-edge` | `k3s-master` |
| Incus network | `city-porto` | `city-porto` | `city-porto` | `incus-default` |
| Incus config | `user.hier-id`, `user.location.lat`, `user.location.lon` | | `user.hier-id` | `user.hier-id` |
| Certificate | — | `CN=pt.porto.st-003.cp-2, OU=porto, O=EVCharge Lab`, SAN DNS = DNS name | `CN=pt.porto.edge`, SAN `edge.porto.pt.lab` | — |
| OCPP charge-point id | — | URL path `/ocpp/pt.porto.st-003.cp-2` | — | — |
| Wazuh agent name / group | `pt.porto.st-003` / `city-porto,station` | (logged as field `hier_id`) | `pt.porto.edge` / `city-porto,edge` | `pt.central.k3s-master` / `central` |
| Prometheus labels | `hier_id`, `city`, `station` | `hier_id`, `city`, `station`, `charger` | `hier_id`, `city` | `hier_id` |
| Log fields | `hier_id` (+ `city`, `station` derived) | `hier_id` (+ `city`, `station`, `charger`) | `hier_id`, `edge` | `instance` |

Notes:
- Each city network resolves `<name>.<city>.pt.lab` itself and forwards `*.incus` to the
  central bridge (`infra/02_networks.sh`). Incus registers instances under their instance
  name (`porto-st-003.porto.pt.lab`); the short station and edge names
  (`st-003.porto.pt.lab`, `edge.porto.pt.lab`) are aliases added by the city setup (Phase 3).
- A charger has no address of its own: it is reached through its station's IP. The IP
  locates the station; the hier-id (and the certificate) locates the charger.
- Code never hardcodes IPs. Components find each other by name or by environment variable.

### 1.3 Delegation

Each city operator is authoritative for `pt.<city>.*`. In the registry this is enforced
by per-city bearer tokens (section 4.2): the token of city X may create, change, delete,
quarantine and release only elements strictly below `pt.X`. The NOC (admin token) owns
`pt`, the city nodes `pt.<city>` and `pt.central.*`. The `operator` of an element
(reported by `/locate`) is its city, or `noc` for central elements.

### 1.4 Library

`evcommon.hierid`: `parse()`, `is_valid()`, `is_prefix()`, `is_under()`, builders
(`charger()`, `station()`, `edge()`, `city()`, `central()`), `HierId.dns_name()`,
`from_dns()`, `incus_instance()`, `labels()`, `ancestry()`, `path_id()` (OCPP URL path to
id) and `cn_from_dn()` (CN from the DN the edge forwards).

---

## 2. Components, ports and endpoints

### 2.1 Who calls whom

```
charger (sim) --ws/wss--> edge nginx (city) --ws--> csms :9000        (local dev: sim -> csms directly)
csms  --> registry  GET /nodes/{id}                 identity and quarantine check
csms  --> billing   /authorize, /sessions           cards and accounting (local stub if BILLING_URL is empty)
csms  --> fraud     POST /events                    best effort (disabled if FRAUD_URL is empty)
fraud --> billing, registry, csms                   responses (Phase 7)
Wazuh active responses --> registry, csms, billing, edge firewall
Wazuh integration, Alertmanager --> correlator --> registry /locate, csms /connections
city Prometheus --> sim :9300, edge exporters;  central Prometheus --> services /metrics, federates city:*
edge (city operator) --> registry :30801          registration of its own subtree (city token), Prometheus SD
```

### 2.2 Ports

| Component | Port | Purpose | Local dev (Phase 1) | K3s (namespace `evcharge`) |
|---|---|---|---|---|
| csms | 9000 | OCPP 1.6J WebSocket, path `/ocpp/<hier_id>` | `ws://127.0.0.1:9000/ocpp/` | Service `csms-ocpp`, **NodePort 30900**, `externalTrafficPolicy: Local` |
| csms | 8000 | command API, `/health`, `/metrics` | `http://127.0.0.1:8000` | Service `csms`, NodePort 30800 |
| registry | 8001 | API, `/health`, `/metrics` | `http://127.0.0.1:8001` | Service `registry`, NodePort 30801 |
| billing | 8002 | API, `/health`, `/metrics` | `http://127.0.0.1:8002` | Service `billing`, NodePort 30802 |
| fraud | 8003 | API, `/health`, `/metrics` | `http://127.0.0.1:8003` | Service `fraud` (ClusterIP) |
| correlator | 8004 | API, `/health`, `/metrics` | `http://127.0.0.1:8004` | Service `correlator`, NodePort 30804 |
| postgres | 5432 | databases and roles `registry`, `csms`, `billing` | (SQLite instead) | StatefulSet + Service `postgres` (ClusterIP) |
| sim | 9300 | station metrics `/metrics` | `127.0.0.1:<dev_metrics_port>` (9301…, from the roster) | on each station container |
| central Prometheus | 9090 | federation of the cities, alert rules (section 14) | | namespace `monitoring`, NodePort 30090 |
| Alertmanager | 9093 | | | `monitoring`, NodePort 30093 |
| Grafana | 3000 | | | `monitoring`, NodePort 30300 |
| edge nginx | 443 | `wss://edge.<city>.pt.lab/ocpp/<hier_id>`, mTLS (TLS cities) | | `<city>-edge` |
| edge nginx | 80 | `ws://edge.<city>.pt.lab/ocpp/<hier_id>` (plain cities only) | | `<city>-edge` |
| edge nginx status | 127.0.0.1:8080 | `stub_status` for the exporter | | |
| nginx exporter | 9113 | | | `<city>-edge` |
| city Prometheus | 9090 | scrapes its city every 5 s, serves `/federate` (section 14.1) | | on `<city>-edge` |
| node exporter | 9100 | | | edges, stations |
| Wazuh manager | 1514, 1515, 55000 | agents, enrolment, API | | `wazuh-container` |

Edge upstreams are `k3s-master.incus:30900` and `k3s-node1.incus:30900`. With
`externalTrafficPolicy: Local` the CSMS pod sees the edge's real IP (with the default
`Cluster` policy kube-proxy would SNAT it to a node IP and the trust check of section 5.2
could not work); a node without a CSMS pod refuses, and nginx moves to the next upstream.

CSMS placement: HPA 2–4 replicas on CPU; `topologySpreadConstraints` maxSkew 1 on the host
name with `DoNotSchedule`, and rolling updates with `maxSurge: 0, maxUnavailable: 1`. With
two nodes and at least two replicas, every node keeps a replica except for the seconds a
pod is being replaced; the edge's failover (section 11) covers that gap. A replica added by
the HPA only receives new connections (WebSockets are not rebalanced).

All HTTP services also expose `GET /health` (no auth) and `GET /metrics` (no auth, exact
path, no redirect).

---

## 3. Configuration (environment variables)

Defaults suit local dev. Secrets have **no default**: a service refuses to start if a
required token is missing or shorter than 16 characters. Local dev keeps generated
secrets in `secrets/local.env` (gitignored); K3s gets them from Secrets built from
`secrets/*.env` (template: `secrets.example/platform.env.example`).

Variable names avoid the `<SERVICE>_PORT` / `<SERVICE>_SERVICE_HOST` pattern that K8s
injects for Services, and every pod sets `enableServiceLinks: false` as well.

### 3.1 All Python services

| Variable | Default | Meaning |
|---|---|---|
| `BIND_HOST` | `127.0.0.1` | listen address (`0.0.0.0` in containers) |
| `LOG_DIR` | `./logs` | JSON log directory (`/var/log/evcharge` in deployment) |
| `LOG_LEVEL` | `info` | `debug`, `info`, `warning`, `error` |
| `LOG_STDOUT` | `true` | also write the JSON lines to stdout |
| `LOG_MAX_BYTES` | `52428800` | rotate the log file at this size (3 backups) |
| `INSTANCE_ID` | hostname | instance name in logs and the `connections.pod` column; the pod name in K8s (downward API) |
| `DATABASE_URL` | per service, below | SQLAlchemy URL; `sqlite:///…` locally, `postgresql+psycopg://…` on K3s |

### 3.2 Per service

| Service | Variable | Default | Meaning |
|---|---|---|---|
| registry | `REGISTRY_LISTEN_PORT` | `8001` | |
| | `DATABASE_URL` | `sqlite:///./data/registry.db` | |
| | `REGISTRY_ADMIN_TOKEN` | **required** | NOC token, full scope |
| | `REGISTRY_TOKEN_<CITY>` | optional, one per city | city operator token; `<CITY>` upper-case, `-` written as `_` (`REGISTRY_TOKEN_PORTO`) |
| | `REGISTRY_RESPONDER_TOKEN` | optional | isolate-only token for the active responses (4.1). Deliberately **not** `REGISTRY_TOKEN_RESPONDER`, which would be read as a city |
| csms | `CSMS_OCPP_LISTEN_PORT` | `9000` | |
| | `CSMS_API_LISTEN_PORT` | `8000` | |
| | `DATABASE_URL` | `sqlite:///./data/csms.db` | |
| | `CSMS_API_TOKEN` | **required** | bearer for `/connections` and `/commands` |
| | `REGISTRY_URL` | `http://127.0.0.1:8001` | |
| | `BILLING_URL` | empty | empty = built-in local stub (section 4.3.1) |
| | `BILLING_API_TOKEN` | required if `BILLING_URL` set | |
| | `FRAUD_URL` | empty | empty = do not send events |
| | `FRAUD_API_TOKEN` | required if `FRAUD_URL` set | |
| | `CSMS_TRUSTED_PROXIES` | empty | comma list of edge IPs, CIDRs or host names (resolved at start and every 5 min). On K3s: from ConfigMap `csms-edge`, written by the city setup with the edges' addresses (section 13) |
| | `CSMS_ALLOW_DIRECT` | `false` | accept connections that do not come through an edge (local dev only). On K3s: ConfigMap `csms-edge`; `true` only until the edges exist |
| | `CSMS_PLAIN_CITIES` | `aveiro` | cities where a missing client certificate is accepted (legacy site) |
| | `CSMS_ENFORCE_STATION_IP` | `false` | reject (not just log) `ident.ip_mismatch` (check 9). Hijacking after-state: `true` turns a stolen key used from another host into a 403 instead of a warning. Needs each station's registered `ip` kept current (static leases, or re-registration on a DHCP change); set per city in ConfigMap `csms-edge` |
| | `CSMS_CHECK_CONCURRENT_TX` | `true` | answer `ConcurrentTx` if the card already has an open transaction |
| | `CSMS_HEARTBEAT_INTERVAL` | `30` | seconds, sent in BootNotification.conf |
| | `CSMS_METER_INTERVAL` | `10` | expected MeterValues period (s), for anomaly checks |
| | `CSMS_POWER_TOLERANCE` | `1.10` | factor over `max_kw` before `over_power`/`over_energy` |
| | `CSMS_RATE_LIMIT_PER_S` | `1.0` | sustained OCPP messages/s per connection |
| | `CSMS_RATE_BURST` | `20` | token-bucket size |
| | `CSMS_RATE_KICK_AFTER_S` | `30` | close a connection that stays over the limit this long |
| | `CSMS_REGISTRY_CACHE_S` | `30` | cache TTL of registry lookups, found or not found (a newly registered charger may wait this long) |
| | `CSMS_REGISTRY_FAIL_OPEN` | `false` | accept unknown-state chargers when the registry is down |
| | `CSMS_BILLING_FAIL_OPEN` | `false` | authorise cards when billing is down |
| | `CSMS_CMD_TIMEOUT_S` | `10` | wait for a charger's answer to a command |
| | `CSMS_METER_FLUSH_S` | `60` | write the last reading of every open transaction to `sessions` this often (one batched update) |
| | `CSMS_POD_HEARTBEAT_S` | `5` | replica heartbeat in `csms_pods` |
| | `CSMS_POD_STALE_S` | `15` | a replica silent this long is dead: its `connections` rows are ignored and deleted |
| | `CSMS_LOCAL_BLOCKED_CARDS` | empty | comma list, used only by the local billing stub |
| | `CSMS_RESPONDER_TOKEN` | optional | isolate-only token for the active responses (4.2) |
| billing (Phase 2) | `BILLING_LISTEN_PORT` | `8002` | |
| | `DATABASE_URL` | `sqlite:///./data/billing.db` | |
| | `BILLING_API_TOKEN` | **required** | |
| | `BILLING_RESPONDER_TOKEN` | optional | isolate-only token for the active responses (4.3) |
| | `BILLING_SEED_ROSTER` | empty | roster path; seeds cards and the default tariff if the tables are empty |
| fraud (Phase 7) | `FRAUD_LISTEN_PORT` | `8003` | |
| | `FRAUD_API_TOKEN` | **required** | |
| | `DATABASE_URL` | `sqlite:///./data/fraud.db` | Postgres database `fraud` on K3s |
| | `FRAUD_ALERT_SCORE` | `0.7` | score at which a `FraudAlert` is written |
| | `FRAUD_MAX_KMH` / `FRAUD_ENERGY_TOL` / `FRAUD_PEER_FACTOR` / `FRAUD_GRID_FACTOR` / `FRAUD_WINDOW_H` / `FRAUD_MIN_SESSIONS` / `FRAUD_MAX_SESSIONS_H` | `200` / `1.15` / `0.5` / `0.8` / `6` / `20` / `8` | rule thresholds (4.4) |
| | `FRAUD_DRY_RUN` | `false` | log alerts and intended actions, call no API (baseline-safe runs) |
| | `BILLING_URL`, `BILLING_RESPONDER_TOKEN`, `REGISTRY_URL`, `REGISTRY_RESPONDER_TOKEN`, `CSMS_API_URL`, `CSMS_RESPONDER_TOKEN`, `CORRELATOR_URL`, `CORRELATOR_API_TOKEN` | | act on alerts (isolate-only tokens) and post criticals to the correlator |
| correlator (Phase 9) | `CORRELATOR_LISTEN_PORT` | `8004` | |
| | `CORRELATOR_API_TOKEN` | **required** | |
| | `DATABASE_URL` | `sqlite:///./data/correlator.db` | closed-incident history (optional) |
| | `CORRELATOR_WINDOW_S` | `120` | join window |
| | `CORRELATOR_SKEW_S` | `3` | extra clock-skew tolerance (city host clock vs K3s VMs) |
| | `CORRELATOR_CLOSE_S` | `600` | auto-close an incident after this quiet time |
| | `REGISTRY_URL`, `CSMS_API_URL`, `CSMS_API_TOKEN` | | topology (`/locate`) and connections (read-only) |
| sim | flags, section 9.2 | | env fallbacks `SIM_ROSTER`, `SIM_STATION`, `SIM_CSMS_URL`, `SIM_PKI_DIR` |

`evcommon.config` has the typed getters (`get_int`, `get_bool`, `get_list`, `require`,
`require_token`, `prefixed`, `database_url`, `instance_id`).

---

## 4. HTTP APIs

### 4.0 Conventions

- JSON bodies; models are in `evcommon.models` (pydantic v2, unknown fields rejected).
- Auth: `Authorization: Bearer <token>`, compared in constant time. Missing or wrong
  token: `401` and a `api.auth_failed` log line with `srcip`, `path`, `method`.
- Errors use FastAPI's shape: `{"detail": "..."}`. Validation errors: `422`.
- Order of checks on every endpoint that needs a token: **401** (missing or wrong token)
  → **422** (invalid path id or body) → **403** (token valid but out of scope) →
  **404 / 409** (state). A city token sending a malformed body for another city's id
  therefore gets 422, not 403.
- `GET /health` → `{"status":"ok","service":"registry","version":"0.1.0","checks":{"db":"ok"}}`.
- Every action that changes security state (quarantine, block, commands) carries
  `reason`, `source` and optional `incident_id`; they go into the log line so the
  post-mortem can follow the chain. `source` is one of `wazuh`, `fraud`, `correlator`,
  `operator`, `test` (default `operator`); any other value is 422.

### 4.1 Registry (Phase 1)

Elements of every kind live in one table (section 8.2). Kind, parent and city are derived
from the id, never sent.

| Method and path | Auth | Body → response |
|---|---|---|
| `GET /nodes?prefix=&kind=&state=&limit=500` | none | → `NodeList` |
| `GET /nodes/{hier_id}` | none | → `Node` or 404 |
| `PUT /nodes/{hier_id}` | admin or city (scope) | `NodeIn` → `Node`; 201 created, 200 replaced; 409 if the parent does not exist |
| `PATCH /nodes/{hier_id}` | admin or city (scope) | `NodePatch` → `Node` |
| `DELETE /nodes/{hier_id}?recursive=false` | admin or city (scope) | 204; 409 if it has children and not recursive |
| `POST /quarantine/{hier_id}` | admin or city (scope) | `QuarantineRequest` → `Node` (idempotent) |
| `POST /quarantine/{hier_id}/release` | admin or city (scope) | `QuarantineRequest` → `Node` |
| `GET /quarantine?prefix=` | none | → `NodeList` of elements whose own state is quarantined |
| `GET /locate/{prefix}?kind=` | none | → `LocateResponse`: the element (if it exists) and everything below it, ordered by id (max 500 matches, `truncated`) |
| `GET /locate/ip/{ip}` | none | → `LocateResponse`: elements with that `ip`, and the chargers of a matching station |
| `GET /sd/prometheus?city=&kind=` | none | → Prometheus HTTP SD list for elements with `metrics_target` (all kinds unless `kind` is given) |
| `GET /whoami` | any token | → `WhoAmI` |

Responder token (`REGISTRY_RESPONDER_TOKEN`, operator `responder`): may only
`POST /quarantine/{hier_id}` (any element) and read; release, PUT, PATCH and DELETE → 403
(`registry.write_denied`). Isolation is automatic, undoing it is a human decision.

Scope: the admin token may write anything. The token of city X may write only ids
strictly below `pt.X` (stations, chargers, `pt.X.edge`). Anything else: `403` and a
`registry.write_denied` line (`operator`, `hier_id`, `srcip`). Every successful write
logs `registry.write` (`op` = create|update|delete, `operator`).

`effective_state` is the element's own state if it is not `active`; otherwise
`quarantined` if any ancestor is quarantined. The CSMS uses `effective_state`, so
quarantining a station blocks all its chargers.

Quarantine and release are idempotent (repeating one returns the same `Node`, and logs
and counts it again). In `evcharge_registry_nodes` and
`evcharge_registry_quarantines_total`, central elements have `city="central"`.

```http
PUT /nodes/pt.porto.st-003.cp-2
Authorization: Bearer $REGISTRY_TOKEN_PORTO

{"name": "Aliados charger 2", "max_kw": 22, "connectors": 1,
 "vendor": "EVLab", "model": "EVL-AC22", "serial": "EVL22-0032"}
```
```json
{"hier_id": "pt.porto.st-003.cp-2", "kind": "charger", "parent": "pt.porto.st-003",
 "city": "porto", "station": "st-003", "charger": "cp-2",
 "name": "Aliados charger 2", "lat": null, "lon": null, "max_kw": 22.0, "connectors": 1,
 "vendor": "EVLab", "model": "EVL-AC22", "serial": "EVL22-0032", "cert_serial": null,
 "host": null, "ip": null, "network": null, "dns": null, "metrics_target": null,
 "state": "active", "effective_state": "active", "state_reason": null, "state_source": null,
 "state_changed_at": null, "created_at": "2026-11-06T09:12:03.120000Z",
 "updated_at": "2026-11-06T09:12:03.120000Z", "updated_by": "porto"}
```
Stations carry `lat`, `lon`, `host` (`porto-st-003`), `ip`, `network` (`city-porto`),
`dns` (`st-003.porto.pt.lab`) and `metrics_target` (`st-003.porto.pt.lab:9300`; locally
`127.0.0.1:9303`). Chargers inherit location from their station.

```http
POST /quarantine/pt.porto.st-003.cp-2
Authorization: Bearer $REGISTRY_ADMIN_TOKEN

{"reason": "duplicate charger id from 10.31.0.66", "source": "wazuh", "incident_id": "inc-0007"}
```
→ the `Node` with `"state": "quarantined"`, plus a `registry.quarantine` line.

```http
GET /locate/pt.porto.st-003.cp-2
```
```json
{"query": "pt.porto.st-003.cp-2", "count": 1, "truncated": false,
 "matches": [{"hier_id": "pt.porto.st-003.cp-2", "kind": "charger", "state": "active",
   "effective_state": "active",
   "path": ["pt.porto", "pt.porto.st-003", "pt.porto.st-003.cp-2"],
   "operator": "porto", "edge": "pt.porto.edge", "station": "pt.porto.st-003",
   "lat": 41.1496, "lon": -8.6109, "host": "porto-st-003", "ip": "10.31.0.13",
   "network": "city-porto", "dns": "st-003.porto.pt.lab"}]}
```
For a charger, `lat`, `lon`, `host`, `ip`, `network` and `dns` come from its station.
`GET /locate/pt.porto` returns every element under Porto (city node first, then by id);
`GET /locate/pt.porto.st-003` returns the station followed by its chargers.

```http
GET /sd/prometheus?city=porto
```
```json
[{"targets": ["st-003.porto.pt.lab:9300"],
  "labels": {"hier_id": "pt.porto.st-003", "city": "porto", "station": "st-003"}}]
```

### 4.2 CSMS command API (Phase 1)

All endpoints except `/health` and `/metrics` need `CSMS_API_TOKEN`. The responder token
(`CSMS_RESPONDER_TOKEN`) may only call `GET /connections`, `GET /connections/{hier_id}`,
`remote-stop`, `disconnect` and `change-availability` with `type: Inoperative`; anything
else → 403 and an `api.auth_failed` line with `reason=scope`.

| Method and path | Body → response |
|---|---|
| `GET /connections?prefix=` | → `ConnectionList` (all replicas in Phase 2, from the `connections` table) |
| `GET /connections/{hier_id}` | → `Connection` or 404 |
| `POST /commands/{hier_id}/remote-stop` | `RemoteStopRequest` → `CommandResult` |
| `POST /commands/{hier_id}/change-availability` | `ChangeAvailabilityRequest` → `CommandResult` |
| `POST /commands/{hier_id}/reset` | `ResetRequest` → `CommandResult` |
| `POST /commands/{hier_id}/disconnect` | `DisconnectRequest` → `CommandResult` (CSMS closes the socket, code 1008) |

HTTP status of a command (the body is always a `CommandResult`):

| HTTP | `status` | When |
|---|---|---|
| 200 | `Accepted`, `Rejected`, `Scheduled` | the charger answered with a CALLRESULT |
| 404 | `NotConnected` | no live connection for the id on any replica |
| 409 | `Rejected` | the CSMS refuses locally (e.g. remote-stop without an open transaction) |
| 502 | `Error` | the charger answered with a CallError |
| 504 | `Timeout` | no answer within `CSMS_CMD_TIMEOUT_S` |
 The replica that **received** the API call logs
`command.sent` and `command.result` (`command`, `cmd_id`, `result`, `source`,
`incident_id`, and `target` = the replica that holds the socket); `pod` in the
`CommandResult` is that owner replica. The internal `probe` command (duplicate check)
writes a `commands` row but no log line and no metric. Every command, `probe` excepted,
makes **all** replicas drop the id's registry cache entry (`NOTIFY csms_cache_drop,
'<hier_id>'`; one process: a direct call).

```http
POST /commands/pt.porto.st-003.cp-2/change-availability
Authorization: Bearer $CSMS_API_TOKEN

{"connector_id": 0, "type": "Inoperative", "reason": "quarantine", "source": "wazuh", "incident_id": "inc-0007"}
```
```json
{"cmd_id": "6f1c0b9e-1f0e-4d4b-9a51-0a4b0e2c9d11", "hier_id": "pt.porto.st-003.cp-2",
 "command": "change-availability", "status": "Accepted", "pod": "csms-7d9f5-abcde", "detail": null}
```
```http
GET /connections?prefix=pt.porto.st-003
```
```json
{"count": 1, "items": [{"hier_id": "pt.porto.st-003.cp-2", "city": "porto", "station": "st-003",
  "pod": "csms-7d9f5-abcde", "src_ip": "10.31.0.13", "edge": "pt.porto.edge",
  "cert_cn": "pt.porto.st-003.cp-2", "cert_serial": "1A2B3C", "tls": true,
  "since": "2026-11-06T09:15:00Z", "last_seen": "2026-11-06T09:20:30Z", "booted": true,
  "status": "Charging", "active_transaction_id": 1042}]}
```

### 4.3 Billing (Phase 2)

All endpoints except `/health` and `/metrics` need `BILLING_API_TOKEN`. The responder token
(`BILLING_RESPONDER_TOKEN`) may only call `POST /cards/{card}/block`,
`POST /sessions/{transaction_id}/flag`, `GET /cards/{card}` and `GET /sessions`; unblock and
everything else → 403 (`api.auth_failed`, `reason=scope`).

| Method and path | Body → response |
|---|---|
| `POST /authorize` | `AuthorizeRequest` → `AuthorizeResponse` (`Accepted`, `Blocked`, `Expired`, `Invalid`) |
| `POST /sessions` | `SessionStart` → `Session` (status `open`; idempotent on `transaction_id`) |
| `POST /sessions/{transaction_id}/stop` | `SessionStop` → `Session` (status `closed`, `energy_wh`, `cost_eur`) |
| `POST /sessions/{transaction_id}/flag` | `SessionFlag` → `Session` (status `flagged`) |
| `GET /sessions?card=&hier_id=&status=&since=&limit=100` | → `SessionList` |
| `GET /cards/{card}` | → `Card` |
| `POST /cards` | `CardIn` → `Card` (seeding) |
| `POST /cards/{card}/block` | `CardBlockRequest` → `Card` (idempotent) |
| `POST /cards/{card}/unblock` | `CardBlockRequest` → `Card` |
| `GET /tariffs` | → `list[Tariff]` |

An unknown card is `Invalid`. Cost = `session_fee_eur + energy_kwh × price_per_kwh_eur +
minutes × price_per_min_eur`, rounded to cents. Default tariff `standard`: 0.35 €/kWh,
0 €/min, 0 € fee.

```http
POST /authorize
{"card": "04A1B2C3D4E5F6", "hier_id": "pt.porto.st-003.cp-2"}
```
```json
{"card": "04A1B2C3D4E5F6", "status": "Blocked", "expiry_date": null}
```
```http
POST /sessions/1042/stop
{"meter_stop_wh": 1203456, "stopped_at": "2026-11-06T09:31:10Z", "reason": "Local"}
```
```json
{"transaction_id": 1042, "card": "04A1B2C3D4E5F6", "hier_id": "pt.porto.st-003.cp-2",
 "city": "porto", "connector_id": 1, "tariff_id": "standard",
 "started_at": "2026-11-06T09:21:10Z", "stopped_at": "2026-11-06T09:31:10Z",
 "meter_start_wh": 1199790, "meter_stop_wh": 1203456, "energy_wh": 3666, "cost_eur": 1.28,
 "status": "closed", "flag_reason": null, "stop_reason": "Local"}
```

#### 4.3.1 Local billing stub inside the CSMS (Phase 1)

When `BILLING_URL` is empty the CSMS uses an in-process stub with the same interface:
every syntactically valid card is `Accepted` unless listed in `CSMS_LOCAL_BLOCKED_CARDS`
(then `Blocked`); session start/stop are no-ops. The CSMS code calls billing through one
small client interface with two implementations (HTTP and stub), so Phase 2 only changes
configuration.

### 4.4 Fraud service (Phase 7)

A FastAPI service (module `evfraud`), port 8003, scoring cards and chargers from the
events the CSMS sends it and from the registry. It never sits on the OCPP path: the CSMS
posts events best-effort (bounded queue of 1000, drop-oldest, counted in
`evcharge_csms_events_dropped_total`, 2 s timeout). All endpoints but `/health` and
`/metrics` need `FRAUD_API_TOKEN`.

| Method and path | Body → response |
|---|---|---|
| `POST /events` | `FraudEvent` (6 / models) → `202 {"accepted": true}` |
| `GET /alerts?since=&card=&hier_id=&limit=100` | → `{"count", "items": [FraudAlert]}` |
| `GET /scores/cards/{card}` | → `{"card", "score", "rules": [...], "last_seen"}` |
| `GET /scores/chargers/{hier_id}` | → `{"hier_id", "score", "rules": [...]}` |

`FraudAlert`: `{alert_id, ts, rule, severity (info|warning|critical), card?, hier_id?,
other_hier_id?, city?, score, detail, evidence: {...}}`. State is kept in the fraud
database (`DATABASE_URL`, `sqlite:///./data/fraud.db` locally, Postgres database `fraud`
on K3s): tables `card_events` (recent sessions per card: ts, city, hier_id, lat, lon,
energy_wh, transaction_id), `station_stats` (rolling per-station energy and grid power) and
`fraud_alerts`.

Rules (each adds to a score in [0,1] with a stated weight; crossing `FRAUD_ALERT_SCORE`
writes a `FraudAlert` and, for the critical ones, calls an action):

| Rule | Fires when | Severity | Action |
|---|---|---|---|
| `impossible_travel` | the same card starts sessions in two cities less than `distance / FRAUD_MAX_KMH` hours apart (`FRAUD_MAX_KMH` default 200; city coordinates from the registry) | critical | `block_card` |
| `over_energy` | a stopped session's `energy_wh` exceeds `max_kw × duration_h × 1000 × FRAUD_ENERGY_TOL` (default 1.15) | warning | flag the session |
| `meter_backwards` | a `meter.anomaly` with `reason` `backwards` or `stop_mismatch` | warning | flag the session |
| `peer_deviation` | a station's energy per session is below `FRAUD_PEER_FACTOR` (0.5) of its city's median over `FRAUD_WINDOW_H` (6 h), with at least `FRAUD_MIN_SESSIONS` (20) peer sessions | warning | — (the charger, not a card) |
| `grid_gap` | a station's reported energy is below `FRAUD_GRID_FACTOR` (0.8) of its feeder-meter energy over the window (tamper signature that the CSMS cannot see) | critical | `quarantine_charger` |
| `card_velocity` | more than `FRAUD_MAX_SESSIONS_H` (8) sessions for one card in an hour | warning | — |

Actions use the **responder tokens** (never admin): `block_card` → billing
`POST /cards/{card}/block` (+ `/sessions/{id}/flag`), `quarantine_charger` → registry
`POST /quarantine/{id}` then CSMS `change-availability Inoperative` + `disconnect`. Each
action logs `fraud.action` (`rule`, `target`, `result`) and, before it, `fraud.alert`.
`FRAUD_DRY_RUN` (default `false` locally, see Phase 7) makes it log the alert and the
intended action without calling the APIs — used so the service can run during the baseline
without changing state.

Metrics: section 7.2 (`evcharge_fraud_*`). The fraud service is a **second detector**
beside Wazuh (which has its own impossible-travel and meter rules on the same logs); the
report compares the two and the correlator joins their alerts. `ConcurrentTx` alone is
normal traffic (4.4 note kept): only `impossible_travel` (distance and time) is a fraud
signal.

### 4.5 Correlator (Phase 9)

A FastAPI service (module `evcorrelator`), port 8004, that joins Prometheus (Alertmanager)
alerts, Wazuh alerts and fraud alerts into **one incident** per root cause, locates the
source through the registry, and records the response timeline. All endpoints but
`/health` and `/metrics` need `CORRELATOR_API_TOKEN`. In-memory state plus an optional
`DATABASE_URL` (`sqlite:///./data/correlator.db`) for the closed-incident history; losing
the memory loses only open incidents (they re-open from new signals).

| Method and path | Body → response |
|---|---|---|
| `POST /signals/alertmanager` | Alertmanager v4 webhook (a batch of alerts) → `202 {"accepted": n}` |
| `POST /signals/wazuh` | one Wazuh alert JSON (integration `custom-correlator`) → 202 |
| `POST /signals/fraud` | one `FraudAlert` (the fraud service posts criticals here too) → 202 |
| `GET /incidents?status=open|mitigated|closed&since=&limit=` | → `{"count", "items": [Incident]}` |
| `GET /incidents/{incident_id}` | → `Incident` |
| `POST /incidents/{incident_id}/mitigated` | `{"action", "by", "ts"?}` → `Incident` (sets `t_mitigated` if unset) |
| `POST /incidents/{incident_id}/close` | `{"by", "note"?}` → `Incident` |

Each inbound signal is normalised to `{source (prometheus|wazuh|fraud), key_type, key,
hier_id?, srcip?, card?, city?, kind, severity, ts, raw}`. `kind` is read from the
alert's `threat` label (Prometheus), the rule group (Wazuh: `hijack|fraud|botnet|infra`)
or the fraud rule.

Join keys, tried in this order: `hier_id`, then `srcip`, then `card`. A signal joins an
open incident when they share a key and their timestamps are within `CORRELATOR_WINDOW_S`
(default 120) widened by `CORRELATOR_SKEW_S` (default 3; the city containers run on the
host clock and the K3s VMs drift ~2 s, W3 finding) — i.e. |Δt| ≤ window + skew. Keys are
linked transitively within an incident: a `charger.duplicate_id` (hier_id + srcip) binds
later srcip-only signals to the same incident. Incident `kind` = the highest-severity
member's kind; `severity` = the max.

Root cause (`t_located`): the correlator walks the registry topology from the signals'
elements. For `hier_id` it calls `/locate/{hier_id}` (charger → station → edge → city);
for `srcip`, `/locate/ip/{srcip}` maps the address to a station or edge. The root cause is
the **lowest common element** of the located paths (a single charger if every signal is
about it; its station if several of its chargers; its city if several stations; `pt.central`
for platform signals). `RootCause` = `{key_type, key, hier_id, host, ip, city, edge,
explanation}`. `t_located` is set when the walk resolves to a concrete element.

`Incident` (schema in `evcommon.models`): `incident_id` (uuid), `kind`, `status`
(`open|mitigated|closed`), `severity`, `root_cause`, `signals` (the normalised list),
`keys` (the union of join keys), `t_first_signal`, `t_located`, `t_mitigated`,
`t_closed`, `summary`. `t_mitigated` is set by `POST .../mitigated` or when a signal that
is itself a response is seen (a Wazuh `ar.action result=ok`, a `registry.quarantine` or a
`billing.card_blocked` event joined into the incident). An incident auto-closes after
`CORRELATOR_CLOSE_S` (default 600) with no new signal.

Idempotency and de-dup: a repeated identical alert (same `alertname`+labels for Prometheus,
same `rule.id`+keys for Wazuh, within the window) updates the existing signal's `last_ts`
and count, it does not create a new incident. The Alertmanager inhibition (14.4) already
drops dependent alerts, so an edge outage yields one `infra` incident, not dozens.

Metrics: section 7.2 (`evcharge_correlator_*`), including the
`evcharge_correlator_time_to_locate_seconds` and `_time_to_mitigate_seconds` histograms
(from `t_first_signal`). The report's "locate the source in N s" number comes from these.

---

## 5. OCPP 1.6J profile

### 5.1 Transport

- URL: `<base>/ocpp/<charger hier-id>`, e.g. `ws://127.0.0.1:9000/ocpp/pt.porto.st-003.cp-2`
  (dev), `wss://edge.porto.pt.lab/ocpp/pt.porto.st-003.cp-2` (TLS city),
  `ws://edge.aveiro.pt.lab/ocpp/pt.aveiro.st-001.cp-1` (plain city).
- Subprotocol `ocpp1.6`, required. The server lists only `ocpp1.6`; websockets then
  answers HTTP 400 to a client that offers none.
- Max frame 64 KiB (`max_size=65536`); WebSocket ping every 20 s, timeout 20 s.
- Libraries: `ocpp==2.1.0` (`ocpp.v16`) and `websockets==17.4`, asyncio API
  (`websockets.asyncio.server.serve`, `websockets.asyncio.client.connect`).

### 5.2 Charger identity (CSMS handshake checks)

All checks run in `process_request(connection, request)`, **before** the WebSocket
upgrade, so a refused charger never gets an OCPP session. A refusal is an HTTP status
plus one log line. Order:

| # | Check | On failure |
|---|---|---|
| 1 | Peer address in `CSMS_TRUSTED_PROXIES`? Then trust the edge headers below: `srcip = X-Real-IP`. Otherwise, ignore every edge header present and log one `ident.spoofed_header` line per header (`reason` = header name); then, unless `CSMS_ALLOW_DIRECT`, refuse | 403 `ident.untrusted_peer` |
| 2 | Path is `/ocpp/<charger hier-id>` | 404 `ident.bad_id` |
| 3 | Registry knows the charger (`GET /nodes/{id}`, cached `CSMS_REGISTRY_CACHE_S`; any command for the id drops its cache entry, so a quarantine followed by `disconnect` takes effect at once) | 404 `ident.unknown`; registry down → 503 `dependency.error` (unless fail-open) |
| 4 | `effective_state` is `active` | 403 `ident.quarantined` |
| 5 | Through an edge: the edge's city (`X-Edge-Id`) equals the charger's city | 403 `ident.city_mismatch` |
| 6 | Through an edge in a city not in `CSMS_PLAIN_CITIES`: `X-Client-Cert-Verify` is `SUCCESS`. In any city, a presented certificate that failed verification (`FAILED:...`) is refused too | 403 `ident.no_cert` |
| 7 | If a certificate was presented: CN (`cn_from_dn(X-Client-Cert-DN)`) equals the id | 403 `ident.cert_mismatch` |
| 8 | If the registry has `cert_serial`: it equals `X-Client-Cert-Serial`, both normalised (upper case, `:` removed, leading zeros stripped) | 403 `ident.cert_serial_mismatch` |
| 9 | If the station has an `ip` in the registry: `srcip` equals it | log `ident.ip_mismatch`; 403 only if `CSMS_ENFORCE_STATION_IP` |
| 10 | The id is not already connected. If it is, probe the old connection (WebSocket ping, 5 s; through the command channel if it is on another replica). Old alive → refuse; old dead → close it and accept. With several replicas the guard is the `connections` primary key (8.3): the row is inserted before the upgrade completes, and a failed insert is a duplicate | 409 `charger.duplicate_id` (with `other_srcip`); or `conn.replaced` |

Accepted: `conn.open` with `srcip`, `peer`, `edge`, `tls`, `cert_cn`, `cert_serial`;
a row in `connections`. Every refusal also increments
`evcharge_csms_identity_rejections_total{reason}`.

Headers set by the edge (nginx always overwrites client-sent values with
`proxy_set_header`):

| Header | nginx value | Example |
|---|---|---|
| `X-Real-IP` | `$remote_addr` | `10.31.0.13` |
| `X-Client-Cert-DN` | `$ssl_client_s_dn` (RFC 2253) | `CN=pt.porto.st-003.cp-2,OU=porto,O=EVCharge Lab` |
| `X-Client-Cert-Serial` | `$ssl_client_serial` (upper-case hex) | `1A2B3C` |
| `X-Client-Cert-Verify` | `$ssl_client_verify` | `SUCCESS`, `NONE`, `FAILED:...` |
| `X-Edge-Id` | constant per edge | `pt.porto.edge` |

In a plain city the certificate headers are empty. In local dev (`CSMS_ALLOW_DIRECT=true`,
no edge) `srcip` is the socket peer, `tls` is false and checks 5–8 are skipped.

Division of work with the edge (section 11): the edge requests a client certificate but
does not require one (`ssl_verify_client optional`). A charger **without** a certificate
is forwarded with `X-Client-Cert-Verify: NONE` and refused by the CSMS (`ident.no_cert`,
logged centrally). A certificate that **fails** verification (unknown CA, expired,
revoked by the CRL) is refused by nginx with HTTP 400 and appears only in the edge log
(`edge.access`, `cert_verify` = `FAILED:...`). Check 6's `FAILED` branch stays as a second
line of defence.

What mTLS does **not** give: a certificate proves possession of the key, not where the
device is. A key copied to another host in the same city passes checks 5–8; the only
traces are `ident.ip_mismatch` (warning) and, if the real charger is connected, a 409
`charger.duplicate_id` for whichever of the two connects second. With
`CSMS_ENFORCE_STATION_IP=true` the IP mismatch becomes a refusal (the stricter setting for
the hijacking response), at the cost that a station whose address changes must be
re-registered first.

### 5.3 Messages

Charger → CSMS:

| Action | Fields the sim sends | CSMS reply | CSMS side effects |
|---|---|---|---|
| BootNotification | `chargePointVendor`, `chargePointModel`, `chargePointSerialNumber`, `firmwareVersion` | `status=Accepted`, `interval=CSMS_HEARTBEAT_INTERVAL`, `currentTime` | `charger.boot`; `charger.boot_mismatch` if the serial differs from the registry |
| Heartbeat | — | `currentTime` | `last_seen` |
| StatusNotification | `connectorId` (0 = whole charger), `errorCode`, `status`, `timestamp` | empty | `charger.status` on change; `charger.fault` if `Faulted` |
| Authorize | `idTag` = card id | `idTagInfo.status` | billing `/authorize`; `auth.accept` / `auth.reject` |
| StartTransaction | `connectorId`, `idTag`, `meterStart` (Wh), `timestamp` | `transactionId` (allocated by the CSMS), `idTagInfo.status` | re-authorises the card (and `ConcurrentTx`); `session.start` or `session.reject`; billing `/sessions`; fraud event |
| MeterValues | `connectorId`, `transactionId`, `meterValue[{timestamp, sampledValue[...]}]` | empty | anomaly checks (5.4); metrics |
| StopTransaction | `transactionId`, `meterStop` (Wh), `timestamp`, `reason`, `idTag` | `idTagInfo.status` | `session.stop`; billing `/sessions/{id}/stop`; fraud event |

CSMS → charger: `RemoteStopTransaction{transactionId}`, `ChangeAvailability{connectorId,
type}`, `Reset{type}`. Expected answers: remote-stop `Accepted` (the charger then sends
StopTransaction with `reason=Remote`); change-availability `Accepted`, or `Scheduled`
while a transaction is open (the change applies when it ends); reset `Accepted`, then
StopTransaction (`SoftReset`/`HardReset`), close, reconnect, BootNotification.

Response sequences:
- **Quarantine**: registry `POST /quarantine/{id}`, then CSMS remote-stop (a 409 just
  means nothing was charging), change-availability `Inoperative`, disconnect. The
  reconnect is refused with `ident.quarantined`.
- **Release**: registry `POST /quarantine/{id}/release`; the charger reconnects within
  its backoff, but stays `Unavailable` (chargers remember their availability) until the
  operator sends change-availability `Operative`.

Transaction edge cases:
- A refused StartTransaction still gets a `transactionId` (OCPP requires one); the row is
  stored with `state=rejected`. The charger then sends a StopTransaction for it: the CSMS
  answers normally, logs nothing and raises no `meter.anomaly`. The check uses the
  `sessions` table, so it also holds when the Stop arrives on another replica.
- A charger that reconnects does not resume its session (9.3). At its next
  BootNotification the CSMS closes every `open` session of that charger with
  `stop_reason=PowerLoss` and `meter_stop_wh` = the last known reading, logs
  `session.stop` (`reason=PowerLoss`) and closes it in billing.
- MeterValues or StopTransaction for a transaction that is `open` in `sessions` but not in
  this replica's memory (the charger moved replica without rebooting): the replica
  reloads it from `sessions` (`last_meter_wh`, `last_meter_at`) and carries on.

Any other action is answered by the library with a `NotImplemented` CallError and logged
as `ocpp.invalid` (`reason=not_implemented`). Messages before an accepted
BootNotification are processed but logged once as `ocpp.invalid` (`reason=before_boot`).
Schema violations get a `FormationViolation` CallError from the library
(`ocpp.invalid`, `reason=schema`).

`idTag` is the card id: 8–20 upper-case hex characters (an RFID UID; the roster uses 14,
like a 7-byte NFC UID). Card ids are synthetic in the lab and logged in clear.

Status values: `Available`, `Preparing`, `Charging`, `SuspendedEV`, `SuspendedEVSE`,
`Finishing`, `Reserved`, `Unavailable`, `Faulted`. StopTransaction reasons are the OCPP
enum (`Local`, `Remote`, `EVDisconnected`, `DeAuthorized`, `SoftReset`, `HardReset`,
`Reboot`, `PowerLoss`, `EmergencyStop`, `UnlockCommand`, `Other`).

### 5.4 Meter values

Every `CSMS_METER_INTERVAL` (default 10 s) during a transaction, one `meterValue` with two
sampled values:

```json
{"connectorId": 1, "transactionId": 1042,
 "meterValue": [{"timestamp": "2026-11-06T09:25:10Z", "sampledValue": [
   {"value": "1201234", "context": "Sample.Periodic", "measurand": "Energy.Active.Import.Register", "unit": "Wh"},
   {"value": "21950",   "context": "Sample.Periodic", "measurand": "Power.Active.Import", "unit": "W"}]}]}
```

`Energy.Active.Import.Register` is the connector's cumulative register in Wh: it never
decreases, and `meterStart`/`meterStop` are readings of the same register. Values are
strings as OCPP requires (integers for Wh). The CSMS keeps the open transactions'
last reading in memory (not a DB write per sample) and checks:

| `reason` | Condition |
|---|---|
| `backwards` | register lower than the previous reading (or than `meterStart`) |
| `over_power` | power > `max_kw × 1000 × CSMS_POWER_TOLERANCE` |
| `over_energy` | register delta > `max_kw × Δt_h × 1000 × CSMS_POWER_TOLERANCE + 10` |
| `no_transaction` | `transactionId` not open on this charger |
| `stop_mismatch` | `meterStop` lower than the last reading |

Each hit logs `meter.anomaly` (warning, with `reason`, `transaction_id`, `meter_wh`,
`prev_meter_wh`, `power_w`, `max_kw`, `card`) and is sent to fraud. The message is still
answered normally: the CSMS records, it does not argue with the meter. Under-reporting
(the `--tamper scale` attack) is invisible to these checks on purpose; it is caught by
fraud (peers and the station's feeder meter).

### 5.5 Rate limiting

A token bucket per connection (`CSMS_RATE_LIMIT_PER_S`, `CSMS_RATE_BURST`). A normal
charger sends about 0.15 messages/s. Over the limit, the CSMS answers the CALL with a
`GenericError` CallError ("rate limited") without processing it, and logs
`ocpp.rate_limited` at most once per 10 s per charger (`msg_rate`, `dropped`). A
connection still over the limit after `CSMS_RATE_KICK_AFTER_S` is closed (code 1008,
`conn.kicked`, `reason=rate_limit`). nginx `limit_req` at the edge only sees handshakes,
not frames inside a WebSocket, so flooding over an open connection must be handled here.

### 5.6 Implementation notes (verified with ocpp 2.1.0 and websockets 17.4 on Python 3.13)

- Payload classes are `ocpp.v16.call.BootNotification`, `call_result.BootNotification`
  etc. (no `Payload` suffix); fields are snake_case (`charge_point_vendor`, `id_tag`,
  `meter_start`, `connector_id`, `transaction_id`, `meter_value`, `id_tag_info`).
- Handlers: `@on(Action.boot_notification)` etc. (enum members are snake_case:
  `Action.start_transaction`, `RegistrationStatus.accepted`,
  `AuthorizationStatus.blocked`, `AvailabilityType.inoperative`). Handlers receive
  snake_case kwargs, nested keys included (`meter_value[0]["sampled_value"][0]["measurand"]`);
  they may be sync or async; take `**kwargs` for optional fields.
- `ChargePoint.start()` loops on `recv()` and raises `ConnectionClosedOK`/`ConnectionClosedError`
  when the socket closes. Run it as a task. A handler must never `await self.call(...)`
  on the same charger (the receive loop is busy): send commands from another task.
- `ChargePoint.call()` holds a lock, so one outstanding CALL per direction per charger.
  Pass `suppress=False` to get CallErrors as exceptions.
- To count or rate-limit every incoming message, override `route_message(raw)` and
  call `super().route_message(raw)`.
- `process_request(connection, request)` may be async; return `None` to continue or
  `connection.respond(HTTPStatus.FORBIDDEN, "text\n")` to refuse. State for the handler
  can be attached to `connection` (e.g. `connection.ident = ...`). The handler gets the
  path at `ws.request.path`, headers at `ws.request.headers`, peer at `ws.remote_address`.
- Silence the library loggers (`ocpp` logs every message at INFO); `jsonlog` does this.

---

## 6. JSON log lines

### 6.1 Format and files

- One flat JSON object per line, UTF-8, no nesting, no `null` values (absent instead).
- File: `${LOG_DIR}/<service>-<instance>.json`, e.g. `logs/csms-laptop.json`,
  `/var/log/evcharge/csms-csms-7d9f5-abcde.json`, `logs/sim-pt.porto.st-003.json`. One file
  per process, so replicas sharing a node's hostPath never interleave. Rotated at
  `LOG_MAX_BYTES`.
- The same lines go to stdout (`LOG_STDOUT`).
- Clocks: city containers use the host's clock; the K3s nodes are VMs on the laptop and
  drifted about 2 s from it in Phase 3. Joins across sources (the correlator) use windows of
  tens of seconds, never exact timestamps; VMs should run time synchronisation.
- Wazuh reads `/var/log/evcharge/*.json` with `<log_format>json</log_format>`.
- Library warnings and errors are written too, as `service.log` lines with `logger`.
- Written only through `evcommon.jsonlog` (`setup_logging()` then `log.event(...)`);
  unknown events raise `ValueError` unless registered with `register_events()` (and
  reported as a contract deviation). Run uvicorn with `log_config=None` so its loggers
  go through the same handlers.

### 6.2 Fields

Base fields, always present, in this order:

| Field | Type | Meaning |
|---|---|---|
| `ts` | string | `2026-11-06T09:25:10.123Z` |
| `level` | string | `debug`, `info`, `warning`, `error`, `critical` |
| `service` | string | `csms`, `registry`, `billing`, `fraud`, `correlator`, `sim` |
| `instance` | string | `INSTANCE_ID` (pod name); the station hier-id for the sim |
| `event` | string | vocabulary below |
| `msg` | string | short human text |

Context fields (only when they apply). `city`, `station` and `charger` are filled in
automatically from `hier_id`.

| Field | Type | Field | Type |
|---|---|---|---|
| `hier_id` | string | `transaction_id` | int |
| `city`, `station`, `charger` | string | `connector_id` | int |
| `srcip` | string, real client IP | `ocpp_action` | string |
| `peer` | string, socket peer (edge) | `ocpp_status`, `error_code` | string |
| `edge` | string, edge hier-id | `meter_wh`, `prev_meter_wh`, `energy_wh` | int |
| `other_srcip` | string | `power_w`, `peak_power_w` | float |
| `tls` | bool | `max_kw` | float |
| `cert_cn`, `cert_serial` | string | `duration_s` | float |
| `expected` | string, registry value in a mismatch | `msg_rate` | float |
| `http_status` | int | `dropped` | int |
| `card` | string | `command`, `cmd_id` | string |
| `result` | string, OCPP status or outcome | `operator` | string |
| `reason` | string, machine code | `source`, `incident_id` | string |
| `op` | string, create/update/delete | `rule`, `score` | string, float |
| `vendor`, `model`, `serial`, `firmware` | string | `other_hier_id` | string |
| `dependency` | string | `distance_km`, `speed_kmh` | float |
| `path`, `method` | string | `kind`, `root_cause`, `t_locate_s` | string, string, float |
| `version` | string | `target` | string |
| `logger`, `exc` | string | `cost_eur` | float |
| `cert_verify`, `cert_dn` | string (edge) | `upstream`, `upstream_status` | string (edge) |
| `bytes_out` | int (edge) | `dstip`, `dstport` | string, int (netwatch; Wazuh static `dstip`) |
| `process_name` | string (netwatch) | `ar_command`, `rule_id`, `alert_id` | string (active responses) |

`srcip` is deliberately the Wazuh static field name, so `same_source_ip` frequency rules
and `firewall-drop` work on these logs. The names `action`, `status`, `id`, `url` and
`data` are avoided because the Wazuh JSON decoder treats them as static fields with other
meanings (hence `ocpp_action`, `ocpp_status`, `result`).

### 6.3 Event vocabulary

Wazuh rule blocks (`wazuh/rules`): 100120–139 hijacking,
100140–159 fraud, 100160–179 botnet, 100180–199 infrastructure.

| Event | Service | Level | Key fields | Used for |
|---|---|---|---|---|
| `service.start` / `service.stop` | all | info | `version` | infra |
| `service.log` | all | warning+ | `logger`, `exc` | infra |
| `dependency.error` | all | error | `dependency`, `reason` | infra |
| `api.auth_failed` | all HTTP | warning | `srcip`, `path`, `method` | infra (token guessing) |
| `conn.open` | csms | info | `hier_id`, `srcip`, `peer`, `edge`, `tls`, `cert_cn`, `cert_serial` | baseline, botnet (reconnect storms) |
| `conn.close` | csms | info | `hier_id`, `srcip`, `duration_s`, `reason` | flapping |
| `conn.replaced` | csms | info | `hier_id`, `srcip`, `other_srcip` | hijacking context |
| `conn.kicked` | csms | warning | `hier_id`, `srcip`, `reason` (`rate_limit`, `quarantine`, `command`) | botnet |
| `ident.bad_id` | csms | warning | `srcip`, `path` | hijacking (scanning) |
| `ident.unknown` | csms | warning | `hier_id`, `srcip` | hijacking (scanning, frequency) |
| `ident.quarantined` | csms | warning | `hier_id`, `srcip` | hijacking (retry after quarantine) |
| `ident.no_cert` | csms | warning | `hier_id`, `srcip`, `edge` | hijacking (no TLS in a TLS city) |
| `ident.cert_mismatch` | csms | warning | `hier_id`, `srcip`, `cert_cn` | hijacking (T0849 masquerading) |
| `ident.cert_serial_mismatch` | csms | warning | `hier_id`, `srcip`, `cert_serial`, `expected` | hijacking (revoked/reissued cert) |
| `ident.city_mismatch` | csms | warning | `hier_id`, `srcip`, `edge` | hijacking (Porto id on Aveiro's network) |
| `ident.ip_mismatch` | csms | warning | `hier_id`, `srcip`, `expected` | hijacking (rogue host in the right city) |
| `ident.untrusted_peer` | csms | warning | `srcip` | hijacking (bypassing the edge) |
| `ident.spoofed_header` | csms | warning | `srcip`, `reason` (header name) | hijacking (header forgery) |
| `charger.duplicate_id` | csms | warning | `hier_id`, `srcip`, `other_srcip` | hijacking (the main signal) |
| `charger.boot` | csms | info | `hier_id`, `vendor`, `model`, `serial`, `firmware` | baseline |
| `charger.boot_mismatch` | csms | warning | `hier_id`, `serial`, `expected` | hijacking |
| `charger.status` | csms | info | `hier_id`, `connector_id`, `ocpp_status`, `error_code` | map, flapping |
| `charger.fault` | csms | warning | `hier_id`, `connector_id`, `error_code` | infra |
| `ocpp.invalid` | csms | warning | `hier_id`, `srcip`, `ocpp_action`, `reason` | hijacking / protocol abuse |
| `ocpp.rate_limited` | csms | warning | `hier_id`, `srcip`, `msg_rate`, `dropped` | botnet (flood) |
| `auth.accept` | csms | info | `hier_id`, `card`, `result` | fraud baseline |
| `auth.reject` | csms | warning | `hier_id`, `card`, `result` (`Blocked`, `Invalid`, `Expired`, `ConcurrentTx`) | fraud (blocked or cloned card) |
| `session.start` | csms | info | `hier_id`, `card`, `transaction_id`, `connector_id`, `meter_wh` | fraud (travel), botnet (surge frequency per city) |
| `session.reject` | csms | warning | `hier_id`, `card`, `transaction_id`, `result` | fraud |
| `session.stop` | csms | info | `hier_id`, `card`, `transaction_id`, `meter_wh`, `energy_wh`, `duration_s`, `reason` (`PowerLoss` for orphans closed at boot) | fraud |
| `meter.anomaly` | csms | warning | `hier_id`, `card`, `transaction_id`, `reason`, `meter_wh`, `prev_meter_wh`, `power_w`, `max_kw` | fraud (tamper), hijacking (MitM rewriting) |
| `command.sent` | csms | info | `hier_id`, `command`, `cmd_id`, `source`, `incident_id` | response audit |
| `command.result` | csms | info | `hier_id`, `command`, `cmd_id`, `result` | response audit |
| `registry.write` | registry | info | `hier_id`, `op`, `operator` | audit |
| `registry.write_denied` | registry | warning | `hier_id`, `operator`, `srcip` | delegation abuse (T1078) |
| `registry.quarantine` / `registry.release` | registry | warning / info | `hier_id`, `reason`, `source`, `operator`, `incident_id` | response audit |
| `billing.session_closed` | billing | info | `transaction_id`, `card`, `hier_id`, `energy_wh`, `cost_eur` | accounting |
| `billing.session_flagged` | billing | warning | `transaction_id`, `card`, `reason`, `source` | fraud response |
| `billing.card_blocked` / `billing.card_unblocked` | billing | warning / info | `card`, `reason`, `source`, `incident_id` | fraud response |
| `fraud.alert` | fraud | warning | `rule`, `score`, `card`, `hier_id`, `other_hier_id`, `distance_km`, `speed_kmh`, `energy_wh`, `max_kw` | fraud |
| `fraud.action` | fraud | warning | `rule`, `target`, `result` | fraud response |
| `incident.open` / `incident.update` / `incident.close` | correlator | warning / info | `incident_id`, `kind`, `root_cause`, `hier_id`, `srcip`, `card`, `t_locate_s` | correlation |
| `edge.access` | edge (nginx) | info / warning (4xx) / error (5xx) | `hier_id`, `srcip`, `http_status`, `tls`, `cert_verify`, `cert_dn`, `cert_serial`, `upstream`, `upstream_status`, `duration_s`, `bytes_out` | hijacking (revoked / foreign certs, scanning), botnet (handshake floods, 429) |
| `host.new_outbound` | netwatch (station) | warning | `hier_id` (station), `dstip`, `dstport`, `process_name` | botnet (C2 beacon) |
| `ar.action` | ar (Wazuh manager) | info / warning (refused) / error | `ar_command`, `target`, `hier_id` or `card`, `srcip`, `rule_id`, `alert_id`, `result`, `reason` | response audit |
| `ar.evidence` | ar (Wazuh manager) | info | `ar_command`, `target`, `kind`, `http_status`, `detail` | kept before an action (post-mortem) |
| `sim.start` | sim | info | `hier_id` (station), `version` | |
| `sim.connect` / `sim.disconnect` | sim | info / warning | `hier_id`, `reason` | |
| `sim.rejected` | sim | warning | `hier_id`, `http_status` | |
| `sim.command` | sim | info | `hier_id`, `command`, `result` | |
| `sim.auth_rejected` | sim | info | `hier_id`, `card`, `result` | |
| `sim.session_start` / `sim.session_stop` | sim | info | `hier_id`, `card`, `transaction_id`, `energy_wh` | |

Example:
```json
{"ts":"2026-11-06T10:02:41.207Z","level":"warning","service":"csms","instance":"csms-7d9f5-abcde","event":"charger.duplicate_id","msg":"charger id already connected from another address","hier_id":"pt.porto.st-003.cp-2","srcip":"10.31.0.66","other_srcip":"10.31.0.13","edge":"pt.porto.edge","http_status":409,"city":"porto","station":"st-003","charger":"cp-2"}
```

The simulator's attack modes (`--compromised`, `--tamper`, `--clone-card`) never log
what they do: a compromised station controller does not report itself.

---

## 7. Prometheus metrics

### 7.1 Conventions

- Prefix `evcharge_<service>_`, base units in the name (`_watts`, `_wh`, `_seconds`,
  `_eur`), counters end in `_total`. `*_created` series are disabled
  (`metrics.init_exposition()`).
- Labels come only from: `hier_id`, `city`, `station`, `charger`, plus the bounded enums
  listed per metric (`action`, `direction`, `result`, `reason`, `kind`, `outcome`,
  `command`, `dependency`, `operator`, `state`, `source`, `rule`, `event`, `status`).
  Never a card id, IP, transaction id or free text as a label.
- `hier_id` on a per-station or per-charger metric is the id of that station or charger.
  Per-city metrics carry only `city`; alert rules add `hier_id="pt.<city>"` themselves.
- Scope: **per_charger** series exist only in the city Prometheus; **per_station** series
  are bounded by the number of stations and may go central; **aggregate** series are
  bounded by small enums.
- `evcommon.metrics` holds every spec; services create metrics with `metrics.make(SPEC)`.
- The CSMS runs as several replicas: its gauges are per replica and dashboards use
  `sum by (...)`.

### 7.2 Metrics by service

**csms** (scraped by the central Prometheus):

| Name | Type | Labels | Scope |
|---|---|---|---|
| `evcharge_csms_connected_chargers` | gauge | `hier_id`, `city`, `station` | per_station |
| `evcharge_csms_connections_total` | counter | `city`, `result` (`accepted`, `rejected`) | aggregate |
| `evcharge_csms_identity_rejections_total` | counter | `city`, `reason` (`bad_id`, `unknown`, `quarantined`, `no_cert`, `cert_mismatch`, `cert_serial_mismatch`, `city_mismatch`, `ip_mismatch`, `untrusted_peer`, `duplicate_id`) | aggregate |
| `evcharge_csms_duplicate_id_total` | counter | `city`, `outcome` (`rejected`, `replaced`) | aggregate |
| `evcharge_csms_ocpp_messages_total` | counter | `city`, `action`, `direction` (`in`, `out`) | aggregate |
| `evcharge_csms_ocpp_errors_total` | counter | `city`, `kind` (`schema`, `not_implemented`, `before_boot`, `call_error`, `timeout`) | aggregate |
| `evcharge_csms_ocpp_handle_seconds` | histogram | `action` | aggregate |
| `evcharge_csms_rate_limited_total` | counter | `city` | aggregate |
| `evcharge_csms_card_auth_total` | counter | `city`, `result` | aggregate |
| `evcharge_csms_sessions_active` | gauge | `hier_id`, `city`, `station` | per_station |
| `evcharge_csms_sessions_started_total` | counter | `city` | aggregate |
| `evcharge_csms_sessions_stopped_total` | counter | `city`, `reason` | aggregate |
| `evcharge_csms_energy_wh_total` | counter | `hier_id`, `city`, `station` | per_station |
| `evcharge_csms_power_watts` | gauge | `hier_id`, `city`, `station` | per_station |
| `evcharge_csms_meter_anomalies_total` | counter | `city`, `reason` | aggregate |
| `evcharge_csms_commands_total` | counter | `command`, `result` | aggregate |
| `evcharge_csms_dependency_errors_total` | counter | `dependency` | aggregate |
| `evcharge_csms_dependency_seconds` | histogram | `dependency` | aggregate |
| `evcharge_csms_events_dropped_total` | counter | — | aggregate |

(`city` is `unknown` when the id could not be parsed.)

**registry**: `evcharge_registry_nodes` (gauge; `city`, `kind`, `state`),
`evcharge_registry_station_info` (gauge = 1; `hier_id`, `city`, `station`, `lat`, `lon`:
the Geomap's coordinates), `evcharge_registry_writes_total` (`operator`, `result` =
`ok|denied|invalid|not_found`), `evcharge_registry_quarantines_total` (`city`, `action`
= `quarantine|release`).

**billing** (Phase 2): `evcharge_billing_authorize_total` (`result`),
`evcharge_billing_sessions_total` (`city`, `status` = `opened|closed|flagged`),
`evcharge_billing_energy_wh_total` (`city`), `evcharge_billing_revenue_eur_total`
(`city`), `evcharge_billing_blocked_cards` (gauge), `evcharge_billing_card_blocks_total`
(`source`).

**fraud** (Phase 7): `evcharge_fraud_events_total` (`event`), `evcharge_fraud_alerts_total`
(`city`, `rule`), `evcharge_fraud_actions_total` (`action`, `result`),
`evcharge_fraud_high_risk_cards` (gauge). Names and labels are fixed in
`evcommon.metrics`; the fraud builder adds no new names without a contract change.

**correlator** (Phase 9): `evcharge_correlator_signals_total` (`source`),
`evcharge_correlator_incidents_open` (gauge), `evcharge_correlator_incidents_total`
(`kind`), `evcharge_correlator_time_to_locate_seconds` and
`evcharge_correlator_time_to_mitigate_seconds` (histograms). Fixed in `evcommon.metrics`.

**sim** (one endpoint per station; city Prometheus only):

| Name | Type | Labels | Scope |
|---|---|---|---|
| `evcharge_sim_charger_connected` | gauge 0/1 | `hier_id`, `city`, `station`, `charger` | per_charger |
| `evcharge_sim_charger_status` | gauge, code | same | per_charger |
| `evcharge_sim_power_watts` | gauge | same | per_charger |
| `evcharge_sim_energy_register_wh` | gauge | same | per_charger |
| `evcharge_sim_ocpp_messages_sent_total` | counter | same + `action` | per_charger |
| `evcharge_sim_reconnects_total` | counter | same | per_charger |
| `evcharge_sim_sessions_total` | counter | same + `result` (`completed`, `rejected`, `interrupted`) | per_charger |
| `evcharge_sim_ocpp_call_seconds` | histogram | `hier_id`, `city`, `station`, `action` | per_station |
| `evcharge_sim_station_grid_power_watts` | gauge | `hier_id`, `city`, `station` | per_station |

Status codes: Available 0, Preparing 1, Charging 2, SuspendedEV 3, SuspendedEVSE 4,
Finishing 5, Reserved 6, Unavailable 7, Faulted 8 (`metrics.CHARGER_STATUS_CODES`).
`evcharge_sim_station_grid_power_watts` models the utility's feeder meter at the station:
the true power drawn, unaffected by `--tamper`. Power and energy values reported to the
CSMS are the (possibly tampered) charger values. The sim metrics are the station's own
telemetry: detection rules prefer the CSMS and edge views, which a compromised station
cannot forge.

The scrape config must not add target labels named `hier_id`, `city`, `station` or
`charger` that clash with the exporter's (use `honor_labels: true` for the sim job).

**edge** (Phase 3): the standard `nginx-prometheus-exporter` metrics (`nginx_connections_active`,
`nginx_http_requests_total`, …) and node exporter; the JSON access log feeds Wazuh.

### 7.3 Recording rules federated from each city

Each city Prometheus evaluates these every 5 s (rule group `city`, file
`monitoring/city/city.rules.yml`); the central Prometheus federates only
`{__name__=~"city:.*"}` every 30 s. Station-level rules carry the station's hier-id, built
with `STATION(e) = label_replace(label_join(e, "hier_id", ".", "city", "station"),
"hier_id", "pt.$1", "hier_id", "(.+)")`.

| Rule | Labels | Expression |
|---|---|---|
| `city:chargers_connected:sum` | `city` | `sum by (city) (evcharge_sim_charger_connected)` |
| `city:station_chargers:count` | `city`, `station`, `hier_id` | `STATION(count by (city, station) (evcharge_sim_charger_connected))` |
| `city:station_chargers_connected:sum` | same | `STATION(sum by (city, station) (evcharge_sim_charger_connected))` |
| `city:station_chargers_charging:sum` | same | `STATION(sum by (city, station) (evcharge_sim_charger_status == bool 2))` |
| `city:station_chargers_faulted:sum` | same | `STATION(sum by (city, station) (evcharge_sim_charger_status == bool 8))` |
| `city:power_watts:sum` | `city` | `sum by (city) (evcharge_sim_power_watts)` (reported by chargers) |
| `city:grid_power_watts:sum` | `city` | `sum by (city) (evcharge_sim_station_grid_power_watts)` (feeder meters: the truth) |
| `city:station_power_watts:sum` | `city`, `station`, `hier_id` | `STATION(sum by (city, station) (evcharge_sim_power_watts))` |
| `city:station_grid_power_watts:sum` | same | `sum by (city, station, hier_id) (evcharge_sim_station_grid_power_watts)` |
| `city:station_power_gap_watts` | same | `city:station_grid_power_watts:sum - on (hier_id) group_left () city:station_power_watts:sum` |
| `city:ocpp_messages_sent:rate1m` | `city` | `sum by (city) (rate(evcharge_sim_ocpp_messages_sent_total[1m]))` |
| `city:station_ocpp_messages_sent:rate1m` | `city`, `station`, `hier_id` | `STATION(sum by (city, station) (rate(evcharge_sim_ocpp_messages_sent_total[1m])))` |
| `city:charger_msg_rate:max1m` | `city` | `max by (city) (sum by (city, hier_id) (rate(evcharge_sim_ocpp_messages_sent_total[1m])))` |
| `city:reconnects:rate5m` | `city` | `sum by (city) (rate(evcharge_sim_reconnects_total[5m]))` |
| `city:station_up:min` | `city`, `station`, `hier_id` | `min by (city, station, hier_id) (up{job="stations"})` |
| `city:edge_up:min` | `city` | `min by (city) ((up{job="edge-nginx"} * on (instance) group_left () nginx_up) or up{job="edge-nginx"})` |
| `city:edge_ws_connections:sum` | `city` | `sum by (city) (nginx_connections_active)` |
| `city:edge_requests:rate1m` | `city` | `sum by (city) (rate(nginx_http_requests_total[1m]))` |

The nginx exporter reads `stub_status`, which has no status codes: handshakes refused at
the edge (400, 429, ...) are counted from the edge log by Wazuh, not by Prometheus.
City Prometheus external label: `site="pt.<city>"`. The list of names is
`evcommon.metrics.CITY_RECORDING_RULES`.

---

## 8. Storage

### 8.1 Engine and URL

SQLAlchemy 2 with the **sync** engine for every service (FastAPI sync endpoints run in its
thread pool; the asyncio CSMS calls the DB with `asyncio.to_thread`, never on the
MeterValues hot path). `DATABASE_URL` selects the backend:

- local: `sqlite:///./data/<service>.db` (rollback journal, `busy_timeout` 5 s; WAL off so
  no extra files appear next to the ignored `*.db`);
- K3s: **one** Postgres StatefulSet (one PVC) with one database **and one role** per
  service: `postgresql+psycopg://<service>:<password>@postgres:5432/<service>` for
  `registry`, `csms`, `billing`. Each role owns only its database and has no rights on the
  others, so a compromised billing pod cannot read CSMS data. Passwords:
  `PG_PASSWORD_REGISTRY`, `PG_PASSWORD_CSMS`, `PG_PASSWORD_BILLING` (superuser:
  `POSTGRES_PASSWORD`, used only by the init script) in `secrets/platform.env`. The init
  script creates roles and databases only on an empty volume; changing a password later
  needs `ALTER ROLE` or a fresh PVC. Each service's Secret holds its full `DATABASE_URL`.
  `evcommon.config.database_url()` rewrites `postgresql://` to the psycopg 3 driver.
  Engines use a small pool (`pool_size=5`, `max_overflow=5`, `pool_pre_ping=True`).

Tables are created at startup with `metadata.create_all()`, followed by
`evcommon.dbschema.add_missing_columns(engine, metadata)`: a column added in the code is
added to an existing table if it is nullable or has a server default, and logged once
(`service.start` lists the added columns). There is no other migration tool: renames, type
changes, new constraints or a new NOT NULL column without default need a manual `ALTER`
(written down in the service README) or a fresh database. Types are portable: `TEXT`,
`INTEGER`/`BIGINT`, `DOUBLE PRECISION`/`REAL`, `NUMERIC(10,2)`, `BOOLEAN`,
`TIMESTAMP WITH TIME ZONE` (stored UTC).

### 8.2 Registry

```
nodes
  hier_id          TEXT PRIMARY KEY
  kind             TEXT NOT NULL            city|station|charger|edge|central
  parent           TEXT NULL  -> nodes.hier_id   (indexed)
  city             TEXT NULL                (indexed)
  station          TEXT NULL
  charger          TEXT NULL
  name             TEXT NULL
  lat, lon         DOUBLE NULL
  max_kw           DOUBLE NULL
  connectors       INTEGER NULL
  vendor, model, serial, cert_serial   TEXT NULL
  host, ip, network, dns, metrics_target   TEXT NULL   (ip indexed)
  state            TEXT NOT NULL DEFAULT 'active'   active|quarantined|retired
  state_reason, state_source   TEXT NULL
  state_changed_at TIMESTAMPTZ NULL
  created_at, updated_at   TIMESTAMPTZ NOT NULL
  updated_by       TEXT NOT NULL            admin | <city>

quarantine_log
  id           INTEGER PRIMARY KEY (identity)
  hier_id      TEXT NOT NULL
  action       TEXT NOT NULL                quarantine|release
  reason, source, operator, incident_id   TEXT
  at           TIMESTAMPTZ NOT NULL
```

### 8.3 CSMS

```
connections                                  one row per open charger connection
  hier_id      TEXT PRIMARY KEY              (the unique key is the cross-replica duplicate guard)
  city, station TEXT NOT NULL
  pod          TEXT NOT NULL                 INSTANCE_ID of the replica holding the socket
  src_ip       TEXT NOT NULL
  edge         TEXT NULL
  cert_cn, cert_serial TEXT NULL
  tls          BOOLEAN NOT NULL
  since        TIMESTAMPTZ NOT NULL
  last_seen    TIMESTAMPTZ NOT NULL          updated at most every 30 s per charger
  booted       BOOLEAN NOT NULL DEFAULT false
  status       TEXT NULL

csms_pods
  pod          TEXT PRIMARY KEY
  started_at, heartbeat_at TIMESTAMPTZ NOT NULL   heartbeat every 5 s

sessions
  transaction_id  INTEGER PRIMARY KEY (identity / autoincrement)   the OCPP transactionId
  hier_id         TEXT NOT NULL (indexed)
  connector_id    INTEGER NOT NULL
  card            TEXT NOT NULL (indexed)
  auth_status     TEXT NOT NULL               Accepted|Blocked|...
  meter_start_wh  BIGINT NOT NULL
  last_meter_wh   BIGINT NULL                 written every CSMS_METER_FLUSH_S and at stop
  last_meter_at   TIMESTAMPTZ NULL            charger timestamp of last_meter_wh
  meter_stop_wh   BIGINT NULL
  peak_power_w    DOUBLE NULL
  started_at      TIMESTAMPTZ NOT NULL
  stopped_at      TIMESTAMPTZ NULL
  stop_reason     TEXT NULL
  state           TEXT NOT NULL               open|closed|rejected

commands
  cmd_id        TEXT PRIMARY KEY              uuid4
  hier_id       TEXT NOT NULL
  command       TEXT NOT NULL                 remote-stop|change-availability|reset|disconnect|probe
  args          TEXT NOT NULL                 JSON
  source, reason, incident_id TEXT NULL
  requested_at  TIMESTAMPTZ NOT NULL
  target_pod    TEXT NULL
  status        TEXT NOT NULL                 pending|done|timeout
  result        TEXT NULL                     CommandStatus
  detail        TEXT NULL
  done_at       TIMESTAMPTZ NULL
```

Rules shared by SQLite (one process) and Postgres (several replicas):
- **Startup**: a replica upserts its `csms_pods` row and deletes only the `connections`
  rows whose `pod` is its own `INSTANCE_ID` (left from a crash). It never closes sessions
  at startup (other replicas may hold them; orphans are closed at the charger's next
  BootNotification, 5.3). K8s pod names are unique, so a restarted pod has a new id: its
  predecessor's rows go stale instead.
- **Liveness**: every `CSMS_POD_HEARTBEAT_S` the replica updates `heartbeat_at`. A
  `connections` row whose pod is missing from `csms_pods` or silent for more than
  `CSMS_POD_STALE_S` is stale: the duplicate check deletes it and accepts the newcomer.
- **Duplicate guard**: a connection is registered with a plain `INSERT` (never delete +
  insert). A primary-key violation means the id is connected somewhere: the CSMS probes
  the owner (locally, or with the `probe` command through the bus), then refuses (409) or
  deletes the dead row and inserts again.
- **Meter flush**: every `CSMS_METER_FLUSH_S` one batched `UPDATE` writes `last_meter_wh`,
  `last_meter_at` and `peak_power_w` of the replica's open transactions (bounded loss after
  a crash; never one write per MeterValues).
- **ConcurrentTx** is decided with `SELECT ... WHERE card = ? AND state = 'open'` on
  `sessions` (all replicas), not with one replica's memory.

### 8.4 Billing (Phase 2)

```
tariffs       tariff_id TEXT PK, price_per_kwh_eur NUMERIC(8,4), price_per_min_eur NUMERIC(8,4),
              session_fee_eur NUMERIC(8,2), currency TEXT
cards         card TEXT PK, holder TEXT, home_city TEXT, tariff_id TEXT -> tariffs,
              status TEXT (active|blocked|expired), expiry_date TIMESTAMPTZ NULL, created_at TIMESTAMPTZ
blocked       id PK, card TEXT (indexed), reason TEXT, source TEXT, incident_id TEXT NULL,
              blocked_at TIMESTAMPTZ, unblocked_at TIMESTAMPTZ NULL, unblocked_by TEXT NULL
              (history; the current state is cards.status)
transactions  transaction_id INTEGER PK (from the CSMS), card TEXT (indexed), hier_id TEXT (indexed),
              city TEXT, connector_id INTEGER, tariff_id TEXT, started_at, stopped_at TIMESTAMPTZ NULL,
              meter_start_wh BIGINT, meter_stop_wh BIGINT NULL, energy_wh BIGINT NULL,
              cost_eur NUMERIC(10,2) NULL, status TEXT (open|closed|flagged), flag_reason TEXT NULL,
              stop_reason TEXT NULL
```

The CSMS owns the transaction id (OCPP needs it in StartTransaction.conf even when
billing is slow); billing is keyed by it.

### 8.5 Command channel

The CSMS hides delivery behind a small `CommandBus` interface:
`await bus.execute(hier_id, command, args, meta) -> CommandResult`.

- **Phase 1 (`LocalBus`)**: one process. The bus finds the live `ChargePoint` in an
  in-memory dict and calls it directly; the `commands` row is written for the audit.
- **Phase 2 (`PgNotifyBus`)**: the API request may land on any replica.
  1. Insert the `commands` row (`status=pending`, `target_pod` from `connections.pod`).
  2. If `target_pod` is this replica, execute locally. Otherwise
     `NOTIFY csms_cmd, '{"cmd_id": "...", "pod": "<target_pod>"}'` (payload < 8000 bytes).
  3. Every replica `LISTEN csms_cmd` on a dedicated psycopg 3 connection; the target loads
     the row, sends the OCPP CALL, updates the row (`status=done`, `result`, `done_at`)
     and sends `NOTIFY csms_cmd_done, '<cmd_id>'`.
  4. The requesting replica waits on `csms_cmd_done` (or polls the row every 200 ms) up to
     `CSMS_CMD_TIMEOUT_S`, then answers `Timeout`.
  The internal `probe` command (WebSocket ping) uses the same path for the cross-replica
  duplicate check of section 5.2.

---

## 9. Simulator

One process = one station: it runs every charger of that station, each with its own
OCPP connection and charge-point id. In deployment it runs in the station's container;
locally, `dev/run_local.sh` starts one process per station.

### 9.1 Roster (`dev/roster.yaml`)

Loaded with `evcommon.roster.load_roster()` (validated by pydantic). Chargers take the
station's coordinates.

```yaml
version: 1
country: pt
cities:
  - city: porto
    name: Porto
    plain_ws: false            # true for the legacy city (aveiro)
    edge_lat: 41.1579          # optional, for the map
    edge_lon: -8.6291
    stations:
      - station: st-001
        name: Porto - Avenida dos Aliados
        lat: 41.1496
        lon: -8.6109
        dev_metrics_port: 9301  # local dev only; containers use 9300
        chargers:
          - {charger: cp-1, max_kw: 22, connectors: 1, vendor: EVLab, model: EVL-AC22, serial: EVL22-0001}
          - {charger: cp-2, max_kw: 50, connectors: 1, vendor: EVLab, model: EVL-DC50, serial: EVL50-0002}
cards:
  - {card: 04A1B2C3D4E5F6, holder: driver-0001, home_city: porto, tariff: standard, blocked: false}
```

### 9.2 Command line

```
python -m evsim --roster dev/roster.yaml --station pt.porto.st-003 [options]
```

| Flag | Default | Meaning |
|---|---|---|
| `--roster PATH` | `$SIM_ROSTER` | roster file |
| `--station ID` | `$SIM_STATION` | station hier-id to run |
| `--chargers cp-1,cp-2` | all | run only these chargers (a rogue device runs one) |
| `--csms-url URL` | `$SIM_CSMS_URL` or `ws://127.0.0.1:9000/ocpp` | base URL; `/<charger hier-id>` is appended |
| `--pki-dir DIR` | `$SIM_PKI_DIR` | for `wss://`: `ca.crt`, `<charger hier-id>.crt`, `<charger hier-id>.key`. A directory with only `ca.crt` verifies the server and presents no client certificate |
| `--metrics-host` / `--metrics-port` | `127.0.0.1` / `9300` (`0` disables) | containers use `0.0.0.0` |
| `--log-dir DIR` | `$LOG_DIR` or `./logs` | JSON log, `sim-<station>.json` |
| `--meter-interval S` | `10` | MeterValues period |
| `--arrival-rate N` | `6` | driver arrivals per connector per hour (Poisson) |
| `--session-minutes M` | `8` | mean session length (lab-accelerated) |
| `--home-bias P` | `0.8` | probability the driver's card is from this city |
| `--seed N` | random | RNG seed |
| `--duration S` | run forever | stop cleanly after S seconds |
| `--reconnect-max S` / `--reject-backoff S` | `30` / `60` | reconnect backoff cap; wait after a refused handshake |
| `--compromised` | off | botnet client (9.4) |
| `--c2-url URL` | — | required with `--compromised` |
| `--beacon-interval S` | `15` | |
| `--tamper F` | off | under-report: reported power and energy deltas × F (0 < F < 1) |
| `--tamper-mode` | `scale` | `scale`, `rollback` (register jumps back 500 Wh once per session), `freeze` (register stops after half the session) |
| `--tamper-chargers cp-1` | all | which chargers tamper |
| `--clone-card CARD` | off | the next sessions here use this card (cloned) |
| `--clone-sessions N` | `3` | how many |

Exit codes: 0 clean stop, 2 bad arguments or roster. SIGINT/SIGTERM: stop open
transactions (`reason=Reboot`), close connections, exit 0.

### 9.3 Charger and driver model

Per charger:
1. Connect (subprotocol `ocpp1.6`) → BootNotification → StatusNotification for connector
   0 and each connector (`Available`, or `Unavailable` if set Inoperative) → Heartbeat
   every `interval` from the Boot reply.
2. Per connector, idle time to the next driver is exponential with rate `--arrival-rate`.
   The driver's card is drawn from the roster (`--home-bias` from this city, else any
   city; blocked cards stay in the pool and produce realistic rejections).
3. `Preparing` → Authorize. Not `Accepted`: `Finishing` → `Available`
   (`sim.auth_rejected`, `sessions_total{result="rejected"}`).
4. StartTransaction (`meterStart` = register). If the answer is not `Accepted`: stop at
   once with `reason=DeAuthorized`.
5. `Charging`. Car power limit drawn from {7.4, 11, 22, 50, 100, 150} kW; power =
   min(car, `max_kw`) × U(0.90, 1.00), re-drawn ±2 % each sample. The register grows by
   power × elapsed time; MeterValues every `--meter-interval`.
6. Session length: log-normal with mean `--session-minutes` (fractions allowed), clipped to [0.25 × mean, 4 × mean]
   minutes. Then StopTransaction (`Local`, or `EVDisconnected` 20 % of the time) →
   `Finishing` (2 s) → `Available`.
7. Registers start at a random value in [0, 1 000 000] Wh and persist for the process.
8. Commands: RemoteStopTransaction → `Accepted`, stop with `reason=Remote`.
   ChangeAvailability `Inoperative` → `Accepted` (or `Scheduled` while charging),
   status `Unavailable`, no new drivers; `Operative` → back to `Available`.
   Reset → `Accepted`, stop the transaction (`SoftReset`/`HardReset`), close, reconnect.
9. Reconnect with exponential backoff 1 s → `--reconnect-max` plus jitter. A refused
   handshake (HTTP 4xx) logs `sim.rejected` and waits `--reject-backoff`. A lost connection
   interrupts the charger's sessions without a StopTransaction (a simplification: real
   chargers keep charging offline and report later); the CSMS closes them at the next
   BootNotification (5.3). The availability set by ChangeAvailability is kept across
   reconnects.
10. OCPP call timeout 10 s.

Timing: with the default `--session-minutes 8` a session lasts about 8 minutes, so checks
that count completed sessions per city need a soak of at least 15 minutes. Demos and
short tests pass a smaller value (e.g. `--session-minutes 2`).

### 9.4 Attack modes

- `--compromised` (botnet; the whole station controller is infected): keeps one
  persistent HTTP connection to `--c2-url` and beacons every `--beacon-interval`:
  `POST <c2>/beacon` with `{"bot": "<station hier-id>", "chargers": [...], "ts": "..."}`.
  The answer is a task:
  - `{"task": "idle"}`
  - `{"task": "flood", "rate_hz": 20, "duration_s": 60}`: every charger sends MeterValues
    at `rate_hz` for `duration_s`.
  - `{"task": "surge", "duration_s": 300}`: every idle connector starts a session at once
    at full `max_kw` (random roster card) and holds it for `duration_s`.
  - `{"task": "stop"}`: back to normal behaviour.
  The C2 server itself belongs to the botnet PoC (`poc/botnet`, Phase 8).
- `--tamper F`: under-reports to the CSMS; the feeder meter metric keeps the truth.
- `--clone-card CARD`: the next `--clone-sessions` drivers here present `CARD`.

### 9.5 Metrics

Section 7.2 (sim). Served at `http://<metrics-host>:<metrics-port>/metrics`.

---

## 10. Packaging and run conventions

- Each service is a Python package with a src layout: `<dir>/pyproject.toml`,
  `<dir>/src/<module>/`, `<dir>/tests/`. Modules: `evcommon` (common), `evregistry`,
  `evcsms`, `evsim`, `evbilling`, `evfraud`, `evcorrelator`.
- Start command: `python -m <module>`. Configuration comes only from the environment
  (the sim also takes flags).
- Dependencies: only from fastapi, uvicorn, pydantic, sqlalchemy, psycopg (Phase 2 extra
  `pg`), prometheus-client, ocpp, websockets, pyyaml, httpx, pytest, plus `evcommon`.
  Tested versions are pinned in `/constraints.txt`. Python ≥ 3.13.
- Local dev: one venv at the repo root (`.venv`), e.g.
  `pip install -c constraints.txt -e common -e registry -e csms -e sim`.
- Containers (Phase 2): `python:3.13-slim`, non-root uid 10001, `HEALTHCHECK` on `/health`,
  build context = repo root (to include `common/`), `LOG_DIR=/var/log/evcharge`,
  `BIND_HOST=0.0.0.0`. Pods: `enableServiceLinks: false`, `INSTANCE_ID` from
  `metadata.name`, logs on a hostPath `/var/log/evcharge` read by the Wazuh agent.
- Images are built inside the Incus instance `docker` and pushed to the lab registry:
  `docker.incus:5000/evcharge-<service>:<tag>` (`registry`, `csms`, `billing`, `sim`), tag =
  build time `YYYYMMDD-HHMM`, plus `latest`. Third-party images are mirrored there too
  (`docker.incus:5000/postgres:17-alpine`), so the cluster pulls only from the lab
  registry. K3s nodes reach it through `/etc/rancher/k3s/registries.yaml` (plain http, lab
  shortcut).
- K8s objects (namespace `evcharge`): Deployments `registry`, `csms`, `billing`;
  StatefulSet `postgres`; Services as in 2.2; Secrets `registry-env`, `csms-env`,
  `billing-env`, `postgres-env`; ConfigMap `roster` (from `dev/roster.yaml`, mounted at
  `/etc/evcharge/roster.yaml`, used by `BILLING_SEED_ROSTER`). Labels `app.kubernetes.io/name`
  = the service, `app.kubernetes.io/part-of=evcharge`.

---

## 11. Edge gateway

One nginx per city in the Incus instance `<city>-edge` (`pt.<city>.edge`,
`edge.<city>.pt.lab`). It terminates the chargers' TLS, checks their certificates, adds
the identity headers and forwards to the CSMS NodePort. Source: `gateway/`.

### 11.1 Listeners

| City mode | Port | Behaviour |
|---|---|---|
| TLS (every city not in `PLAIN_WS_CITIES`) | 443 | `wss://`. Server certificate `pt.<city>.edge`; `ssl_client_certificate ca.crt`, `ssl_verify_client optional`, `ssl_verify_depth 1`, `ssl_crl crl.pem`; TLS 1.2 and 1.3 |
| TLS | 80 | `return 426` ("use wss"), logged |
| plain (`aveiro` at first) | 80 | `ws://`, no client certificate; the certificate headers are empty |
| both | 127.0.0.1:8080 | `stub_status` at `/stub_status` |

Switching a city from plain to TLS = re-render the edge with `--tls` and point its
stations at `wss://` (`SIM_CSMS_URL`, section 13).

### 11.2 Requests

- Only `^/ocpp/pt\.[a-z0-9-]+\.st-[0-9]{3}\.cp-[0-9]{1,2}$` is forwarded; any other path →
  404. A request without `Upgrade: websocket` → 426. The edge does **not** check that the
  id belongs to its own city: the CSMS does (`ident.city_mismatch`), so the attempt is
  logged centrally.
- Headers: exactly the table of section 5.2 (`X-Real-IP`, `X-Client-Cert-DN`,
  `X-Client-Cert-Serial`, `X-Client-Cert-Verify`, `X-Edge-Id: pt.<city>.edge`), set with
  `proxy_set_header`, which replaces any value the client sent. Plus `Host`, `Upgrade`,
  `Connection: upgrade`, `proxy_http_version 1.1`. The subprotocol header passes through.
- Timeouts: `proxy_connect_timeout 2s`, `proxy_read_timeout` and `proxy_send_timeout`
  `3600s` (WebSocket pings keep the link busy).
- Limits per source address (one station = one address): `limit_req` 2 handshakes/s,
  burst 20, `nodelay`; `limit_conn` 24 concurrent connections; both answer 429. Values are
  render parameters (`EDGE_RATE`, `EDGE_BURST`, `EDGE_CONN`). They act on handshakes only:
  frames inside an open WebSocket are limited by the CSMS (5.5).

### 11.3 Upstream and failover

```
upstream csms_ocpp {
    server k3s-master.incus:30900 max_fails=0;
    server k3s-node1.incus:30900  max_fails=0;
}
proxy_next_upstream error timeout;  proxy_next_upstream_tries 2;
```
`max_fails=0`: nginx never marks a node down; every handshake tries the next node when
the first refuses. (With `max_fails=1` a node that refused once stayed "down" far longer
than `fail_timeout`, because the counters of long-lived WebSockets do not decay as for short
requests: 247 s to recover instead of 11 s in the Phase 3 run.)
The node list comes from `K3S_NODES` (`infra/env.sh`) at render time; names resolve
through the city resolver (`*.incus` forwarded to the central bridge) when nginx loads
the configuration. With `externalTrafficPolicy: Local` a node without a CSMS pod refuses
the TCP connection, so nginx retries the other node at once: a charger whose replica dies
reconnects through the surviving node. The edge-to-CSMS link is plain HTTP inside the lab
(a stated limitation; TLS with the edge certificate is future work).

### 11.4 Access log

`/var/log/evcharge/edge-pt.<city>.edge.json`, one JSON object per line with the base fields
of section 6 (`ts` from `$time_iso8601`, i.e. seconds and `+00:00`), `service: edge`,
`instance: pt.<city>.edge`, `event: edge.access`, and:

| Field | nginx | Field | nginx |
|---|---|---|---|
| `hier_id` | id captured from the path (empty if none) | `cert_serial` | `$ssl_client_serial` |
| `srcip` | `$remote_addr` | `upstream` | `$upstream_addr` |
| `http_status` | `$status` (number) | `upstream_status` | `$upstream_status` |
| `tls` | `true` / `false` | `duration_s` | `$request_time` (number) |
| `cert_verify` | `$ssl_client_verify` | `bytes_out` | `$bytes_sent` (number) |
| `cert_dn` | `$ssl_client_s_dn` | `level` | `info` < 400 ≤ `warning` < 500 ≤ `error` |

nginx writes a line when the request **ends**: for an accepted charger (101) that is when
its WebSocket closes, with the whole session in `duration_s`; refused handshakes
(400/403/404/409/426/429/502) are logged at once. `log_format ... escape=json`.

### 11.5 Exporters and files

- `prometheus-nginx-exporter` on 9113 (scrapes `stub_status`), `prometheus-node-exporter`
  on 9100 (Debian packages).
- `/etc/evcharge/pki/`: `ca.crt`, `crl.pem`, `pt.<city>.edge.crt`, `pt.<city>.edge.key`
  (mode 0600, root). Configuration: `/etc/nginx/conf.d/evcharge.conf` rendered from
  `gateway/nginx.conf.tmpl`; the Debian default site is removed.

### 11.6 Scripts (interfaces other components call)

| Script | Does |
|---|---|
| `gateway/deploy_edge.sh <city> [--tls\|--plain]` | instance `<city>-edge` must exist and run. Installs packages if missing, exports the edge certificate, CA and CRL with `pki/export.sh`, renders and pushes the configuration, `nginx -t`, enables and reloads nginx and the exporters. Mode default from `PLAIN_WS_CITIES`. Idempotent |
| `gateway/push_crl.sh [city ...]` | copies the current CRL from the CA to the edges (default: all) and reloads nginx (`nginx -t && systemctl reload nginx`) |

A reload applies the CRL to **new** handshakes; open WebSockets keep their old worker.
The revoke flow therefore also disconnects the charger through the CSMS (12.5).

---

## 12. PKI

### 12.1 Where

The lab CA lives in its own unprivileged Incus container `lab-ca` (Debian trixie,
`openssl` only, no listening service, `limits.memory=64MiB`). The CA key never leaves it.
Leaf keys are generated there and copied straight into the instance that uses them
(`incus exec lab-ca -- cat ... | incus exec <target> -- ...`), never written to the host
disk. Only public material (`ca.crt`, `crl.pem`, the issued list) may be exported to
`pki/out/` (gitignored). Inside `lab-ca`: `/root/ca/{openssl.cnf, ca.key (0600), ca.crt,
index.txt, crlnumber, crl.pem, certs/<hier_id>.crt, keys/<hier_id>.key}`; `openssl.cnf`
comes from `pki/openssl.cnf`.

### 12.2 Profiles

| | CA | Charger | Edge |
|---|---|---|---|
| Subject | `CN=EVCharge Lab Root CA, O=EVCharge Lab` | `CN=<hier_id>, OU=<city>, O=EVCharge Lab` | `CN=pt.<city>.edge, OU=<city>, O=EVCharge Lab` |
| Key | EC P-256 | EC P-256 | EC P-256 |
| Validity | 10 years | 397 days | 397 days |
| Extensions | `CA:true, pathlen:0`; keyCertSign, cRLSign | `CA:false`; digitalSignature; EKU clientAuth; SAN DNS `cp-N.st-NNN.<city>.pt.lab` | `CA:false`; digitalSignature; EKU serverAuth; SAN DNS `edge.<city>.pt.lab`, `<city>-edge.<city>.pt.lab` |

Signature SHA-256; random serials (`-rand_serial`). A serial is written as upper-case hex
without colons or leading zeros (the form stored in the registry's `cert_serial`). One
CA level only (an intermediate CA is future work).

### 12.3 CRL

`crl.pem`, `default_crl_days 30`. Rebuilt on every revocation and by `pki/crl.sh`
(run at every city deploy). An expired CRL makes nginx refuse **every** client
certificate, so `pki/crl.sh` + `gateway/push_crl.sh` must run at least weekly (limitation).

### 12.4 Scripts

| Script | Does |
|---|---|
| `pki/ca.sh` | creates `lab-ca` and the CA if missing; idempotent |
| `pki/issue.sh <hier_id> [--force]` / `pki/issue.sh --roster PATH [--city X] [--force]` | issues a charger or edge certificate (all chargers and edges of the roster); skips ids with a valid certificate unless `--force` (then the old one is revoked first). Prints `<hier_id> <serial>` per line |
| `pki/serial.sh <hier_id>` | current serial |
| `pki/export.sh <hier_id> <instance> [--dir /etc/evcharge/pki] [--group GID] [--crl]` | writes `ca.crt`, `<hier_id>.crt`, `<hier_id>.key` (0640, group GID; 0600 root without it) and with `--crl` also `crl.pem` into the instance |
| `pki/revoke.sh <hier_id> [reason]` | revokes the current certificate (default `keyCompromise`), regenerates the CRL, runs `gateway/push_crl.sh`; prints the revoked serial |
| `pki/crl.sh` | regenerates the CRL |

### 12.5 Revoke flow

Response to a hijacked or stolen charger identity (wired into the Wazuh active response
in Phase 6): registry `POST /quarantine/{id}` → CSMS `disconnect` → `pki/revoke.sh <id>`.
A later reconnect with the old certificate is refused by the edge (400, `cert_verify`
`FAILED:certificate revoked`). Re-enrolment: `pki/issue.sh <id> --force`, registry
`PATCH cert_serial`, `pki/export.sh` to the station, restart `evsim`, release the
quarantine, change-availability `Operative`.

---

## 13. City sites and station runtime

### 13.1 Networks and names

- Network `city-<city>` (`CITY_NET` in `infra/env.sh`, `infra/02_networks.sh`): own DNS
  zone `<city>.pt.lab`, `*.incus` forwarded to the central bridge, `user.hier-id=pt.<city>`.
- Aliases in the network's `raw.dnsmasq`, generated from the roster:
  `cname=edge.<city>.pt.lab,<city>-edge.<city>.pt.lab` and, per station,
  `cname=st-NNN.<city>.pt.lab,<city>-st-NNN.<city>.pt.lab`.
- City networks reach the central side only on NodePorts 30900 (OCPP, edges) and 30801
  (registry, edges) and on Wazuh 1514/1515; enforcing this with host firewall rules or a
  NetworkPolicy is a later step.

### 13.2 Instances

| Instance | Role | Address | Limits | Config keys |
|---|---|---|---|---|
| `<city>-edge` | edge gateway (section 11) and city Prometheus (section 14.1) | static: host `.2` of the city network (derived from `CITY_NET`) | 1 CPU, 384 MiB | `user.hier-id=pt.<city>.edge`, `user.role=edge`, `user.location.lat/lon` |
| `<city>-st-NNN` | station controller: runs `evsim` for its chargers | DHCP (recorded in the registry) | 1 CPU, 224 MiB (Wazuh agent included) | `user.hier-id=pt.<city>.st-NNN`, `user.role=station`, `user.location.lat/lon/city` |

All are unprivileged Debian trixie containers launched from one published image alias
`evcharge-city` (Python 3.13 venv `/opt/evcharge/venv` with `evcommon` and `evsim`; nginx
and the exporters installed but disabled), rebuilt with `REBUILD=1`. A user `evcharge`
(uid 10001) runs the services.

### 13.3 Station runtime

Files: `/etc/evcharge/roster.yaml` (copy of the roster), `/etc/evcharge/pki/` (`ca.crt`
and only this station's chargers' certificates and keys, group `evcharge`),
`/etc/evcharge/sim.env`, `/var/log/evcharge/` (owned by `evcharge`).

`/etc/evcharge/sim.env`:
```
SIM_CSMS_URL=wss://edge.<city>.pt.lab/ocpp      # ws://edge.<city>.pt.lab/ocpp in a plain city
SIM_EXTRA_ARGS=                                 # PoCs add e.g. --tamper 0.5 here
```
`evsim.service`:
```
[Service]
User=evcharge
EnvironmentFile=/etc/evcharge/sim.env
ExecStart=/opt/evcharge/venv/bin/python -m evsim --roster /etc/evcharge/roster.yaml \
  --station pt.<city>.st-NNN --csms-url ${SIM_CSMS_URL} --pki-dir /etc/evcharge/pki \
  --metrics-host 0.0.0.0 --metrics-port 9300 --log-dir /var/log/evcharge $SIM_EXTRA_ARGS
Restart=always
RestartSec=5
NoNewPrivileges=yes
ProtectSystem=strict
ReadWritePaths=/var/log/evcharge
PrivateTmp=yes
```

### 13.4 Registration and CSMS configuration

- The NOC creates `pt.<city>` with the admin token (from the central side). The city's
  subtree is registered **from its edge with the city's token** (delegation in action):
  `pt.<city>.edge` (`ip`, `host`, `network`, `dns`, `metrics_target`
  `edge.<city>.pt.lab:9113`), each station (`name`, `lat`, `lon` from the roster, `ip` from
  Incus, `host`, `network`, `dns`, `metrics_target` `st-NNN.<city>.pt.lab:9300`) and each
  charger (roster fields + `cert_serial`). Tokens travel on stdin, never on command lines
  or in files left inside instances.
- ConfigMap `csms-edge` (namespace `evcharge`), keys `CSMS_TRUSTED_PROXIES` (the edges'
  addresses, read with `incus list` when the cities are deployed) and `CSMS_ALLOW_DIRECT`.
  `infra/05_deploy.sh` creates it, only if absent, with `CSMS_ALLOW_DIRECT=true` and no
  proxies (Phase 2 behaviour); the city setup writes the real values with
  `CSMS_ALLOW_DIRECT=false` and restarts the CSMS. From then on simulators run from the
  host (`dev/run_sims.sh`) are refused with `ident.untrusted_peer`; set
  `CSMS_ALLOW_DIRECT=true` again to use them.

---

## 14. Monitoring

Two levels: one Prometheus per city that sees every charger, and one central Prometheus
that sees only aggregates of the cities plus the central services. Per-charger series
never cross the inter-site link (the bandwidth argument, measured in 14.6).

### 14.1 City Prometheus

- Runs **on the edge** (`<city>-edge`, Debian package `prometheus`, systemd), listening on
  `0.0.0.0:9090`. Reason: the edge is already the city's monitored entry point and has
  spare memory (about 30 MiB used); a separate container would cost another base system per
  city and a new element kind. Consequence, used on purpose: when the edge is down, the
  central side loses the city's federation too, which is exactly the `EdgeDown` condition.
- Flags: `--storage.tsdb.retention.time=12h --storage.tsdb.retention.size=256MB`; no
  `--web.enable-lifecycle` (reload with `systemctl reload prometheus`). Global
  `scrape_interval: 5s`, `scrape_timeout: 4s`, `evaluation_interval: 5s`; external label
  `site: pt.<city>`.
- Files: `/etc/prometheus/prometheus.yml` rendered from `monitoring/city/prometheus.yml.tmpl`,
  `/etc/prometheus/city.rules.yml` from `monitoring/city/city.rules.yml`.
- Scrape jobs:

| Job | Targets | Labels | Notes |
|---|---|---|---|
| `stations` | HTTP SD `http://k3s-master.incus:30801/sd/prometheus?city=<city>&kind=station` (refresh 60 s) | from the registry: `hier_id` (station), `city`, `station` | `honor_labels: true` (the sim's own `hier_id`/`charger` labels win; `up` keeps the station's) |
| `edge-nginx` | `127.0.0.1:9113` | `city`, `hier_id=pt.<city>.edge` | nginx exporter |
| `edge-node` | `127.0.0.1:9100` | same | node exporter of the edge |
| `prometheus` | `127.0.0.1:9090` | same | self |

  Stations run no node exporter (memory: 15 more processes); a station's liveness is its
  `evsim` target (`up`). The registry SD is the G3 pattern (registry-driven discovery): a
  station registered by its city operator is scraped without editing Prometheus.

### 14.2 Central monitoring (K3s, namespace `monitoring`)

Plain manifests in `k8s/monitoring/` (no operator, no kube-prometheus-stack: too heavy
next to the platform on 3 GiB nodes). Images mirrored to `docker.incus:5000` with exact
versions recorded in the manifests. Placement: preferred on `k3s-node1` (the master hosts
Postgres and is at about 50 % memory).

| Component | Kind | Port / NodePort | Storage | Resources (request / limit) |
|---|---|---|---|---|
| Prometheus | Deployment (1) + ServiceAccount/ClusterRole (pods, nodes, nodes/proxy, endpoints: get/list/watch) | 9090 / 30090 | PVC 5 Gi local-path, retention 7 d | 100m, 256 Mi / 768 Mi |
| Alertmanager | Deployment (1) | 9093 / 30093 | none | 25m, 32 Mi / 128 Mi |
| Grafana | Deployment (1) | 3000 / 30300 | none (provisioned from the repo) | 50m, 128 Mi / 384 Mi |

Prometheus flags: `--enable-feature=extra-scrape-metrics` (gives `scrape_body_size_bytes`,
used in 14.6), `--storage.tsdb.retention.time=7d`. Global `scrape_interval: 15s`,
`evaluation_interval: 15s`, external label `site: pt.central`.

| Job | Discovery | Interval | Notes |
|---|---|---|---|
| `csms`, `registry`, `billing` | `kubernetes_sd` role `pod`, namespace `evcharge`, keep `prometheus.io/scrape=true`; `job` = label `app.kubernetes.io/name`; labels `pod`, `node` | 15 s | the pod annotations already set |
| `cadvisor` | role `node`, through the API server proxy `/api/v1/nodes/<node>/proxy/metrics/cadvisor` | 30 s | keep only `container_cpu_usage_seconds_total` and `container_memory_working_set_bytes` of namespaces `evcharge` and `monitoring` |
| `prometheus`, `alertmanager`, `grafana` | static, in-namespace Services | 30 s | |
| `federate-<city>` (one per `CITY_NET` entry) | static `edge.<city>.pt.lab:9090`, `metrics_path: /federate`, `params: {match[]: ['{__name__=~"city:.*"}']}`, `honor_labels: true` | **30 s**, timeout 10 s | target labels `city`, `hier_id=pt.<city>.edge` |
| `direct-<city>` | **only during the bandwidth measurement** (14.6): the city's station SD URL plus `edge.<city>.pt.lab:9113` and `:9100` | 5 s | off by default |

Name resolution: the K3s pods resolve `*.pt.lab` because the central bridge
(`incus-default`) forwards each `<city>.pt.lab` zone to that city's resolver
(`raw.dnsmasq` `server=/<city>.pt.lab/<city gateway>`, added by `infra/02_networks.sh`) and
CoreDNS forwards to the node's resolver.

### 14.3 Alert rules (central, `monitoring/rules/`)

Every alert carries labels `severity` (`info|warning|critical`), `city`, `hier_id`
(the most precise element known: station, edge, city `pt.<city>` or `pt.central.<svc>`)
and `threat` (`hijack|fraud|botnet|infra`), plus annotations `summary` and `description`.
These labels are the join keys of the correlator (4.5). Names are
`evcommon.metrics.ALERT_NAMES`.

| Alert | Expression (sketch; exact text in `monitoring/rules/alerts.yml`) | for | threat |
|---|---|---|---|
| `CSMSDown` | `up{job="csms"} == 0` or `absent(up{job="csms"})` | 1m | infra |
| `RegistryDown` / `BillingDown` | `up{job="registry"} == 0` / `up{job="billing"} == 0` | 1m | infra |
| `EdgeDown` | `min by (city) (up{job=~"federate-.+"}) == 0 or min by (city) (city:edge_up:min) == 0` | 1m | infra |
| `StationDown` | `city:station_up:min == 0` | 1m | infra |
| `ChargerOffline` | per station: `sum by (hier_id) (evcharge_csms_connected_chargers)` (0 if absent) `< on (hier_id) max by (hier_id, city, station) (city:station_chargers:count)` | 2m | infra |
| `ChargerFlapping` | `city:reconnects:rate5m * 300 > 5` | 5m | infra (also botnet/hijack context) |
| `DuplicateChargerID` | `sum by (city) (increase(evcharge_csms_duplicate_id_total{outcome="rejected"}[5m])) > 0` | 0m | hijack |
| `IdentityRejections` | `sum by (city, reason) (increase(evcharge_csms_identity_rejections_total{reason!="duplicate_id"}[5m])) > 0` | 0m | hijack |
| `AuthFailureBurst` | `sum by (city) (increase(evcharge_csms_card_auth_total{result=~"Blocked\|Invalid\|Expired"}[10m])) > on (city) baseline:city_auth_failures_10m:upper` | 0m | fraud |
| `MsgRateAnomaly` | `city:ocpp_messages_sent:rate1m > on (city) baseline:city_msgs_rate:upper` | 2m | botnet |
| `ChargerMsgFlood` | `city:charger_msg_rate:max1m > 1` (above the CSMS sustained limit) | 1m | botnet |
| `CityLoadSpike` | `city:grid_power_watts:sum > on (city) baseline:city_grid_power_watts:upper` | 2m | botnet |
| `MeterUnderReporting` | `city:station_power_gap_watts > 1000` | 5m | fraud |
| `MeterAnomalies` | `sum by (city, reason) (increase(evcharge_csms_meter_anomalies_total[5m])) > 0` | 0m | fraud |

Counters such as `evcharge_csms_duplicate_id_total` are created at their first event, so
`increase()` misses the first increment (the series is born at 1). Alerts on such counters
use the growth pattern `m - (m offset 5m or 0 * m)`. City-level alerts (`EdgeDown`,
`CityLoadSpike`, ...) carry `hier_id="pt.<city>"`; alerts on central services carry
`city="central"` and `hier_id="pt.central.<service>"`.

`ConcurrentTx` is deliberately not in `AuthFailureBurst` (normal traffic, 4.4). A
`rollout restart` of the CSMS reconnects every charger and can fire `ChargerFlapping`: a
known false positive, stated in the report.

### 14.4 Alertmanager

`monitoring/alertmanager/alertmanager.yml`: group by `alertname, city, hier_id`,
`group_wait 10s`, `group_interval 1m`, `repeat_interval 4h`. Receiver `default` with no
integration until the correlator exists (Phase 9 adds a webhook to
`http://correlator.evcharge:8004/signals/alertmanager` with its bearer token).
Inhibition:

```yaml
inhibit_rules:
  - source_matchers: [alertname="EdgeDown"]
    target_matchers: [alertname=~"ChargerOffline|StationDown|ChargerFlapping|MsgRateAnomaly|CityLoadSpike"]
    equal: [city]
  - source_matchers: [alertname="CSMSDown"]
    target_matchers: [alertname=~"ChargerOffline|ChargerFlapping"]
```

A dead edge (or a cut link to a city) produces one `EdgeDown` instead of one alert per
station and charger.

### 14.5 Grafana

Provisioned from `monitoring/grafana/` (datasources, dashboard provider, dashboard JSON),
admin password from `GRAFANA_ADMIN_PASSWORD` (Secret `grafana-env`), anonymous access off.
Datasources: `central` (uid `central`, default) and one per city (uid `city-<city>`,
`http://edge.<city>.pt.lab:9090`) for per-charger drill-down, the only place where
per-charger series exist.

| Dashboard (uid) | Content |
|---|---|
| `evcharge-map` | **Geomap** of the stations, colour = share of connected chargers, red layer for stations with a firing alert; stat panels per city (chargers connected, kW reported vs grid, sessions); alert list panel |
| `evcharge-city` | variable `city`: per station and per charger (datasource `city-$city`): status codes, power, energy, message rate, reconnects; edge connections and requests |
| `evcharge-platform` | CSMS replicas (`count(up{job="csms"})`), connections per replica, handle latency p50/p95 (`evcharge_csms_ocpp_handle_seconds`), dependency errors and latency, registry/billing, pod CPU and memory (cadvisor) |
| `evcharge-bandwidth` | federated bytes per minute per city, samples per scrape, series count, and the direct-scrape counterfactual when it runs (14.6) |

Geomap query (instant, table format; Grafana "labels to fields" gives `lat`/`lon`):
```
(
  sum by (hier_id) (evcharge_csms_connected_chargers)
    / on (hier_id) max by (hier_id) (city:station_chargers:count)
)
* on (hier_id) group_left (city, station, lat, lon)
  max by (hier_id, city, station, lat, lon) (evcharge_registry_station_info)
```
Coordinates come from the registry (`evcharge_registry_station_info`, scraped centrally),
status from the CSMS (central, trusted view: a station whose chargers disconnect drops to 0
within one scrape) and the charger count from the city. Thresholds: 1 green, between 0 and 1
orange, 0 grey. Chargers share their station's coordinates, so the map shows stations with
"connected/total" in the tooltip; the per-charger detail is the `evcharge-city` dashboard.
Expected delay for a stopped station: CSMS scrape (15 s) + dashboard refresh (10 s) ≈ 25 s.

### 14.6 Bandwidth measurement (bonus: per-site monitoring)

Question: how many bytes per minute cross the city → central link with federation,
compared with central scraping every city target directly.

1. **Federated (A):** for 30 minutes of normal load,
   `sum by (city) (avg_over_time(scrape_body_size_bytes{job=~"federate-.+"}[30m])) * 60 / 30`
   bytes per minute (uncompressed body; `scrape_samples_scraped` for samples).
2. **Direct (B), counterfactual:** in the next 30 minutes, same load, enable the
   `direct-<city>` jobs (every station target and the edge exporters, 5 s, exactly the city
   Prometheus' targets) with `infra/06_monitoring.sh --direct-scrape on`, and compute
   `sum by (city) (avg_over_time(scrape_body_size_bytes{job=~"direct-.+"}[30m])) * 60 / 5`.
   Switch it off afterwards.
3. **On the wire:** `scrape_body_size_bytes` is the uncompressed body; Prometheus asks for
   gzip, so also measure the compressed sizes once with `eval/bandwidth.sh` (curl with
   `--compressed -w '%{size_download}'` from a pod: one `/federate` request per city and one
   request per direct target) and report both. TCP/HTTP overhead is ignored in both.
4. Report per city and in total: bytes/min A, bytes/min B, saving `1 - A/B`, samples/min,
   and the scaling argument (B grows with chargers, A with stations). Repeat at the larger
   scales of Phase 10.

Same load, same window length and the same tool for both sides make the comparison fair;
the direct jobs also cost central memory, which is reported.

### 14.7 Baseline and thresholds

- Series: per city `city:grid_power_watts:sum` (load), `city:ocpp_messages_sent:rate1m`
  (message rate) and `increase(evcharge_csms_card_auth_total{result=~"Blocked|Invalid|Expired"}[10m])`.
- Window: **24 h** recorded by the central Prometheus (retention 7 d) with the normal
  roster load; **minimum 6 h** if the schedule forces it (stated in the report with the
  actual window).
- Thresholds: from 1-minute samples of the window, `upper = mean + 3σ`. For the load, also
  capped at 90 % of the city's installed capacity (sum of `max_kw` in the roster), since a
  higher upper bound could never fire. The simulator's load is stationary (no daily cycle),
  so hour-of-day bands would add nothing here; they are future work for real traffic.
- Output: `monitoring/rules/baseline.yml`, recording rules with constant values per city
  (`baseline:city_grid_power_watts:upper`, `baseline:city_grid_power_watts:mean`,
  `baseline:city_msgs_rate:upper`, `baseline:city_msgs_rate:mean`,
  `baseline:city_auth_failures_10m:upper`), each with labels `city` and `basis`
  (`provisional` before the recording, then e.g. `24h-20261107`), and a header comment with
  window, sample count, mean and σ. Written by `monitoring/baseline/compute.py`, run in a
  throwaway pod with the lab's Python image (nothing on the host). Grafana draws the bands.

### 14.8 Network flows and the firewall decision

Flows the platform needs (beyond DNS/DHCP to each network's own gateway):

| From | To | Port | Why |
|---|---|---|---|
| edges | K3s nodes | 30900 | OCPP to the CSMS |
| edges | K3s nodes | 30801 | registration, Prometheus SD |
| stations, edges | `wazuh-container` | 1514, 1515 | Wazuh agents (Phase 5) |
| K3s nodes (pods, SNAT to the node) | edges | 9090 | federation |
| K3s nodes | stations 9300, edges 9113/9100 | | only during the bandwidth measurement |
| cities | internet (NAT) | 80/443 | package installs at build time |

Decision: today the city networks can reach every central NodePort. Closing this is a
**hijacking/botnet mitigation**, so it is done in **Phase 6** as **Incus network ACLs** on the
`city-*` networks (Incus only, so it works the same on the Debian VM), shown before/after,
with this table as the allow list. Phase 4 adds only the central → edge 9090 flow. Until
then the city Prometheus API (9090, no authentication) is readable from its city network:
a stated limitation.

---

## 15. Wazuh

Manager: the existing `wazuh-container` (`wazuh-container.incus`, Wazuh 4.14.9: manager,
indexer and dashboard in one unprivileged container on the central bridge). Agents send to
1514/tcp and enrol on 1515/tcp. Project files live in `wazuh/` and are deployed with
`incus file push`; the course-guide rules already on the manager (`local_rules.xml`, IDs
100001, 100100, 100200–100202, 100300–100301) are **left untouched**.

### 15.1 Agents

| Where | How | Name | Groups | Reads |
|---|---|---|---|---|
| K3s nodes (both modes) | DaemonSet `wazuh-agent` in namespace `wazuh`, image `docker.incus:5000/evcharge-wazuh-agent:<4.14.9>` (Debian trixie + the Wazuh 4.14.9 agent package), hostPath `/var/log/evcharge` read-only, hostPath `/var/lib/evcharge-wazuh/agent` for `client.keys` (identity survives pod restarts), `NODE_NAME` from the downward API | `pt.central.<node>` | `central` | `/var/log/evcharge/*.json` (CSMS, registry, billing; one file per pod) |
| edges (3) | native package in `<city>-edge` | `pt.<city>.edge` | `city-<city>`, `edge` | `/var/log/evcharge/edge-*.json` (json), `/var/log/nginx/error.log` (syslog, custom decoder) |
| stations (all 15) | native package in `<city>-st-NNN` | `pt.<city>.st-NNN` | `city-<city>`, `station` | `/var/log/evcharge/sim-*.json`, `/var/log/evcharge/netwatch.json` (json); `full_command` `station-sockets` |
| manager | itself (agent 000) | `wazuh-container` | — | `/var/ossec/logs/evcharge-ar.json` (json) |

Why an agent on **every** station: the botnet threat is visible first on the station host
(a new outbound connection to a C2 server), which no central log shows; with an agent on
one station per city the detection would only cover a fifth of the fleet. Cost: about
30–45 MiB per agent, about 0.7 GiB for 15 of the ~5.4 GiB the host has free; the station
limit becomes 224 MiB (13.2). Fallback if memory runs short: agents on the edges and on
one station per city, the others covered by the edge and CSMS logs (stated in the report).

Group configuration (`wazuh/groups/<group>/agent.conf`, pushed to
`/var/ossec/etc/shared/<group>/`): `central`, `edge`, `station` define the `<localfile>`
entries above; `station` also defines
`<localfile><log_format>full_command</log_format><command>ss -tnpH state established</command><alias>station-sockets</alias><frequency>60</frequency></localfile>`.

Station outbound watcher (`netwatch`): a small Python script run by a systemd timer every
10 s on each station. It lists established outbound TCP connections (`ss -tnpH`), and for
each destination not in `/etc/evcharge/netwatch-allow.txt` (the city edge, the Wazuh
manager, the city gateway; written at deploy from `incus list`, never in the repo) that it
has not reported in the last hour, writes one `host.new_outbound` line (base fields of 6,
`service: netwatch`, `instance`/`hier_id` = the station, `dstip`, `dstport`, `process_name`) to
`/var/log/evcharge/netwatch.json`.

### 15.2 Decoders

- Service and edge logs: the built-in `json` decoder (verified on the manager with
  `wazuh-logtest`: every field becomes a dynamic field, and `srcip` is the static `srcip`,
  so `<srcip>` CIDR matches, `same_source_ip` and `firewall-drop` work). Rules select our
  logs with `<decoded_as>json</decoded_as>` and `<field name="service">`.
- Custom decoder `evcharge-nginx-limit` (`wazuh/decoders/evcharge_decoders.xml`) for the
  edge's nginx `error.log` lines "limiting requests" / "limiting connections" (nginx logs
  them at `error` level as they happen, unlike the access log of a WebSocket): extracts
  `srcip`, `zone`, `url` (the requested path) and `server`. Built as a child of the stock
  nginx error-log decoder if that matches the line in `wazuh-logtest`, else standalone.
- CDB lists (`wazuh/lists/`): `evcharge-plain-cities` (cities without TLS), `evcharge-ip-exceptions`
  (charger ids whose IP mismatch is expected, e.g. a test spare), `evcharge-card-allowlist`
  (cards never auto-blocked).

### 15.3 Rule ID blocks

| IDs | File | Block |
|---|---|---|
| 110000–110099 | `wazuh/rules/evcharge_base.xml` | parent rules, catch-alls, whitelists |
| 110100–110199 | `wazuh/rules/evcharge_hijack.xml` | signal hijacking |
| 110200–110299 | `wazuh/rules/evcharge_fraud.xml` | fraud |
| 110300–110399 | `wazuh/rules/evcharge_botnet.xml` | botnet |
| 110400–110499 | `wazuh/rules/evcharge_infra.xml` | platform, audit of responses |

(The plan's earlier 100100–100199 block is moved here because the course-guide rule 100100
already uses that ID on the same manager.)

Conventions: every rule is in group `evcharge` plus its block (`hijack`, `fraud`, `botnet`,
`infra`); `<mitre><id>` uses **Enterprise** ATT&CK IDs (the manager's MITRE database holds
only that matrix; all IDs below were checked against it); the matching **ATT&CK for ICS**
technique, where one fits, is a group `mitre_ics_T0xxx` and appears in the description.
Informational parents have level 2: below the alert threshold (3), so they are not stored,
but frequency and composite rules can count them. Level 0 = explicit whitelist. Specific
children are written **before** the catch-alls (Wazuh takes the first matching sibling).

### 15.4 Rules

Field tests are on the JSON fields of section 6 (`event`, `result`, `reason`, `level`, ...).
"freq N/T" = `frequency` N within `timeframe` T seconds.

**Base and whitelists (110000–110099)**

| ID | Level | Matches | Notes |
|---|---|---|---|
| 110000 | 0 | `decoded_as json` and `service` ∈ {csms, registry, billing, fraud, correlator, sim, edge, netwatch, ar} | parent of every rule below |
| 110010 | 0 | `edge.access` with `http_status` 101 | a WebSocket that ended normally |
| 110011 | 2 | `auth.reject` with `result` `ConcurrentTx` | normal traffic (4.4); counted by 110203 |
| 110012 | 0 | `netwatch` lines whose `process_name` is a package manager (`apt`, `http`, `https`, `gpgv`) | maintenance traffic |
| 110013 | 0 | `ident.ip_mismatch` for a `hier_id` in `evcharge-ip-exceptions` | known exceptions |
| 110014 | 0 | `sim.*` events other than `sim.rejected` | station controller chatter |
| 110015 | 2 | `session.start` | counted by 110206 and 110308 |
| 110090 | 2 | any other event with `level` info or debug | catch-all, counted by frequency rules |
| 110091 | 3 | any other event with `level` warning | visible, low |
| 110092 | 7 | any other event with `level` error or critical | service errors |

**Hijacking (110100–110199)**

| ID | Level | Matches | MITRE (Enterprise / ICS) | Response |
|---|---|---|---|---|
| 110100 | 10 | `charger.duplicate_id` | T1036 / T0849 | — |
| 110101 | 10 | `ident.cert_mismatch` | T1036 / T0849 | `quarantine-charger` |
| 110102 | 8 | `ident.cert_serial_mismatch` | T1552.004 / T0849 | — |
| 110103 | 10 | `ident.city_mismatch` | T1036 / T0849 | `quarantine-charger` |
| 110104 | 7 | `ident.ip_mismatch` | T1078 / T0859 | — |
| 110105 | 6 | `ident.no_cert` | T1078 | — |
| 110106 | 8 | `ident.untrusted_peer` | T1599 | — |
| 110107 | 9 | `ident.spoofed_header` | T1557 / T0830 | — |
| 110108 / 110109 | 5 | `ident.unknown` / `ident.bad_id` | T1595 / T0846 | — |
| 110110 | 10 | freq 10/60 of 110108–110109, `same_source_ip` | T1595 / T0846 | `evcharge-edge-drop` |
| 110111 | 7 | `ident.quarantined` | T1078 | — |
| 110112 | 10 | freq 5/300 of 110111, `same_field hier_id` | T1078 | — |
| 110113 | 7 | `charger.boot_mismatch` | T1036 / T0849 | — |
| 110114 | 6 | `ocpp.invalid` | T1499 | — |
| 110115 | 9 | freq 20/60 of 110114, `same_field hier_id` | T1499 / T0814 | — |
| 110116 | 10 | `meter.anomaly` with `city` in `evcharge-plain-cities` | T1557, T1565.002 / T0830, T0856 | — (MitM indicator in the legacy city) |
| 110120 | 8 | `edge.access` with `http_status` 400 and `cert_verify` starting `FAILED` | T1552.004 / T0849 | — |
| 110121 | 11 | freq 5/120 of 110120, `same_source_ip` | T1552.004 | `firewall-drop` (edge, local) |
| 110122 | 5 | `edge.access` with `http_status` 404 | T1595 / T0846 | — |
| 110123 | 9 | freq 20/60 of 110122, `same_source_ip` | T1595 / T0846 | `firewall-drop` (edge, local) |
| 110130 | 12 | 110104 (`ip_mismatch`) with `if_matched_sid` 110100 within 300 s, `same_field hier_id` | T1036, T1552.004 / T0849 | `quarantine-charger` + `evcharge-edge-drop` (the `srcip` of an IP mismatch is by definition not the station) |
| 110131 | 12 | 110100 (`duplicate_id`) with `if_matched_sid` 110104 within 300 s, `same_field hier_id` | T1036 / T0849 | `quarantine-charger` only (this event's `srcip` may be the real station) |

**Fraud (110200–110299)**

| ID | Level | Matches | MITRE | Response |
|---|---|---|---|---|
| 110200 | 4 | `auth.reject` with `result` Blocked, Invalid or Expired | T1078 | — |
| 110201 | 10 | freq 5/600 of 110200, `same_field card` | T1078 | — (the card is already refused) |
| 110203 | 10 | freq 2/600 of 110011 (`ConcurrentTx`), `same_field card`, `different_field city` | T1078 / T0859 | `block-card` |
| 110204 | 7 | `meter.anomaly` (any reason) | T1565.002 / T0856 | — |
| 110205 | 11 | freq 3/600 of 110204, `same_field hier_id` | T1565.002 / T0856 | `quarantine-charger` (bound in Phase 7) |
| 110206 | 10 | freq 2/1800 of 110015 (`session.start`), `same_field card`, `different_field city` | T1078 | `block-card` (Wazuh-side impossible travel) |
| 110207 | 10 | `fraud.alert` with `rule` impossible_travel | T1078 | `block-card` |
| 110208 | 9 | `fraud.alert`, any other rule | T1565.002 / T0856 | decided in Phase 7 |
| 110209 | 5 | `billing.card_blocked`, `billing.card_unblocked`, `billing.session_flagged` | — | audit |

**Botnet (110300–110399)**

| ID | Level | Matches | MITRE | Response |
|---|---|---|---|---|
| 110300 | 6 | `ocpp.rate_limited` | T1499 / T0814 | — |
| 110301 | 10 | `conn.kicked` with `reason` rate_limit | T1499 / T0814 | — |
| 110302 | 12 | freq 3/60 of 110300, `same_field city`, `different_field hier_id` | T1498 / T0814 | decided in Phase 8 (bulk quarantine) |
| 110303 | 8 | `host.new_outbound` | T1071 / T0869 | — |
| 110304 | 12 | freq 3/300 of 110303, `same_field dstip`, `different_field hier_id` | T1071.001 / T0869 | decided in Phase 8 |
| 110305 | 7 | `full_command` `station-sockets` output changed (`check_diff`) | T1071 / T0869 | — |
| 110306 | 6 | `evcharge-nginx-limit` (handshake refused by `limit_req`/`limit_conn`) | T1498 | — |
| 110307 | 10 | freq 10/60 of 110306, `same_source_ip` | T1498 / T0814 | `firewall-drop` (edge, local) |
| 110308 | 10 | freq 10/60 of 110015, `same_field city` | T1498 / T0831 | — (grid-surge indicator; joined with `CityLoadSpike`) |

**Platform and audit (110400–110499)**

| ID | Level | Matches | MITRE | Notes |
|---|---|---|---|---|
| 110400 | 8 | `dependency.error` | — | |
| 110401 | 11 | freq 10/120 of 110400, `same_field dependency` | T1499 | |
| 110402 | 6 | `api.auth_failed` | T1110 | |
| 110403 | 10 | freq 10/120 of 110402, `same_source_ip` | T1110 | |
| 110404 | 10 | `registry.write_denied` | T1078 | delegation abuse |
| 110405 | 4 | `registry.quarantine`, `command.result` with `source` wazuh or fraud | — | isolation acted |
| 110406 | 3 | `registry.release`, `billing.card_unblocked` | — | a human reversed a response |
| 110410 | 5 | `ar.action` with `result` ok | — | an active response acted |
| 110411 | 10 | `ar.action` with `result` error or refused | — | a response failed or a guard stopped it |
| 110412 | 3 | `ar.evidence` | — | evidence snapshot kept before an action (post-mortem trail) |

Every rule has a positive and a negative sample in `wazuh/tests/` (15.8).

### 15.5 Active responses

Principles: isolate, block and **keep evidence**; nothing is deleted; anything
irreversible needs a person. Every response logs `ar.action` lines to
`/var/ossec/logs/evcharge-ar.json` (read by the manager itself, rules 110410/110411).

| Command | Runs on | Triggered by | Does | Allow-list / guards | Timeout |
|---|---|---|---|---|---|
| `firewall-drop` (stock) | the edge that wrote the log (`location local`) | 110121, 110123, 110307 | drops the `srcip` in the edge's `iptables` (Debian `iptables`, nft backend, installed on the edges) | manager `<white_list>`: city gateway, edges, K3s nodes, the manager, `lab-ca` (generated from `incus list` at deploy, never in the repo) | 600 s, then removed |
| `evcharge-edge-drop` (custom) | sent to all agents (`location all`); acts only on an agent whose role is `edge` and only if `srcip` is inside that edge's city network | 110110, 110130 | same drop as above, at the city edge, for addresses seen by the CSMS | the same white list, plus the edge's own and gateway addresses; refuses non-private and out-of-city addresses | 600 s, then removed |
| `quarantine-charger` (custom, Python, manager framework interpreter) | manager (`location server`) | 110101, 110103, 110130, 110131 (110205 in Phase 7) | 1) evidence: saves `GET /connections/{id}` and registry `GET /nodes/{id}` into the AR log; 2) registry `POST /quarantine/{id}` (`source=wazuh`, `incident_id` = alert id); 3) CSMS `remote-stop` (409 is fine), `change-availability Inoperative`, `disconnect`; 4) writes a **revocation request** (no revocation) | only charger hier-ids; ids in `/var/ossec/etc/evcharge/quarantine-allowlist` skipped; **rate guard 5 per 10 min** (more → `result=refused`, rule 110411: a flood of forged log lines cannot quarantine the fleet); HTTP timeout 5 s per call | none: release is manual (registry release + `Operative`) after review |
| `block-card` (custom, Python) | manager | 110203, 110206, 110207 | evidence: `GET /sessions?card=`; billing `POST /cards/{card}/block`; `POST /sessions/{id}/flag` when the alert has `transaction_id` | card regex of 5.3; `evcharge-card-allowlist`; **rate guard 10 per hour** | none: unblock is manual |

Why no automatic release for quarantine and card blocks: a timer would hand a hijacked
identity or a cloned card back to the attacker; both actions are reversible by an operator
in seconds. The firewall drops expire because an address may be reused by a legitimate
device. Collateral damage (the real charger is quarantined together with its cloned
identity; a blocked card hits its real owner) is a stated cost, discussed in the report.

Revocation: the manager has no access to the CA, on purpose. `quarantine-charger` appends
`{"hier_id", "alert_id", "rule_id", "ts", "reason"}` to
`/var/ossec/logs/evcharge-revocation-requests.json`; an operator runs
`pki/process_revocations.sh` on the host, which reads the pending requests with
`incus exec wazuh-container`, shows them, and on confirmation runs `pki/revoke.sh` for each
and records it as processed in `lab-ca`. A forged log line can therefore at most isolate a
charger, never destroy its credentials.

Credentials of the active responses: `/var/ossec/etc/evcharge/ar.env` on the manager
(mode 0640, `root:wazuh`) holds `REGISTRY_URL`, `CSMS_API_URL`, `BILLING_URL` (NodePorts on
`k3s-master.incus`, falling back to `k3s-node1.incus`) and the three **responder tokens**
(4.1–4.3: isolate-only scopes). Never the admin or full API tokens. Written by the deploy
script from `secrets/platform.env`; never in the repo.

### 15.6 Correlator hook (stub until Phase 9)

`<integration>` `custom-correlator` (`wazuh/integrations/custom-correlator`, Python, manager
framework interpreter): for alerts of level ≥ 7 in group `evcharge`, `POST` the alert JSON
unchanged to `http://k3s-master.incus:30804/signals/wazuh` with the bearer
`CORRELATOR_API_TOKEN` (read from `ar.env`), timeout 2 s. While the correlator does not
exist, failed posts are appended to `/var/ossec/logs/evcharge-correlator-outbox.json`
(capped at 10 MB), so Phase 9 can replay them.

### 15.7 Credentials and enrolment

- Enrolment requires a password from now on: `<auth><use_password>yes</use_password></auth>`
  and `/var/ossec/etc/authd.pass` (0640 `root:wazuh`) = `WAZUH_ENROLL_PASSWORD` from
  `secrets/platform.env`. Native agents get it as `WAZUH_REGISTRATION_PASSWORD` in the
  environment of the install command (stdin, not the command line) and the file is removed
  after enrolment; the DaemonSet gets it from Secret `wazuh-enroll` (namespace `wazuh`).
  Agents already enrolled keep their keys.
- Nothing in the project needs the manager API or the dashboard password: scripts use
  `agent_control`, `agent_groups` and `wazuh-logtest` through `incus exec`. The course
  guide's password files stay where they are and are never copied.
- Configuration changes go into `ossec.conf` between `<!-- evcharge begin -->` and
  `<!-- evcharge end -->`, are checked with `wazuh-analysisd -t` and only then the manager is
  restarted.

### 15.8 Tests

`wazuh/tests/<block>.jsonl`: one JSON log line per case plus an expectation file
(`<block>.expect`: line number → expected rule id or `none`). `wazuh-logtest` uses the
ruleset installed on the manager, so the deploy script installs the rules first and
`wazuh/tests/run_logtest.sh` replays the lines through `wazuh-logtest` and compares the rule
ids. Frequency and composite rules
get sequences in one session. Live tests (a real event → alert → active response) are done
only after the monitoring baseline is computed, because they change the load the baseline
measures.

---

## 16. City network ACLs (Phase 6)

Until Phase 6 the city networks can reach every central NodePort and each city's Prometheus
API (no authentication) is readable from its own network (14.8, stated limitation). Phase 6
closes this with **Incus network ACLs** on the three `city-*` networks: Incus-native, so
they behave identically on this laptop and on the Debian VM (unlike host nft rules, which
the course-way rule discourages), and they are applied and removed by one script so the
hijacking PoC can show a rogue station's bypass attempt **before** (reaches the NodePort,
refused by the CSMS at `ident.untrusted_peer`) and **after** (stopped at the network, never
reaching the CSMS).

Model: one ACL per role, attached to the relevant instances' NICs (the ACL egress rules
apply to traffic leaving the instance; `incus network acl` with `ipv4`/port selectors).
Default for a city network's members: `egress` to the allow list below plus the network's
own gateway (DNS, DHCP) and ICMP, then `drop`; `ingress` from the gateway and same-network
peers only. Addresses are resolved from `incus list` and the network gateways at apply
time, never written into the repo.

Allow list (destinations, from 14.8):

| Role | May reach | Port |
|---|---|---|
| station | its own city edge | 443 (or 80 in a plain city) |
| station | Wazuh manager | 1514, 1515 |
| station | internet (NAT) | 80, 443 — only while `--allow-updates` is set (package installs/builds) |
| edge | K3s nodes | 30900, 30801 |
| edge | Wazuh manager | 1514, 1515 |
| edge | internet (NAT) | 80, 443 — only with `--allow-updates` |
| K3s nodes (central → edge) | city edge | 9090 (federation), 9100, 9113 |

Stations may **not** reach the NodePorts, other cities, other stations, `lab-ca` or the
Prometheus/Grafana NodePorts; the central Prometheus still scrapes the edges (ingress from
the K3s node addresses on 9090/9100/9113 is allowed).

`infra/08_acls.sh [apply|remove|status] [--allow-updates] [city ...]`: idempotent; builds the
ACLs from `incus list` and `CITY_NET`, attaches/detaches them, and prints the effective
rules. `--allow-updates` adds the NAT egress rule (needed when rebuilding station or edge
containers); the normal running state omits it. `remove` restores the open state (needed if
a later phase must rebuild instances). The script never touches non-`city-*` networks.

This ACL set is both the Phase 6 hijacking mitigation (a compromised station cannot talk
straight to the CSMS or pivot to peers) and a standing control for the botnet phase (a bot
cannot reach a C2 outside its allow list on 80/443 unless updates are open; the PoC's C2
runs on the central bridge and is reached only because the test explicitly allows it, which
the report notes as a lab concession). Limitation: ACLs are L3/L4 only; a station is still
free to talk to its own edge, so an attacker who fully controls a station can still abuse
the legitimate OCPP path (that is what the identity checks and rate limits are for).
