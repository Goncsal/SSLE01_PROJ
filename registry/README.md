# evregistry

Source of truth for the elements of the charging network: what exists, where it is,
who is responsible (per-city delegation) and whether it is quarantined. The CSMS
checks it on every charger connection; incident response quarantines and locates
elements through it. API: `docs/interfaces.md`, section 4.1.

## Environment

| Variable | Default | Meaning |
|---|---|---|
| `REGISTRY_ADMIN_TOKEN` | required, 16+ chars | NOC token, full scope |
| `REGISTRY_TOKEN_<CITY>` | optional | city operator token (`PORTO`, `PORTO_ALEGRE` is `porto-alegre`); writes only below `pt.<city>` |
| `REGISTRY_LISTEN_PORT` | `8001` | |
| `DATABASE_URL` | `sqlite:///./data/registry.db` | SQLite or `postgresql+psycopg://` (extra `pg`) |
| `BIND_HOST`, `LOG_DIR`, `LOG_LEVEL`, `LOG_STDOUT`, `INSTANCE_ID` | see the contract, section 3.1 | |

All tokens must be different. The service refuses to start otherwise.

## Run and test

```bash
pip install -c constraints.txt -e common -e 'registry[test]'
export REGISTRY_ADMIN_TOKEN=... REGISTRY_TOKEN_PORTO=...
python -m evregistry
cd registry && python -m pytest -q
```

## Examples

```bash
H='content-type: application/json'
# the NOC creates the city, the Porto operator creates a station below it
curl -X PUT -H "$H" -H "Authorization: Bearer $REGISTRY_ADMIN_TOKEN" \
  localhost:8001/nodes/pt.porto -d '{"name":"Porto"}'
curl -X PUT -H "$H" -H "Authorization: Bearer $REGISTRY_TOKEN_PORTO" \
  localhost:8001/nodes/pt.porto.st-001 -d '{"name":"Aliados","lat":41.1496,"lon":-8.6109}'
# where is this charger, and who is responsible?
curl localhost:8001/locate/pt.porto.st-001.cp-1
# quarantine the station: its chargers become effective_state "quarantined"
curl -X POST -H "$H" -H "Authorization: Bearer $REGISTRY_TOKEN_PORTO" \
  localhost:8001/quarantine/pt.porto.st-001 -d '{"reason":"test","source":"operator"}'
```

## Responder token

`REGISTRY_RESPONDER_TOKEN` (optional, at least 16 characters, different from every other
token) is for the SIEM active responses. Operator `responder` may read and
`POST /quarantine/{hier_id}` on any element; release, PUT, PATCH and DELETE answer 403
and log `registry.write_denied` with `operator=responder`. The variable is not
`REGISTRY_TOKEN_RESPONDER` on purpose: that name would be read as the token of a city.
Absent token = feature off.
