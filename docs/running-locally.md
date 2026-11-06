# Running the stack locally

Phase 1 runs every service as a plain local process: no TLS, no containers, no edge
gateway. The contract (ports, variables, log and metric names) is in
[interfaces.md](interfaces.md). The scripts below are in `dev/`.

## Prerequisites

- Python 3.13 or newer (`python3 --version`); set `PYTHON=/path/to/python3.13` if the
  default `python3` is older.
- `bash`, `curl`. Nothing else: the scripts create `.venv` at the repo root and install
  `common`, `registry`, `csms`, `sim` (pinned by `constraints.txt`) and `httpx` into it.
- Everything listens on `127.0.0.1`.

## Quick start

```bash
dev/run_local.sh --fresh      # venv, secrets, registry (seeded from dev/roster.yaml), CSMS, 15 simulators
dev/smoke.sh                  # PASS/FAIL per check; exit 1 on any failure
dev/smoke.sh --sessions --wait 900   # also wait for a finished charging session
dev/stop_local.sh             # TERM every process, KILL after 10 s
```

`run_local.sh` options: `--fresh` (delete `data/*.db` and `logs/*` first), `--roster PATH`
(default `dev/roster.yaml`), `--sim-args "..."` (extra flags for every simulator, e.g.
`--sim-args "--arrival-rate 12"`). It refuses to start if a stack is already running or if
another process already answers on port 8001 or 8000.

`run_local.sh` prints the URLs when it is up. The ports are:

| Process | Address | Notes |
|---|---|---|
| registry | `http://127.0.0.1:8001` | `/health`, `/nodes`, `/locate/pt.porto` |
| CSMS API | `http://127.0.0.1:8000` | `/health`, `/connections`, `/metrics`; needs `CSMS_API_TOKEN` |
| CSMS OCPP | `ws://127.0.0.1:9000/ocpp/<hier-id>` | chargers connect here |
| simulator, one per station | `http://127.0.0.1:<port>/metrics` | ports 9301 onwards, one per station, in roster order |

## Secrets

`dev/run_local.sh` writes `secrets/local.env` on the first run: a random token (at least
24 characters) for each `*_TOKEN` name in `secrets.example/platform.env.example`. The file
is mode 600 and gitignored. Delete it to get new tokens. The scripts source it; to use a
token by hand:

```bash
set -a; . secrets/local.env; set +a
curl -H "Authorization: Bearer $CSMS_API_TOKEN" http://127.0.0.1:8000/connections
```

## Where things live

| What | Where |
|---|---|
| virtual environment and its install stamp | `.venv/` (stamp: `.venv/.deps-stamp`) |
| registry and CSMS databases | `data/registry.db`, `data/csms.db` |
| process output (stdout and stderr) | `logs/<name>.out`, e.g. `logs/registry.out`, `logs/sim-pt.porto.st-001.out` |
| JSON event lines | `logs/csms-<instance>.json`, `logs/sim-<station>.json` (one file per process) |
| pids of the running stack | `logs/run_local.pids` (`<pid> <name>` per line) |
| tokens | `secrets/local.env` |
| roster | `dev/roster.yaml` (regenerate with `dev/gen_roster.py`) |

Watch the events of one station or charger:

```bash
tail -f logs/sim-pt.porto.st-001.json
grep '"hier_id":"pt.porto.st-001.cp-2"' logs/csms-*.json | tail
```

## Metrics

```bash
curl -s http://127.0.0.1:9301/metrics | head      # station pt.porto.st-001 (simulator)
curl -s http://127.0.0.1:8000/metrics | grep evcharge_csms_connected_chargers
```

Station ports are listed in `dev/roster.yaml` (`dev_metrics_port`). The simulator serves
only the station's own metrics; the CSMS metrics cover all chargers.

## Running one service by hand

Start from the repo root with the venv active or `.venv/bin/python` as the interpreter.
Load the tokens first (`set -a; . secrets/local.env; set +a`). Set `LOG_DIR=./logs` and
`LOG_STDOUT=false` to match the scripts.

Registry (port 8001):

```bash
DATABASE_URL=sqlite:///./data/registry.db python -m evregistry
```

CSMS (OCPP 9000, API 8000), accepting direct connections as the scripts do:

```bash
CSMS_ALLOW_DIRECT=true REGISTRY_URL=http://127.0.0.1:8001 \
  DATABASE_URL=sqlite:///./data/csms.db \
  CSMS_LOCAL_BLOCKED_CARDS=<comma list of blocked cards in dev/roster.yaml> \
  python -m evcsms
```

Registry seeding (idempotent; safe to repeat):

```bash
python dev/seed_registry.py --roster dev/roster.yaml --local
python dev/seed_registry.py --roster dev/roster.yaml --dry-run   # print the requests only
```

One simulator (one station, all its chargers):

```bash
python -m evsim --roster dev/roster.yaml --station pt.porto.st-001 \
  --metrics-port 9301 --log-dir logs
```

A rogue device is a simulator that runs only some of a station's chargers: `--chargers cp-1`
runs `cp-1` alone. Starting a second process for a charger id that is already connected
(for example a second `--chargers cp-2` process for `pt.porto.st-001` while the station is
up) is the duplicate-id case: the CSMS logs `charger.duplicate_id`. Give the extra process
its own `--metrics-port` (for example 9399) so the ports do not clash.

Run the simulator with `--seed N` to make its driver behaviour repeatable, and with
`--duration 120` to stop it after two minutes.

## Stopping and starting again

`dev/stop_local.sh` stops the processes listed in `logs/run_local.pids`. Databases and logs
stay, so `dev/run_local.sh` (without `--fresh`) resumes from the same state. Use `--fresh`
for an empty network with empty logs.
