# evsim: station simulator

One process simulates one charging station: every charger of that station,
each with its own OCPP 1.6J (JSON over WebSocket) connection, Poisson driver
arrivals with RFID cards, realistic charging sessions and meter values. It
also carries the attack modes used by the proofs of concept: a botnet client,
meter tampering and cloned cards. See `docs/interfaces.md`, sections 5 and 9,
for the full contract.

## Install

```bash
uv venv --python 3.13 /path/to/venv
uv pip install --python /path/to/venv/bin/python -c constraints.txt -e common -e 'sim[test]'
```

## Run

```bash
python -m evsim --roster dev/roster.yaml --station pt.porto.st-003 \
  --csms-url ws://127.0.0.1:9000/ocpp --log-dir ./logs
```

### Flags (section 9.2 of the contract)

| Flag | Meaning |
|---|---|
| `--roster`, `--station` | roster file and the station hier-id to run (env `SIM_ROSTER`, `SIM_STATION`) |
| `--chargers cp-1,cp-2` | run only these chargers of the station; default: all |
| `--csms-url` | base URL; `/<charger hier-id>` is appended (env `SIM_CSMS_URL`) |
| `--pki-dir` | for `wss://`: `ca.crt`, `<charger hier-id>.crt/.key` (env `SIM_PKI_DIR`; untested so far) |
| `--metrics-host`, `--metrics-port` | `/metrics`; `--metrics-port 0` disables it |
| `--log-dir` | JSON log directory (env `LOG_DIR`) |
| `--meter-interval`, `--arrival-rate`, `--session-minutes`, `--home-bias`, `--seed` | the driver and charging model (section 9.3) |
| `--duration` | stop cleanly after this many seconds; otherwise runs forever |
| `--reconnect-max`, `--reject-backoff` | reconnect backoff cap, and the wait after a refused handshake |
| `--compromised`, `--c2-url`, `--beacon-interval` | botnet client (section 9.4) |
| `--tamper F`, `--tamper-mode`, `--tamper-chargers` | meter tampering |
| `--clone-card`, `--clone-sessions` | cloned RFID card |
| `--bot-debug` | print botnet activity to stderr only; never to the JSON log |

Exit codes: `0` on a clean stop (SIGINT/SIGTERM/`--duration`), `2` on bad
arguments or an unreadable/invalid roster.

### Examples

Normal station, both chargers, against a local CSMS:

```bash
python -m evsim --roster dev/roster.yaml --station pt.porto.st-003 \
  --csms-url ws://127.0.0.1:9000/ocpp --log-dir ./logs
```

A rogue device impersonating one charger from another host:

```bash
python -m evsim --roster dev/roster.yaml --station pt.porto.st-003 --chargers cp-1 \
  --csms-url ws://10.31.0.99:9000/ocpp --metrics-port 0
```

Meter tampering (one charger under-reports to 30% of the truth; the feeder
meter metric keeps the truth):

```bash
python -m evsim --roster dev/roster.yaml --station pt.porto.st-003 \
  --tamper 0.3 --tamper-mode scale --tamper-chargers cp-1
```

A cloned card presented for the next 5 sessions at this station:

```bash
python -m evsim --roster dev/roster.yaml --station pt.porto.st-003 \
  --clone-card 04A1B2C3D4E5F6 --clone-sessions 5
```

A compromised station controller beaconing to a C2 server:

```bash
python sim/tests/stub_c2.py --port 8099 --task surge --duration-s 120 &
python -m evsim --roster dev/roster.yaml --station pt.porto.st-003 \
  --compromised --c2-url http://127.0.0.1:8099 --bot-debug
```

## Test helpers

`sim/tests/stub_csms.py` is a minimal accept-everything OCPP 1.6 server, used
by the pytest tests and standalone:

```bash
python sim/tests/stub_csms.py --port 9000 [--block-card CARD]... [--reject-path-substr X]
```

`sim/tests/stub_c2.py` is a tiny C2 server for the botnet attack mode
(standard library only):

```bash
python sim/tests/stub_c2.py --port 8099 --task flood --rate-hz 20 --duration-s 30
```

## Tests

```bash
cd sim && pytest -q
```
