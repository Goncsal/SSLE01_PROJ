# Phase 2 evidence: local simulators against the CSMS on K3s

Run of 2026-11-05 (times in UTC). Platform: Incus VMs `k3s-master` and `k3s-node1`
(`K3S_NODE=vm`), images from the lab registry in the `docker` instance. Namespace `evcharge`.
Node addresses come from `ipof` and are not recorded in files.

## Commands

```
infra/04_build_push.sh csms billing registry sim      # tag 20261105-2031 (and latest)
infra/secrets_to_k8s.sh --generate                    # secrets/platform.env, mode 600
infra/05_deploy.sh                                    # IMAGE_TAG=latest
REGISTRY_URL=http://<master>:30801 python dev/seed_registry.py      # 51 of 51 requests ok
dev/run_sims.sh --fresh --csms-url ws://<master>:30900/ocpp,ws://<node1>:30900/ocpp \
                --sim-args "--session-minutes 2"
dev/run_sims.sh --stop
```

Image tags: `evcharge-{csms,billing,registry,sim}:20261105-2031`, `postgres:17-alpine` (mirror).
Pods, services, HPA and PVC at the end of the run: `pods.txt`.

## Placement

postgres-0 on k3s-master (PVC 2Gi local-path); registry and billing on k3s-node1; CSMS
2 replicas, one per node (HPA 2..4, `cpu: 38%/70%` under load). `restart-spread.txt`: two
`rollout restart` in a row keep one CSMS pod per node (strategy `maxSurge: 0,
maxUnavailable: 1`; before, the surge pod could land next to an old one).
Memory at the end of the run (`kubectl top nodes`): k3s-master 1420Mi (49%), k3s-node1 852Mi
(29%). Pods: csms 68Mi each, billing 65Mi, registry 64Mi, postgres 71Mi.

## Soak (15 minutes, 15 simulator processes, 30 chargers, session length 2 min)

| Check | Result |
|---|---|
| Connected chargers | 30 on `GET /connections` through both nodes; 16 + 14 over the two pods |
| Closed sessions in billing since the start | 32 (aveiro 9, lisboa 12, porto 11); all with cost > 0 |
| `error` lines / tracebacks (`kubectl logs` of csms, billing, registry) | 0 / 0 |
| Same, `/var/log/evcharge/*.json` on both nodes (since the start) | 0 |
| Simulator logs: `error` lines, tracebacks, "Task was destroyed" | 0 / 0 / 0 |
| Billing on Postgres | `numeric(10,2)` cost, `timestamptz` times; e.g. 121 Wh closed with cost 0.04 |

A first soak was discarded: the CSMS database was created by an older image and lacked the
column `sessions.last_meter_at` (the code creates tables but never alters them), so the CSMS
logged one `dependency.error` (ProgrammingError) per minute. The column was added by hand
(`ALTER TABLE ... ADD COLUMN`), the CSMS pods restarted and the soak repeated from zero.

## Scenarios

- Pod kill (`kubectl delete pod` of the CSMS pod on k3s-master): the replacement was
  scheduled on k3s-master; all 30 chargers were connected again after 14 s; the table
  `connections` held only rows of the two live pods; the orphan transactions were closed at
  the next boot: `session.stop "open transaction closed at the next boot" ... transaction_id 78`
  (`stop_reason=PowerLoss`). No `charger.duplicate_id` in the new pods.
- Cross-replica command: `pt.aveiro.st-002.cp-1` is held by pod `...-xqwxg`; two
  change-availability `Inoperative`/`Operative` pairs sent through the API of both nodes
  returned `Accepted` with `"pod":"csms-5b57bf5f7f-xqwxg"`. The receiving pod (`...-449tz`)
  logged `command.sent` and `command.result` ("command change-availability: Accepted") for the
  three commands it relayed.
- Cross-replica duplicate: a second simulator for the same id pointed at the other node got
  HTTP 409 (`sim.rejected http_status 409`); the CSMS logged `charger.duplicate_id` with
  `srcip` and `other_srcip` (both are the host here). The original connection stayed up.
- Fraud plumbing off (`FRAUD_URL` empty): no log line mentioning fraud in any CSMS pod.

Excerpts: `sample-events.jsonl` (CSMS events, trimmed).

## Known limitations

- The CSMS tables are created at start-up but never migrated; a schema change needs a manual
  `ALTER` or a new database.
- `externalTrafficPolicy: Local` on `csms-ocpp`: a node without a CSMS pod refuses OCPP
  connections. The sim round-robin uses both nodes for this reason.
- `/connections` and commands through a NodePort may reach either pod (the API Service does
  not use `Local`); results carry the owner pod in `pod`.
- Sources are the host address for all simulators, so `srcip`/`other_srcip` coincide.
- Billing runs with one replica (seeding from the roster on an empty `cards` table).
- Memory: master at 49% with Postgres and a CSMS pod on it; a third CSMS replica would
  have to share a node (HPA maximum is 4).
