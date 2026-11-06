# evcorrelator

Joins the three detection systems of the platform into **one incident per root cause** and
times the response. It ingests

- **Prometheus** alerts, through the Alertmanager webhook (`POST /signals/alertmanager`),
- **Wazuh** alerts, through the `custom-correlator` integration (`POST /signals/wazuh`),
- **fraud service** alerts (`POST /signals/fraud`),

normalises each to one signal shape, links signals that share a key within a time window,
asks the registry where the evidence points, and records
`t_first_signal` -> `t_located` -> `t_mitigated` for every incident. Contract:
`docs/interfaces.md`, section 4.5 (Wazuh hook: 15.6, Alertmanager webhook: 14.4).

## Environment

| Variable | Default | Meaning |
|---|---|---|
| `CORRELATOR_API_TOKEN` | required, 16+ chars | bearer token of every endpoint but `/health` and `/metrics` |
| `REGISTRY_URL` | required | topology source (`/locate`, `/locate/ip`) |
| `CSMS_API_URL`, `CSMS_API_TOKEN` | empty | read in, reserved for connection look-ups; the walk does not use them yet |
| `CORRELATOR_WINDOW_S` | `120` | join window |
| `CORRELATOR_SKEW_S` | `3` | extra tolerance for clock drift between sources |
| `CORRELATOR_CLOSE_S` | `600` | auto-close after this quiet time |
| `DATABASE_URL` | unset | closed-incident history (SQLite or Postgres). Unset: memory only |
| `CORRELATOR_LISTEN_PORT` | `8004` | |
| `BIND_HOST`, `LOG_DIR`, `LOG_LEVEL`, `LOG_STDOUT`, `INSTANCE_ID` | contract 3.1 | |

## Run and test

```bash
pip install -c constraints.txt -e common -e 'correlator[test,history]'
export CORRELATOR_API_TOKEN=... REGISTRY_URL=http://127.0.0.1:8001
python -m evcorrelator
cd correlator && python -m pytest -q       # fake registry, no real service
```

## Signals

Every source is reduced to `{source, key_type, key, hier_id?, srcip?, card?, city?, kind,
severity, ts, ...}`.

| Source | `kind` | keys | `ts` |
|---|---|---|---|
| Prometheus (`firing` alerts only) | label `threat` | label `hier_id` (city label as fallback) | `startsAt` |
| Wazuh | rule group `hijack`/`fraud`/`botnet`/`infra` | `data.hier_id`, `data.srcip`, `data.card` | the log line's own `data.ts`, else the alert `timestamp` |
| Fraud | `fraud` (the rule is the name) | `hier_id`, `other_hier_id`, `card` | `ts` |

Severity: Prometheus label as is; Wazuh level >= 10 critical, >= 7 warning, else info.
Everything is untrusted: ids, addresses and cards that do not fit their grammar are dropped
(no eval, bodies capped at 1 MB, 200 alerts per batch, strings and nesting bounded, unknown
fields ignored). A signal with no valid key cannot be correlated and is not accepted.

A **response signal** is one that is itself a mitigation: Wazuh `ar.action` with
`result=ok`, `registry.quarantine`, `billing.card_blocked`. It never opens an incident.
Joined into one, it sets `t_mitigated` (and `status=mitigated`). A failed action
(`ar.action` error/refused) is evidence, not mitigation.

## The join (`engine.py`)

For each incoming signal `s`:

1. **De-duplicate.** If an open incident holds a signal with the same identity (Prometheus:
   `alertname` + all labels; Wazuh: `rule.id` + hier_id + srcip + card; fraud: rule + keys)
   and `s` falls inside its repetition window, fold `s` into it (`count += 1`, `last_ts`
   moves). No new signal, no new incident. Alertmanager's repeat notifications and
   re-posted Wazuh outbox lines therefore change nothing.
2. **Link.** `s` links to an incident if it links to any member `m`: the times are
   compatible and a key is shared.
   - Time: `m.ts - T <= s.ts <= m.last_ts + T`, with `T = WINDOW + SKEW`.
   - Key (order of the contract: hier_id, srcip, card): an equal `hier_id`, `srcip` or
     `card`. In addition, two refinements for the topology (not in the contract text):
     hier-ids on the **same branch** (a city alert and one of its stations) link
     when both signals have the same `kind`, and **botnet** signals of the same city link.
   - Because every member counts, keys are **transitive**: a `charger.duplicate_id`
     (hier_id and srcip) binds later srcip-only signals to its incident, and a signal that
     links two incidents merges them (the oldest survives).
3. **Open, join or merge.** No link: new incident (not for a response signal). One: add.
   Several: merge, then add.
4. **Roll up.** `kind` and `severity` are those of the highest-severity non-response
   signal. `keys` is the union. `t_first_signal` is the earliest signal `ts`.
