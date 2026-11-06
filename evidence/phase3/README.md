# Phase 3 evidence: three cities through the edges

Run of 2026-11-05 (UTC, 21:44 to 22:30). Platform: K3s on the VMs `k3s-master` and `k3s-node1`
(`K3S_NODE=vm`), namespace `evcharge`; `lab-ca` for the PKI; three city networks. Addresses are
resolved by name (`ipof`, `*.incus`, `*.pt.lab`) and are not recorded here.

## Topology

| City | Network | Edge | Mode | Stations (5 each, 2 chargers each) |
|---|---|---|---|---|
| Porto | `city-porto` | `porto-edge` (`edge.porto.pt.lab`) | mTLS (`wss://`) | `porto-st-001` .. `porto-st-005` |
| Lisboa | `city-lisboa` | `lisboa-edge` (`edge.lisboa.pt.lab`) | mTLS (`wss://`) | `lisboa-st-001` .. `lisboa-st-005` |
| Aveiro | `city-aveiro` | `aveiro-edge` (`edge.aveiro.pt.lab`) | plain (`ws://`, legacy) | `aveiro-st-001` .. `aveiro-st-005` |

Central side: `k3s-master`, `k3s-node1` (CSMS x2, one per node, registry, billing, Postgres),
`lab-ca`. The ConfigMap `csms-edge` holds `CSMS_ALLOW_DIRECT=false` and the three edge
addresses as trusted proxies. All city instances are unprivileged containers; the temporary
`porto-rogue` and `aveiro-rogue` (image `evcharge-city`) existed only during the scenarios and
were deleted. Keys of the real chargers live in their stations; the stolen-certificate test copied
one pair into `porto-rogue` straight from `lab-ca` and the instance was deleted afterwards.

Clocks differ: the city containers run about 2 s ahead of the K3s VMs, so a CSMS
line and the matching edge or station line differ by about 2 s in the excerpts.

## Commands

```
infra/03_cities.sh status
pki/revoke.sh <id>; pki/issue.sh <id> --force; pki/export.sh <id> <station> --group 10001
incus exec <station> -- systemctl stop|start|restart evsim
curl -H "Authorization: Bearer $T" http://$M:30800/connections            # /connections/<id>
curl -X POST ... /commands/<id>/disconnect | change-availability          # CSMS API
curl -X POST ... :30801/quarantine/<id>   and   /quarantine/<id>/release   # registry
kubectl -n evcharge delete pod <csms pod> ; kubectl -n evcharge rollout restart deployment/csms
incus exec <rogue> -- /opt/evcharge/venv/bin/python -m evsim --roster ... --station ... --chargers ... --csms-url ...
```

## Baseline (stations as deployed, default session length)

Window 21:44:15 to 22:01:42 (17 minutes), before the fixes listed in the report.

| Check | Result |
|---|---|
| Connected through the edges | 30 of 30 (`connections.json`): porto 10 `tls=true`, lisboa 10 `tls=true`, aveiro 10 `tls=false`; 15 per CSMS pod |
| `src_ip` of each charger | equals the address of its own station container (0 mismatches of 30) |
| Sessions closed and billed in the window | porto 4 (1.38 EUR), lisboa 7 (3.88 EUR), aveiro 5 (4.45 EUR) |
| `error` lines or tracebacks in CSMS logs | 0 and 0 (both pods) |
| `ident.*` lines in the window | 0 |
| Edge logs, `error` and `warning` lines in the window | 0 (an edge writes a line when a WebSocket ends, so the window holds no entry) |
| Station logs, `error` lines and `journalctl` tracebacks | 0 in all 15 stations |

## Scenarios

Each block: expected, observed, proving lines. Complete lines are in `csms-sample.jsonl` and
`edge-sample.jsonl`.

### 1. No certificate, Porto against Aveiro (22:03 to 22:05)

Porto: `porto-rogue` ran `evsim --station pt.porto.st-001 --chargers cp-1` against
`wss://edge.porto.pt.lab/ocpp` with a directory that holds only `ca.crt` (it verifies the server and
presents no client certificate). Expected: refused. Observed: HTTP 403 in 0.02 s.

```
csms  22:03:46.889 warning ident.no_cert  hier_id=pt.porto.st-001.cp-1 srcip=<rogue> edge=pt.porto.edge http_status=403
edge  22:03:49     403 GET /ocpp/pt.porto.st-001.cp-1  tls=true cert_verify=NONE upstream_status=403
sim   sim.rejected http_status=403
```

