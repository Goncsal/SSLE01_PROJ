# evcsms: the central system (CSMS)

The OCPP 1.6J central system of the EV charging network. Chargers (real or simulated)
connect with a WebSocket to `/ocpp/<hier-id>`. Before the upgrade the CSMS runs the ten
identity checks of `docs/interfaces.md` section 5.2 (trusted edge, registry, quarantine,
city, client certificate, station address, duplicate id). After it, it handles the seven
OCPP actions, checks meter readings, rate-limits each connection and keeps sessions. A
small HTTP API lists connections and sends commands (remote stop, availability, reset,
disconnect). Everything is logged as JSON events and exposed as Prometheus metrics with
the names fixed in the contract.

One process runs both servers in one asyncio loop: the OCPP server (port 9000) and the
API (port 8000). Locally it is one process on SQLite; on K3s it runs as 2 to 4 replicas
sharing one Postgres (see "Several replicas").

## Modules (`src/evcsms/`)

| File | Role |
|---|---|
| `__main__.py` | start-up, `run(settings, registry, billing)` |
| `settings.py` | environment variables |
| `identity.py` | handshake checks 1-10 (`IdentityChecker.decide` is the pure part) |
| `chargepoint.py` | OCPP handlers, rate limit, commands to the charger |
| `sessions.py` | open transactions in memory, meter checks |
| `billing.py` | `LocalBilling` (stub) and `HttpBilling` |
| `registry_client.py` | `GET /nodes/{id}` with a TTL cache |
| `fraud.py` | bounded queue and background sender |
| `bus.py` | `LocalBus` (one process) and `PgNotifyBus` (LISTEN/NOTIFY between replicas) |
| `db.py` | tables of section 8.3 (SQLAlchemy, sync, called through threads), replica liveness |
| `api.py` | the FastAPI app |

## Run

```bash
uv pip install -c constraints.txt -e common -e 'csms[test,pg]'     # from the repo root
export CSMS_API_TOKEN=$(python3 -c 'import secrets;print(secrets.token_urlsafe(24))')
export CSMS_ALLOW_DIRECT=true REGISTRY_URL=http://127.0.0.1:8001   # local dev, no edge
python -m evcsms
(cd csms && python -m pytest -q)
```

`CSMS_API_TOKEN` is required (at least 16 characters). The service starts even if the
registry is down; handshakes are then refused with 503 until it is back (or accepted if
`CSMS_REGISTRY_FAIL_OPEN=true`).

## Environment variables

Common ones (`BIND_HOST`, `LOG_DIR`, `LOG_LEVEL`, `LOG_STDOUT`, `LOG_MAX_BYTES`,
`INSTANCE_ID`, `DATABASE_URL`) are in section 3.1. CSMS-specific:

| Variable | Default | Meaning |
|---|---|---|
| `CSMS_OCPP_LISTEN_PORT` / `CSMS_API_LISTEN_PORT` | `9000` / `8000` | listen ports |
| `CSMS_API_TOKEN` | required | bearer token of the API |
| `REGISTRY_URL` | `http://127.0.0.1:8001` | registry |
| `BILLING_URL`, `BILLING_API_TOKEN` | empty | empty = built-in stub |
| `FRAUD_URL`, `FRAUD_API_TOKEN` | empty | empty = no events sent |
| `CSMS_TRUSTED_PROXIES` | empty | edge IPs, CIDRs or host names |
| `CSMS_ALLOW_DIRECT` | `false` | accept connections without an edge (dev) |
| `CSMS_PLAIN_CITIES` | `aveiro` | cities that may connect without a certificate |
| `CSMS_ENFORCE_STATION_IP` | `false` | refuse (not only log) a wrong station address |
| `CSMS_CHECK_CONCURRENT_TX` | `true` | `ConcurrentTx` for a card with an open transaction |
| `CSMS_HEARTBEAT_INTERVAL` / `CSMS_METER_INTERVAL` | `30` / `10` | seconds |
| `CSMS_POWER_TOLERANCE` | `1.10` | factor over the rated power |
| `CSMS_RATE_LIMIT_PER_S` / `CSMS_RATE_BURST` / `CSMS_RATE_KICK_AFTER_S` | `1.0` / `20` / `30` | per-connection limit |
| `CSMS_REGISTRY_CACHE_S` | `30` | registry cache TTL |
| `CSMS_REGISTRY_FAIL_OPEN` / `CSMS_BILLING_FAIL_OPEN` | `false` | behaviour when the dependency is down |
| `CSMS_CMD_TIMEOUT_S` | `10` | wait for a charger's answer to a command |
| `CSMS_METER_FLUSH_S` | `60` | write the last reading of every open transaction to `sessions` this often |
| `CSMS_POD_HEARTBEAT_S` / `CSMS_POD_STALE_S` | `5` / `15` | replica heartbeat; a replica silent this long is dead and its `connections` rows are ignored and deleted |
| `CSMS_LOCAL_BLOCKED_CARDS` | empty | blocked cards of the local billing stub |

`DATABASE_URL` is `sqlite:///./data/csms.db` by default, or
`postgresql+psycopg://csms:<password>@<host>:5432/csms` (needs the `pg` extra). On
Postgres the engine pool is 5 connections plus 5 overflow, and each replica holds one more
dedicated connection for `LISTEN`.

## Several replicas

Every replica runs the same code with its own `INSTANCE_ID` (the pod name) and its own
ports, on the same Postgres database. The tables hold what the replicas share:

