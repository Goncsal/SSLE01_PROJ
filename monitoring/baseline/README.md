# Baseline computation

`compute.py` reads a recorded window from the central Prometheus and prints a complete
`baseline.yml` (contract 14.7).

```bash
python compute.py --prometheus http://prometheus.monitoring:9090 --window 24h \
                  --roster ../../dev/roster.yaml [--end 2026-11-07T12:00:00Z]
```

Per city it queries `query_range` (step 60 s) for `city:grid_power_watts:sum`,
`city:ocpp_messages_sent:rate1m` and
`sum(increase(evcharge_csms_card_auth_total{result=~"Blocked|Invalid|Expired"}[10m]))`, then
computes n, mean, population sigma and `upper = mean + 3 sigma`. The load bound is capped at
0.9 x the installed capacity (sum of `max_kw` of the city in the roster). A window with fewer
than 30 samples per series is refused. Output: header comment (window start and end, n, mean,
sigma, whether the cap applied), then recording rules with labels `city` and
`basis="<window>-<YYYYMMDD>"`.

`run.sh [--window 24h] [--end ISO]` runs it in a throwaway pod (`baseline-<pid>`, namespace
`monitoring`, image `docker.incus:5000/evcharge-sim:latest`, script and roster on stdin),
writes `../rules/baseline.yml` and prints the reload step (`infra/06_monitoring.sh`). Nothing
is installed on the host.

Tests (no network): `.venv/bin/python -m pytest -q monitoring/baseline/tests`.
