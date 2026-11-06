# City Prometheus

One Prometheus per city, running **on the city edge** (`<city>-edge`, Debian package
`prometheus` 2.53.3, systemd, port 9090). It scrapes everything in the city every 5 s and
evaluates the `city:*` recording rules; the central Prometheus federates only those series
every 30 s. Per-charger series never leave the city (contract 7.3, 14.1).

| File | Purpose |
|---|---|
| `prometheus.yml.tmpl` | config template (`@CITY@`, `@REGISTRY@`), installed as `/etc/prometheus/prometheus.yml` |
| `city.rules.yml` | the 18 rules of section 7.3, installed as `/etc/prometheus/city.rules.yml` |
| `deploy_city_prom.sh <city>\|all` | installs, configures and reloads; idempotent |
| `tests/city.rules.test.yml` | `promtool test rules` unit tests with synthetic series |

Also reachable as `infra/03_cities.sh monitoring [city]` (part of `all`).

## Jobs

| Job | Targets | Labels |
|---|---|---|
| `stations` | registry HTTP SD `http://k3s-master.incus:30801/sd/prometheus?city=<city>&kind=station`, refresh 60 s, `honor_labels: true` | `hier_id`, `city`, `station` from the registry; the sim's own `hier_id`/`charger` win |
| `edge-nginx` | `127.0.0.1:9113` | `city`, `hier_id=pt.<city>.edge` |
| `edge-node` | `127.0.0.1:9100` | same |
| `prometheus` | `127.0.0.1:9090` | same |

Global: scrape 5 s, timeout 4 s, evaluation 5 s, external label `site=pt.<city>`. Flags (in
`/etc/default/prometheus`): `--web.listen-address=0.0.0.0:9090`,
`--storage.tsdb.retention.time=12h`, `--storage.tsdb.retention.size=256MB`; no lifecycle
API, reload with `systemctl reload prometheus`. A station registered by its city operator
is scraped within 60 s without editing anything.

## Deploy and query

```bash
monitoring/city/deploy_city_prom.sh all          # or: porto | lisboa | aveiro
incus exec porto-edge -- curl -s 'localhost:9090/api/v1/query?query=city:chargers_connected:sum'
incus exec porto-edge -- curl -s -g 'localhost:9090/federate?match[]={__name__=~"city:.*"}'
```

The script sets `limits.memory=384MiB` on the edge, installs the package without
recommends (no second node exporter), checks the config with `promtool check config`,
and restarts only when the flags changed, reloads when the config or rules changed.

## Tests

```bash
incus file push -r monitoring/city/tests porto-edge/tmp/
incus exec porto-edge -- sh -c 'cd /tmp/tests && promtool test rules city.rules.test.yml'
```

The tests read the installed `/etc/prometheus/city.rules.yml`, so deploy first. They cover
2 stations x 2 chargers: counts and the built `hier_id`, charging and faulted sums, power
gap, message rates and the busiest charger, station up, and edge up (exporter and nginx up,
nginx down, exporter down).

## Add a rule

Add it to `city.rules.yml` (and to `evcommon.metrics.CITY_RECORDING_RULES` and contract
7.3 if the central side uses it), add a case to the test file, run `deploy_city_prom.sh
all`. Keep rules free of the `charger` label: the federation takes everything named
`city:*`.

## Notes on the expressions

Two expressions differ from the text of 7.3 by an added `group_left ()`:

- `city:station_power_gap_watts`: `... - on (hier_id) group_left () ...`. Without it,
  `on (hier_id)` drops `city` and `station` from the result, but 7.3 lists them as labels.
- `city:edge_up:min`: `(up * on (instance) group_left () nginx_up) or up`. Without it the
  product keeps only `instance`, so `min by (city)` yields a city-less series and the
  `or` branch still reports 1 when nginx is down.

Values are unchanged; only the labels are kept.

## Memory (edge limit 384 MiB)

Measured with the 30 chargers connected, about 2250 head series per city: Prometheus RSS
about 83-87 MiB; container anonymous memory about 67 MiB; the rest of what `incus info`
shows is reclaimable page cache from the package install.
