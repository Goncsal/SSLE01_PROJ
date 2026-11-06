# evbilling

Accounting for the charging network. It authorises RFID cards for the CSMS, prices
charging sessions with a tariff and lets incident response block (or unblock) a card.
API: `docs/interfaces.md`, section 4.3. Tables: section 8.4.

## Environment

| Variable | Default | Meaning |
|---|---|---|
| `BILLING_API_TOKEN` | required, 16+ chars | bearer token for every endpoint except `/health` and `/metrics` |
| `BILLING_LISTEN_PORT` | `8002` | |
| `DATABASE_URL` | `sqlite:///./data/billing.db` | SQLite or `postgresql+psycopg://` (extra `pg`) |
| `BILLING_SEED_ROSTER` | empty | roster file; when `cards` is empty, inserts its cards and the `standard` tariff (0.35 EUR/kWh) |
| `BIND_HOST`, `LOG_DIR`, `LOG_LEVEL`, `LOG_STDOUT`, `INSTANCE_ID` | see the contract, section 3.1 | |

## Run and test

```bash
pip install -c constraints.txt -e common -e 'billing[test]' -e csms
export BILLING_API_TOKEN=... BILLING_SEED_ROSTER=dev/roster.yaml
python -m evbilling
cd billing && python -m pytest -q
```

## Examples

```bash
A="Authorization: Bearer $BILLING_API_TOKEN"; H='content-type: application/json'
curl -H "$A" -H "$H" localhost:8002/authorize -d '{"card":"04A1B2C3D4E5F6","hier_id":"pt.porto.st-001.cp-1"}'
curl -H "$A" -H "$H" localhost:8002/sessions/7/stop \
  -d '{"meter_stop_wh":1203456,"stopped_at":"2026-11-06T09:31:10Z","reason":"Local"}'
curl -H "$A" -H "$H" localhost:8002/cards/04A1B2C3D4E5F6/block \
  -d '{"reason":"cloned card","source":"wazuh","incident_id":"INC-7"}'
curl -H "$A" 'localhost:8002/sessions?status=flagged&limit=20'
```

## Responder token

`BILLING_RESPONDER_TOKEN` (optional, at least 16 characters, different from
`BILLING_API_TOKEN`) is for the SIEM active responses. It may call `POST /cards/{card}/block`,
`POST /sessions/{id}/flag`, `GET /cards/{card}` and `GET /sessions`. Unblock and every other
endpoint answer 403 and log `api.auth_failed` with `reason=scope` and `operator=responder`.
Order of checks: 401, 422, 403. Absent token = feature off.