5. **Auto-close** after `CORRELATOR_CLOSE_S` without a signal (arrival time, our clock).
   A later signal for the same keys opens a new incident.

### Clock skew

The city hosts run on the host clock and the K3s VMs drift by about 2 s (Phase 3). Two
signals that belong together can therefore look up to ~2 s further apart than they were.
`CORRELATOR_SKEW_S` (3 by default) is added to the window: with the default window of 120 s,
signals 122 s or 123 s apart join, 124 s do not. Wazuh signals use the log line's own time
(event time on the city host) rather than the manager's alert time, which is the closest
to when the thing happened. `t_located` and `t_closed` are stamped with the correlator's
own clock, so the two time-to-X histograms mix at most one clock difference (also ~2 s).

## Root cause (`locate.py`)

After a signal changes an incident, the correlator walks the registry for **all** of the
incident's signals (answers are cached 30 s):

1. each `hier_id` (and `other_hier_id`) -> `GET /locate/{hier_id}`; the match for that exact
   id gives `path` (city -> ... -> element), `edge`, `station`, `host`, `ip`;
2. each `srcip` -> `GET /locate/ip/{ip}`; only a **station, edge or central** match counts
   (the chargers listed with a station are its residents, not the owner of the address). An
   address nobody owns, such as an attacker's, adds no element and is named in the
   explanation instead;
3. root cause = the **lowest common element**: the longest common prefix of the paths.

   | evidence | root cause |
   |---|---|
   | one charger (or only that charger plus the unowned attacker address) | the charger |
   | several chargers of one station | the station |
   | stations of one city, or a city alert | the city, `pt.<city>` |
   | elements of different cities | `pt` |
   | platform elements only | `pt.central` |

4. **Fraud exception.** If the incident is of kind `fraud`, carries a card and its
   elements do not end in one charger (an `impossible_travel` between two cities, say), the
   actor is the **card**: `root_cause = {key_type: card, key: <card>, hier_id: null}` and the
   explanation lists the chargers where it was seen.

`RootCause` = `{key_type, key, hier_id, host, ip, city, edge, explanation}`. `t_located` is
stamped when a walk first resolves an element **in the registry**; the root cause is then
refined as more signals arrive but `t_located` is never moved. If the registry cannot be
reached the root cause is derived from the ids themselves, marked in the explanation, and
`t_located` stays unset.

Example (one charger, hijack):
`all 2 signal(s) concern pt.porto.st-003.cp-2 (charger), host porto-st-003; source
address(es) not in the registry: 198.51.100.10`.

## API

| Endpoint | |
|---|---|
| `POST /signals/alertmanager`, `/signals/wazuh`, `/signals/fraud` | 202 `{"accepted": n}` |
| `GET /incidents?status=&since=&limit=` | `{"count","items"}`, newest first |
| `GET /incidents/{id}` | `Incident` |
| `POST /incidents/{id}/mitigated` `{"action","by","ts"?}` | sets `t_mitigated` if unset |
| `POST /incidents/{id}/close` `{"by","note"?}` | |
| `GET /health`, `GET /metrics` | no token |

Order of checks: 401, then 422 (bad body or id), then 404/409. Metrics:
`evcharge_correlator_signals_total{source}`, `_incidents_open`, `_incidents_total{kind}`,
`_time_to_locate_seconds`, `_time_to_mitigate_seconds` (both measured from
`t_first_signal`; the report's "located in N s" comes from the first). Log events:
`incident.open`, `incident.update`, `incident.close` (with `t_locate_s`).

## Deployment and wiring (Phase 9, after the baseline)

`k8s/correlator.yaml` (Deployment of 1, Service with NodePort 30804, probes, 50m/96Mi/256Mi,
non-root, read-only root) is **not** in `k8s/kustomization.yaml` until the run enables it.

1. Build and push `docker.incus:5000/evcharge-correlator` (`correlator/Dockerfile`, context
   = repo root), add the manifest and an `images:` entry to the kustomization.
2. Create Secret `correlator-env` from `secrets/platform.env`.
3. Alertmanager: add a `webhook_configs` receiver to
   `http://correlator.evcharge:8004/signals/alertmanager` with
   `http_config.authorization` (bearer token from a mounted secret file).
4. Wazuh: install `wazuh/integrations/custom-correlator` on the manager, set
   `CORRELATOR_API_TOKEN` (and optionally `CORRELATOR_URL`) in `ar.env`. The integration
   block only forwards level >= 7; the response signals (rules 110405, 110410, 110209 are
   levels 4-5) need a second `<integration>` block filtered with `<rule_id>`.
5. Fraud service: `CORRELATOR_URL` and `CORRELATOR_API_TOKEN` (already in its settings).

Outage behaviour: losing the pod loses open incidents; they re-open from the next signals.
Wazuh alerts that could not be delivered wait in the manager's outbox and are replayed with
`custom-correlator --replay`.
