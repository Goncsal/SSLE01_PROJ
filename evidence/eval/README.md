# Scalability evaluation (criterion I)

Measured on 2026-11-10 on a single laptop (23 GiB RAM, 8 cores, about 8 GiB available during the runs; Wazuh, K3s, three city sites and all services running). Every raw query output is in `raw/<point>/` (`summary.txt`, `prom.txt`, `resources.txt`, `wazuh.txt`, `stations_cpu*.txt`, `edge_status_codes.txt`). Reproduce with `eval/scale.sh` (`up K [hb]`, `measure <label>`, `down`, `health`).

## Method and lever

The cheapest valid lever is **more OCPP connections and a higher message rate on the plain-ws Aveiro edge**, using a synthetic connection-load generator (`eval/ocpp_load.py`), not more real stations:

- Real extra chargers via `evsim` were rejected. A charger must exist in the registry (an unknown id is refused), and the TLS cities (porto, lisboa) additionally need a per-charger client certificate (CN = hier-id). Aveiro is plain ws and needs no certificate, so registering nodes costs one `PUT` each with the Aveiro city token.
- Real driver sessions at a larger N would reuse the same 20 Aveiro cards on many chargers at once. The fraud service blocks a card on overlapping sessions, which would have changed the demo's threat state. The generator therefore never starts a transaction: BootNotification, StatusNotification, then Heartbeat every `hb` seconds. It sends no card, billing or meter data.
- Placement: one `systemd-run` unit (`evload`) per Aveiro station container, so traffic takes the real path station, nginx edge, CSMS, registry, Postgres. Stations' own roster, `sim.env` and `evsim` service were not modified (roster sha256 prefix `9dc47d873de0` before and after).
- Load points: N counts all connected chargers (30 real + synthetic). Message rate is raised separately by shortening the heartbeat interval at the maximum N.
- Window: 120 s per point after a settle period. p50/p95/p99 come from `evcharge_csms_ocpp_handle_seconds` (`rate()` over 120 s). EPS is the delta of Wazuh `events_processed` (analysisd state file) over the window. Resources from `kubectl top` (metrics-server), cgroups and `incus list`.

## Results

| Point | N | heartbeat | CSMS msg/s | p50 / p95 / p99 (ms) | Wazuh EPS | Prom head series | CSMS replicas (HPA cpu) | csms CPU total | Wazuh analysisd RSS | Host available |
|---|---|---|---|---|---|---|---|---|---|---|
| baseline | 30 | n/a (real) | 2.0 | 3.8 / 23.0 / 42.4 | 4.9 | 4500 | 2 (15%) | 31m | 1444 MiB | 8593 MiB |
| 1 | 50 | 5 s | 6.8 | 2.6 / 9.5 / 22.0 | 6.8 | 4502 | 2 (23%) | 47m | 1446 MiB | 8350 MiB |
| 2 | 80 | 5 s | 12.7 | 2.4 / 8.7 / 19.4 | 7.3 | 4502 | 2 (28%) | 56m | 1449 MiB | 8452 MiB |
| 3 | 140 | 5 s | 24.0 | 2.2 / 8.2 / 13.6 | 5.2 | 4463 | 3 (34%) | 103m | 1454 MiB | 7998 MiB |
| 4 | 140 | 1 s | 111 | 2.1 / 9.0 / 19.9 | 5.5 | 4548 | 4 (70%) | 280m | 1459 MiB | 8280 MiB |
| 5 | 140 | 0.3 s | 169 handled (363 inbound) | 1.4 / 9.4 / 24.2 | 238 | 4622 | 4 (160%) | 643m | 1424 MiB | 8237 MiB |

Per-city federated series (label `city`) stayed at aveiro 148 to 172, porto 118, lisboa 115 to 118. Federation scrape duration stayed at about 1 to 11 ms per city. Station containers: 1 vCPU, 224 MiB limit, `anon` memory 91 to 101 MiB with 22 generator chargers each, so the generator was not the bottleneck. Host available RAM never fell below 7.9 GiB, so the 1.5 GiB stop rule was never near.

