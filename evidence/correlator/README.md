# Incident correlator: evidence (Phase 9 run, 2026-11-10)

The correlator (`correlator/`, NodePort 30804) joins the Prometheus, Wazuh and fraud-service
alerts into **one incident per root cause** and times `t_first_signal` -> `t_located` ->
`t_mitigated`. All three threats were driven from the **recorded** alerts of the earlier
runs (no attack, no load, no PoC script re-run); only one benign edge stop was induced live.

## Wiring

| Source | Path | Result |
|---|---|---|
| Prometheus | Alertmanager `webhook_configs` -> `http://correlator.evcharge:8004/signals/alertmanager`, bearer from Secret `correlator-token` (monitoring) mounted at `/etc/correlator-token/token`; inhibition rules unchanged | live: `AuthFailureBurst` and `EdgeDown` arrived by themselves |
| Wazuh | `custom-correlator` integration on the manager, URL+token in `ar.env`; two `<integration>` blocks (level >= 7, and rule ids 110405/110410/110209 for the response signals) | outbox of 25 alerts replayed with `custom-correlator --replay` (13 signals after de-dup), outbox now empty |
| Fraud | `CORRELATOR_URL=http://correlator:8004` and `CORRELATOR_API_TOKEN` already in `fraud-env` | env checked, no restart; the fraud alert was replayed to `/signals/fraud` |

Deploy: `infra/04_build_push.sh correlator`, `infra/secrets_to_k8s.sh` (now also builds
`correlator-env` and `correlator-token`), `correlator.yaml` in the kustomization,
`infra/05_deploy.sh`, `infra/06_monitoring.sh`. `/health` and `/metrics` answer on 30804.

## Replay method (`replay.py`)

The recorded alerts are posted to the same `/signals/*` endpoints, in recorded order and with
the recorded spacing. All timestamps are shifted by one constant so the first recorded
arrival happens "now"; the lag between an event's own time and its arrival (the Wazuh
pipeline delay) stays as recorded. Below level 7 only response signals are posted, which is
what the integration forwards. Posting the stale originals unchanged gives hours-long locate
times, because `t_located` is stamped with the correlator's clock; that was seen once (first
outbox replay, botnet incidents at 13 000 s) and is why the replay shifts time.

## One incident per threat (`incidents/*.json`, `GET /incidents/{id}`)

| Threat | Incident | Signals joined | Root cause | locate | mitigate |
|---|---|---|---|---|---|
| hijack | `eee549e3-5f85-4c64-91f1-34d6836bf9fb` | Wazuh 110104 `ident.ip_mismatch`, 110131 confirmed, `registry.quarantine` | the charger `pt.porto.st-001.cp-1` (rogue address 10.31.0.32 is not in the registry) | **3.55 s** | 1.65 s |
| fraud | `14328f62-d7d7-4b83-af0e-8c3a1344dcb1` | fraud `impossible_travel`, Wazuh 110210, `billing.card_blocked`, ar.action ok | the **card** `E3EE5BE2ED4120` (seen at two chargers in two cities) | **0.10 s** | 0.07 s |
| botnet | `e74742f8-1fab-469f-968d-2af9e05d0835` (lisboa), `4936df6d-90f6-45c9-bd1a-7ec3604ddfa8` (aveiro) | Wazuh 110303 x2 stations, 110301, `registry.quarantine` | the **city** (`pt.lisboa`, `pt.aveiro`) | **1.16 s**, 0.75 s | 161 s, 166 s |
| botnet | `d9ad5395-86bf-4388-9e8a-f913b53357ef` (porto) | 110303 x2 | the city `pt.porto` | 1.12 s | none (see limits) |
| infra | `97c7beb4-8bbf-4552-ae8b-f28901ba9393` | Prometheus `EdgeDown` | the edge `pt.aveiro.edge` | 10.05 s (Alertmanager `group_wait`) | n/a |