Aveiro: the spare id `pt.aveiro.st-005.cp-3` was registered with the Aveiro token (201),
`aveiro-rogue` connected with `ws://edge.aveiro.pt.lab/ocpp` without any credential. Expected:
accepted. Observed: accepted, boot completed, `tls=false`; the only trace is `ident.ip_mismatch`
(the spare charger inherits the address of `st-005`).

```
csms  22:04:44.497 warning ident.ip_mismatch  hier_id=pt.aveiro.st-005.cp-3 srcip=<rogue> expected=<st-005 address>
csms  22:04:44.502 info    conn.open          edge=pt.aveiro.edge tls=false
edge  22:05:11     101 GET /ocpp/pt.aveiro.st-005.cp-3  tls=false cert_verify="" duration_s=24.712
```

The spare id was deleted from the registry afterwards (204, then 404 on GET).

### 2. Revoked certificate (22:05:38 to 22:06:40)

`pki/revoke.sh pt.porto.st-002.cp-1` (CRL pushed to the Porto and Lisboa edges, Aveiro skipped as
plain), then CSMS `disconnect`. Expected: the reconnects fail at the edge with 400 and never reach
the CSMS. Observed: the charger was kicked at 22:05:37 (CSMS clock) and its first reconnect was
refused at the edge 4 s later; the CSMS has no line for the refused attempts.

```
csms  22:05:37.215 warning conn.kicked  hier_id=pt.porto.st-002.cp-1 reason=command
edge  22:05:43     400 GET /ocpp/pt.porto.st-002.cp-1  cert_verify="FAILED:certificate revoked" upstream="" 
sim   22:05:43.372 warning sim.rejected http_status=400
```

Restore: `pki/issue.sh ... --force` (new serial), registry `PATCH cert_serial`, `pki/export.sh`
to `porto-st-002`, `systemctl restart evsim`. At 22:06:50 the charger was connected again with the
new serial.

### 3. Stolen certificate used from another host (22:07:05 to 22:08:25)

The real `evsim` of `porto-st-003` was stopped, the certificate and key of `pt.porto.st-003.cp-1`
copied into `porto-rogue`, and the rogue connected. Expected: accepted, `ident.ip_mismatch`.
Observed: accepted at 22:07:06 (the certificate is valid, mTLS does not bind it to an address).
Then the real station was started at 22:07:23: its `cp-1` was refused with 409, because the old
connection (the rogue) answered the probe; its `cp-2` connected normally. After the rogue exited
(75 s timeout) the real `cp-1` reconnected at 22:08:21 (reject back-off of 60 s).

```
csms  22:07:06.495 warning ident.ip_mismatch  hier_id=pt.porto.st-003.cp-1 srcip=<rogue> expected=<st-003 address>
csms  22:07:06.505 info    conn.open          tls=true cert_cn=pt.porto.st-003.cp-1
csms  22:07:21.723 warning charger.duplicate_id http_status=409 srcip=<st-003> other_srcip=<rogue>
edge  22:07:23     409 GET /ocpp/pt.porto.st-003.cp-1
```

Answer to the card: the real charger gets 409 (the rogue holds the id and stays alive), it does
not replace the rogue. A stolen certificate therefore both works and locks the legitimate
device out; only the IP mismatch line reveals it (`CSMS_ENFORCE_STATION_IP` is off).

### 4. Wrong city (22:09:05)

`aveiro-rogue` used `pt.porto.st-004.cp-1` through the Aveiro edge. Expected: 403
`ident.city_mismatch`. Observed:

```
csms  22:09:03.737 warning ident.city_mismatch  hier_id=pt.porto.st-004.cp-1 srcip=<rogue> edge=pt.aveiro.edge http_status=403
edge  22:09:05     403 GET /ocpp/pt.porto.st-004.cp-1  tls=false upstream_status=403
```

### 5. Header forgery and edge bypass (22:09:28)

From `porto-rogue`, a WebSocket straight to `ws://k3s-master.incus:30900/ocpp/pt.porto.st-005.cp-2`
with forged `X-Real-IP`, `X-Edge-Id`, `X-Client-Cert-Verify`, `X-Client-Cert-DN`,
`X-Client-Cert-Serial`. Expected: one `ident.spoofed_header` per header, then 403
`ident.untrusted_peer`. Observed exactly that (HTTP 403); the node address is reachable from the
city network (see limitations).

```
csms  22:09:28.943 warning ident.spoofed_header  srcip=<rogue> reason=X-Real-IP        (x5, one per header)
csms  22:09:28.944 warning ident.untrusted_peer  srcip=<rogue> peer=<rogue> http_status=403
```

### 6. CSMS failover (22:26 to 22:28, with the final code)