## Ceiling

1. **Connections, 140 chargers (120 in Aveiro) with 5 stations.** The edge `limit_conn` is 24 per source address (nginx, `ocpp_conn`), so one station container carries at most 24 chargers (22 synthetic plus the 2 real ones). 5 stations give 120 Aveiro connections. Going further needs more stations (source addresses) or a higher limit. Handshake `limit_req` (2 r/s, burst 20) is why the generator staggers its connects.
2. **Message rate, about 110 msg/s sustained and about 170 msg/s peak handled.** The CSMS applies a per-charger rate limit of 1 msg/s (burst 20). At a 1 s heartbeat every charger sits at that limit and the HPA is at its maximum of 4 replicas with CPU 70% of the request (100m per pod). At 0.3 s the limit engages: `evcharge_csms_rate_limited_total` runs at about 198/s next to 169 msg/s handled, the 4 replicas reach 160% of their CPU request, Postgres 61m and the registry 49m. Request latency did **not** degrade (p95 stays 8 to 9 ms, p99 up to 24 ms) because throttled calls are answered cheaply; the component that saturated is the **CSMS tier at HPA maxReplicas=4**, with the application rate limiter as the protection.
3. **Wazuh, EPS 5 to 238.** The rate-limit refusals are logged, so Wazuh events rose about 45x (238 EPS) and alerts written went from 58 to 2160 in two minutes. `events_dropped` stayed 0 and analysisd RSS stayed flat at about 1.4 GiB, so the manager still had headroom at this rate. Alert volume, not memory, is what would hurt first: an abusive client is loud by design.
4. **Prometheus is not a limit here.** Head series stayed at about 4500 to 4600, since monitoring is per station and per city (federation), not per charger. Synthetic chargers expose no metrics, so this says that the series count does not grow with connection count in this design; it does grow with station count (about 6 series per station exporter), which this test did not vary.

Reference rate: 30 real chargers produce 2.0 msg/s (0.07 msg/s per charger with real sessions), so the 110 msg/s sustained rate corresponds to roughly 1,500 real chargers at that behaviour. This is an extrapolation, not a measurement.

## Limitations

- **Single laptop.** All sites, K3s, Wazuh and the load run on one host with shared CPU and RAM. The stopping reasons above are configuration limits (edge `limit_conn`, HPA max, per-charger rate limit), not the host's RAM. Nothing reached the host RAM floor; a larger N was not chased.
- The generator has no charging sessions, no meter values and no card traffic, so the work per message is lighter than real traffic (no billing or fraud calls). Real sessions at the same message rate would cost more CPU per message.
- Heartbeat traffic only; the fraud and billing path was measured at baseline only (dependency p95: billing 43 ms, fraud 91 ms).
- Only Aveiro scaled. TLS cities were not scaled (per-charger certificate minting), so TLS handshake cost is not covered.
- One 120 s window per point, no repetition, so there are no confidence intervals. The 140-charger heartbeat 5 s point was run twice; the second is reported.
- The edge status-code log of the final point shows some 502/504 responses (`raw/n140_hb03/edge_status_codes.txt`), seen while HPA replicas changed; not analysed further.
- Pre-existing `AuthFailureBurst` alerts for porto and lisboa were active during the runs and are unrelated to the Aveiro generator.

## What a real deployment would need to go further

Dedicated hosts for the CSMS, Postgres and Wazuh (the manager and indexer are sized for far above 238 EPS only with real cores and disk), a higher HPA `maxReplicas` with CPU limits and a node pool to back it, a Postgres primary with replicas or connection pooling, one edge address range per station group (or a raised `limit_conn`), load balancing across several Wazuh managers, and an external load generator on separate machines so that the generator CPU is not part of the system under test.
