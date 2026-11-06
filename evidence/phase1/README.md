# Phase 1 evidence: local end-to-end run

Date: 2026-11-05. Host: one laptop, everything on 127.0.0.1, Python 3.14 in `.venv`
(code kept 3.13-compatible). Roster: 3 cities, 15 stations, 30 chargers, 60 cards.

## Command

```bash
dev/run_local.sh --fresh      # registry :8001, CSMS :9000/:8000, 15 evsim processes :9301-9315
dev/smoke.sh                  # 8 passed, 0 failed (right after start)
# 10 min soak, then
dev/smoke.sh --sessions       # 9 passed, 0 failed
dev/stop_local.sh
```

Unit tests before the run: common 55, registry 22, csms 34, sim 14, all passed.

## Numbers

Soak started 18:50:39Z (CSMS start). Readings taken at 19:02Z (about 12 min), and the
session counts again at 19:17Z (about 27 min, scenarios included).

| Item | Result |
|---|---|
| Chargers connected (`/connections?prefix=pt` count) | 30 |
| `sum(evcharge_csms_connected_chargers)` | 30 (15 stations x 2) |
| Registry `/locate/pt.<city>` | 17 elements per city |
| `session.start` / `session.stop` at 12 min (porto, lisboa, aveiro) | 3/1, 2/0, 9/5 |
| `session.start` / `session.stop` at 27 min | 15/7, 12/5, 20/13 |
| `auth.reject` | 2 `Blocked` (blocked cards drawn), 12 `ConcurrentTx` (a card drawn at two chargers at once) |
| `level` error/critical lines in all JSON logs | 1 (see below) |
| Tracebacks in `logs/*.out` | 0 |

The single error line is a simulator asyncio warning ("Task was destroyed but it is
pending", `Event.wait`) written once by `pt.porto.st-002` when the CSMS was killed
(scenario 6). It does not affect behaviour.

Session completion is slow by design (about 8 min average), so the per-city stop counts
only reach 3 after roughly 10 to 15 minutes. The 12 min counts of lisboa (0 stops) and
porto (1 stop) are below that.

`/locate/pt.lisboa.st-002.cp-1` returns one match, `effective_state=active`, path
`pt.lisboa` > `pt.lisboa.st-002` > `pt.lisboa.st-002.cp-1`, `network=city-lisboa`.

## Scenarios

1. Duplicate id: second sim for `pt.porto.st-001.cp-1` was refused; the real charger stayed connected (`/connections` count 2 for the station).
   - CSMS: `charger.duplicate_id ... "http_status":409,"hier_id":"pt.porto.st-001.cp-1"`
   - rogue sim: `sim.rejected ... "http_status":409`
2. Quarantine: Porto token quarantined `pt.porto.st-002.cp-1` at 19:03:45Z. Remote-stop answered 409 (no open transaction), change-availability Inoperative `Accepted`, disconnect `Accepted`.
   - `conn.kicked ... reason=command`, then `ident.quarantined ... http_status 403` one second later (19:03:46).
   - Release at 19:04:12Z; the charger was back at 19:04:46Z (35 s, inside reject-backoff 60 s + cache 30 s). It stayed `Unavailable` until an Operative change-availability was sent.
3. Tamper: `pt.lisboa.st-003` restarted with `--tamper 0.5 --tamper-mode rollback`.
   - `meter.anomaly ... "reason":"backwards" ... pt.lisboa.st-003.cp-1` at 19:09:03Z.
4. Clone: Porto card `E3EE5BE2ED4120` used by an Aveiro station (19:05:17Z, 4 accepts) and by a Lisboa station (19:18:37Z, 2 accepts), always `auth.accept`.
5. Flood: stub C2 at :8099, `pt.porto.st-004` restarted `--compromised`.
   - `ocpp.rate_limited` (cp-2, msg_rate 16 to 18/s, 172 dropped per 10 s) from 19:12:57Z, then `conn.kicked reason=rate_limit` at 19:13:27Z. No other charger was rate limited or kicked.
6. CSMS restart: CSMS killed with SIGKILL and started again at 19:13:51Z; 30 of 30 chargers connected within 4 s (sims reconnect immediately), metrics sum 30.
7. Delegation: `PUT /nodes/pt.lisboa.st-009` with the Porto token returned 403 `token of 'porto' may not write pt.lisboa.st-009`, logged as `registry.write_denied`.

## Fix made during the run

A charger that gets a refused `StartTransaction` (for example `ConcurrentTx`) sends a
`StopTransaction` for that id, as OCPP asks. The CSMS logged each as `meter.anomaly
reason=no_transaction` (about 12 false anomalies in the first 15 minutes). The CSMS now
ignores the stop of a transaction it refused (`csms/src/evcsms/chargepoint.py`). After the
CSMS restart at 19:13Z no refused start occurred, so the effect was not seen live; the 34
CSMS tests still pass.

## Known limitations

- Errors in the request body are validated before the token scope (a Porto `PUT` with a bad body returns 422, not 403).
- The first-boot `.venv` is created by `run_local.sh`; the logs in this folder are trimmed excerpts, not full logs.
- A released charger stays Inoperative until a new change-availability Operative is sent.
- Login-free local mode only: no TLS, no edge, SQLite, a single CSMS process.

Files: `metrics-csms.txt` (a `/metrics` snapshot at about 27 min), `sample-events.jsonl`
(23 representative log lines).