Pod kill: `kubectl delete pod` of the CSMS pod on `k3s-master` at 22:26:53 (held 15 chargers):
30 of 30 connected again 4 s later; the replacement pod was scheduled on `k3s-master`. The edge
logs show the retry through the second node (written when those sessions ended):

```
edge  22:27:25  101 GET /ocpp/pt.porto.st-001.cp-2  upstream="<master>:30900, <node1>:30900" upstream_status="504, 101"
```

`kubectl rollout restart deployment/csms` at 22:27:17 (one pod replaced after the other):
count 30, then 0 at t+8 s (all 30 chargers were lost, each pod was replaced in turn), 15 at
t+9 s, 30 at t+11 s; rollout complete at t+21 s.

Two defects were found and fixed during this scenario (see the report): the same rollout with the
first version took 247 s to reach 27 of 30 (edge upstream marking, count minimum 1) and the
chargers reconnected after 16 to 30 s (simulator back-off never reset). With the first edge fix
alone the rollout took 25 s to reach 30 and a single pod kill 31 s.

### 7. Quarantine across replicas (22:10:04 to 22:13:12)

`pt.lisboa.st-002.cp-1` was held by the pod on `k3s-node1`. Registry `POST /quarantine/<id>`
(Lisboa token), then `disconnect` sent to the API of `k3s-master` (relayed to the holder). Expected: refused on
whichever replica the reconnect lands, `ident.quarantined`. Observed over 150 s, three
reconnect attempts, both replicas:

```
csms (pod on master) 22:10:06.649 warning ident.quarantined http_status=403 hier_id=pt.lisboa.st-002.cp-1
csms (pod on node1)  22:11:06.762 warning ident.quarantined http_status=403
csms (pod on master) 22:12:06.804 warning ident.quarantined http_status=403
edge  22:10:08 / 22:11:08 / 22:12:08  403 GET /ocpp/pt.lisboa.st-002.cp-1  (upstream alternates between the nodes)
```

The first refusal arrives 4 s after the command, on the replica that did not hold the charger,
which had no stale cached "active" entry (cache-drop broadcast). Release at 22:12:42; the charger
reconnected at 22:13:06 (60 s back-off) and `change-availability Operative` returned `Accepted`.

### 8. Handshake flood (22:09:41)

50 parallel handshakes from `porto-rogue` to `wss://edge.porto.pt.lab/ocpp/pt.porto.st-001.cp-1`
(no client certificate). Expected: 429 at the edge. Observed: 29 answered 429 at the edge, 21
reached the CSMS and got 403 (`ident.no_cert`, 21 lines).

```
edge  22:09:42  429 GET /ocpp/pt.porto.st-001.cp-1  srcip=<rogue> upstream="" duration_s=0.000   (x29)
```

## Memory

Memory used inside each instance (`free`, MiB, without page cache); `incus info` "Memory (current)"
also counts cache and reads about 120 MiB per station and 145 MiB per edge.

| Instance | Used | Limit |
|---|---|---|
| 15 stations (`<city>-st-NNN`) | 49 to 50 | 160 MiB |
| 3 edges | 26 to 28 | 256 MiB |
| `lab-ca` | 48.5 (incus current) | 64 MiB |

| Node (`kubectl top nodes`) | CPU | Memory |
|---|---|---|
| k3s-master | 219m (10%) | 1426 Mi (50%) |
| k3s-node1 | 85m (4%) | 852 Mi (29%) |

Pods: csms 65 and 68 Mi, billing 65 Mi, registry 66 Mi, postgres 63 Mi. Host: 23804 MiB total,
6467 MiB available at the end (7263 before the cities, 6494 after them).

## Known limitations

- Nothing separates the city networks from the central bridge: a device in a city can reach the
  K3s NodePorts directly (scenario 5). The CSMS refuses it, but a network rule would stop it earlier.
- mTLS binds the identity to the certificate, not to the host. A copied certificate and key work
  until revoked; `ident.ip_mismatch` is only logged (`CSMS_ENFORCE_STATION_IP=false`).
- In the plain city a rogue device is accepted whenever the id is free (scenario 1); only
  `ident.ip_mismatch` is left as a trace.
- An edge writes its access line when the WebSocket ends, so long sessions are invisible in the edge log
  until they close; refusals are logged at once.
- A charger refused with an HTTP status waits 60 s before the next attempt, so a transient 502 costs
  a minute.
- The baseline ran before the three fixes; a shorter re-check after them (30 of 30, no `error` lines
  in the restart tests) is not a new 15-minute soak.
- Clocks of the city containers run about 2 s ahead of the K3s VMs.