Times are `t_located - t_first_signal` and `t_mitigated - t_first_signal`. The headline: the
source of a hijack or a cloned card is **located in 0.1 to 3.6 s** of its first signal; a
botnet fleet is located to its city in about 1 s. Metric
`evcharge_correlator_time_to_locate_seconds` (7 observations; p90 bucket 300 s because of the
one live `AuthFailureBurst`, which Prometheus reports 45 s after its `startsAt`).
Hijack timeline (one incident, detect -> locate -> mitigate): `t_first_signal` 18:53:38.456
(event time of `ident.ip_mismatch`), `t_mitigated` 18:53:40.107 (registry quarantine, joined
by hier_id), `t_located` 18:53:42.007 (first signal reaches the correlator, registry walk).
`t_mitigated` can precede `t_located` because it uses the event time and `t_located` our clock.

## De-dup and inhibition

- Repeat: the same `110104` alert posted three more times: incident count stayed 6, the signal's
  `count` went 1 -> 4, no new signal, no new incident (engine identity: rule + hier_id + srcip).
- `aveiro-edge` stopped 18:58:18 for ~3 min (the rule needs `for: 1m` plus `group_wait`), restarted
  19:01:39, 30/30 reconnected by 19:02:09. During it 5 `ChargerOffline` were **suppressed** by
  `EdgeDown` in Alertmanager (`alertmanager-during-edgedown.json`) and never reached the
  correlator: **one** `infra` incident, root cause the edge. 10 chargers dropped; no other incident.

## Clock skew

Wazuh signals use the log line's own `data.ts`; the join window is 120 s plus 3 s skew
tolerance (unit-tested at 122/123/124 s). `t_located` and `t_closed` use the correlator
clock, so a sub-second to ~2 s skew between hosts can show as a small negative or positive
offset (the mitigate-before-locate above).

## Integration bugs fixed

1. Response signals never reached the correlator: they are Wazuh levels 4-5 and the single
   integration block forwards level >= 7. Added a second `<integration>` block by rule id and a
   `wanted()` filter in `custom-correlator` that drops release/unblock/flag events.
2. A station-level `registry.quarantine` did not join the botnet incident whose signals named a
   charger of that station (response kind `infra` differs from `botnet`, so the same-branch rule
   was skipped). Same-branch now also links when either signal is a response (test added).
3. `infra/04_build_push.sh` did not copy `correlator/` into the build context;
   `secrets_to_k8s.sh` did not create `correlator-env`/`correlator-token`; Alertmanager had no
   mount for the token.

## Limitations

- Join heuristics: a response arriving more than window + skew (123 s) after the nearest
  member of its incident is dropped (porto botnet: quarantine 158 s after the beacons). A
  longer `CORRELATOR_WINDOW_S` fixes it at the cost of more false merges.
- A fleet across cities is three incidents (the city is the shared key); the cross-city
  Wazuh rule 110304 carries only one station and a C2 address, which is not a join key. Live
  110304 was not in the recorded evidence. A smarter attacker who splits a hijack across
  chargers and addresses, or a fraudster across cards, breaks the shared key.
- Prometheus `DuplicateChargerID` is city-level and was not joined live (the hijack incident
  is Wazuh only); that join is unit-tested only.
- Open incidents are in memory; a pod restart loses them (closed history is in-pod SQLite).
- Rule 110300 (level 6) is below the forwarding threshold, so the flood alerts do not reach
  the correlator; 110301 does.
- The hijack mitigation signal is the recorded registry log line wrapped as its 110405 alert
  (the original 110405 alert was not captured).
- Live Wazuh forwarding after the correlator went up was not exercised (no level >= 7 event
  occurred in the window); the replay path and the empty outbox show delivery works.

## Screenshot list (not captured)

Alertmanager `:30093` during the edge stop (EdgeDown active, ChargerOffline suppressed); Prometheus
`evcharge_correlator_incidents_total`; `GET /incidents/{id}` of the hijack incident.