- `csms_pods`: one heartbeat row per replica. At start a replica registers itself and
  deletes only its own `connections` rows; it never closes sessions. On a clean stop it
  removes its rows. A replica silent for `CSMS_POD_STALE_S` is dead.
- `connections`: the primary key `hier_id` is the duplicate-id guard. A handshake first
  does a plain `INSERT`. If the id is taken, the owner is probed: its own replica pings the
  socket, another live replica is asked with the `probe` command, a dead or missing
  replica is simply replaced. A live owner gives 409 `charger.duplicate_id`.
- `sessions`: `ConcurrentTx` is decided here, across replicas. Every `CSMS_METER_FLUSH_S`
  the last reading of each open transaction is written (`last_meter_wh`, `last_meter_at`).
  A MeterValues or StopTransaction for a transaction this replica does not hold is
  reloaded from the table. The Stop of a refused start is answered normally (state
  `rejected`). At a charger's BootNotification its open sessions are closed as `PowerLoss`.
- `commands` and `NOTIFY csms_cmd` / `csms_cmd_done`: an API call that lands on the wrong
  replica is handed to the replica that holds the socket, which answers through the row.

Two replicas on one machine (ports must differ):

```bash
export DATABASE_URL=postgresql+psycopg://csms:<password>@<host>:5432/csms
INSTANCE_ID=csms-a CSMS_OCPP_LISTEN_PORT=9101 CSMS_API_LISTEN_PORT=8101 python -m evcsms &
INSTANCE_ID=csms-b CSMS_OCPP_LISTEN_PORT=9102 CSMS_API_LISTEN_PORT=8102 python -m evcsms &
```

The Postgres tests run two replicas in one process (ports 19101/19102, API 18101/18102):
`TEST_DATABASE_URL=postgresql+psycopg://... python -m pytest -q`. Without that variable they
are skipped. They drop and recreate the CSMS tables of that database.

## Handshake answers

| Status | Meaning |
|---|---|
| 403 | `ident.untrusted_peer`, `quarantined`, `city_mismatch`, `no_cert`, `cert_mismatch`, `cert_serial_mismatch`, `ip_mismatch` (only if enforced) |
| 404 | `ident.bad_id` or `ident.unknown` |
| 409 | `charger.duplicate_id`: the id is connected and the old socket answers a ping |
| 503 | the registry is down (and fail-open is off) |

## API examples

```bash
H="Authorization: Bearer $CSMS_API_TOKEN"
curl -s localhost:8000/health
curl -s -H "$H" 'localhost:8000/connections?prefix=pt.porto'
curl -s -H "$H" localhost:8000/connections/pt.porto.st-003.cp-2

curl -s -H "$H" -X POST localhost:8000/commands/pt.porto.st-003.cp-2/change-availability \
  -d '{"connector_id": 0, "type": "Inoperative", "reason": "quarantine", "source": "operator"}'
curl -s -H "$H" -X POST localhost:8000/commands/pt.porto.st-003.cp-2/remote-stop -d '{}'
curl -s -H "$H" -X POST localhost:8000/commands/pt.porto.st-003.cp-2/reset -d '{"type": "Soft"}'
curl -s -H "$H" -X POST localhost:8000/commands/pt.porto.st-003.cp-2/disconnect -d '{"reason": "test"}'
```

Command status codes: 200 the charger answered, 404 not connected, 409 refused by the
CSMS (remote stop without an open transaction), 502 the charger answered with an error,
504 no answer in time. Any command for an id also drops its registry cache entry, so a
quarantine followed by `disconnect` refuses the reconnect at once. On Postgres the drop is
broadcast (`NOTIFY csms_cache_drop, '<hier_id>'`, every replica listens on the command
connection), so a reconnect that lands on another replica asks the registry too. `probe`
does not drop. `command.sent` and `command.result` carry `target`, the replica holding the
socket (absent when the charger is not connected).

Certificate serials are compared normalised (upper case, colons and leading zeros
removed): `00A1B2` equals `A1:B2`.

## Schema upgrade

At start the tables are created and `add_missing_columns()` adds any nullable column
(or one with a server default) that the code has and the database lacks; the added
`table.column` names appear in the `service.start` message. Replicas starting together
are serialised with an advisory lock. Renames, type changes and NOT NULL columns without
a default need a manual `ALTER`.

## Edge settings on K3s

`CSMS_ALLOW_DIRECT` and `CSMS_TRUSTED_PROXIES` come from the ConfigMap `csms-edge`,
created by `infra/05_deploy.sh` only if absent (`true` and empty). The city setup rewrites
it; the CSMS reads it at start, so run `kubectl rollout restart deployment/csms` after.

## Notes

- A transaction stays open if its charger disconnects. Start-up never closes sessions;
  the simulated charger does not resume, so its open sessions are closed (`PowerLoss`) at
  its next BootNotification.
- A Phase 1 SQLite file lacks `sessions.last_meter_at`: start it fresh with
  `dev/run_local.sh --fresh`.
- Meter timestamps from the charger are used for the `over_energy` time step.

## Responder token

`CSMS_RESPONDER_TOKEN` (optional, at least 16 characters, different from `CSMS_API_TOKEN`)
is for the SIEM active responses. It may call `GET /connections`, `GET /connections/{id}`,
`remote-stop`, `disconnect` and `change-availability` with `type: Inoperative`. Anything
else (reset, `Operative`) answers 403 and logs `api.auth_failed` with `reason=scope` and
`operator=responder`. Order of checks: 401, 422, 403. Absent token = feature off.
