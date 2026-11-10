# Edge gateway

One nginx per city (`<city>-edge`, hier-id `pt.<city>.edge`, name `edge.<city>.pt.lab`) between
the chargers and the central CSMS. Contract: `docs/interfaces.md` section 11.

## What it does

- Accepts the chargers' OCPP WebSockets on `/ocpp/pt.<city>.st-NNN.cp-N` only (other paths 404,
  requests without `Upgrade: websocket` 426). It does not check that the id belongs to the
  city; the CSMS does and logs it.
- Sets `X-Real-IP`, `X-Client-Cert-DN`, `X-Client-Cert-Serial`, `X-Client-Cert-Verify` and
  `X-Edge-Id` with `proxy_set_header`, so a client cannot forge them.
- Limits handshakes per source address (`limit_req` 2/s burst 20, `limit_conn` 24, both 429).
- Forwards to NodePort 30900 on every K3s node, with failover.
- Writes one JSON line per request for Wazuh and exposes nginx and node metrics for Prometheus.

## Two modes

| Mode | Listeners | Client certificate |
|---|---|---|
| `--tls` (default) | 443 `wss://`, TLS 1.2/1.3, server certificate `pt.<city>.edge`; 80 answers 426 | requested, not required (`ssl_verify_client optional`, depth 1, CRL). No certificate: forwarded with `Verify: NONE`, the CSMS refuses it. Invalid, expired, revoked or foreign-CA certificate: nginx answers 400 and the edge log shows `cert_verify` `FAILED:...` |
| `--plain` | 80 `ws://` | none; certificate headers are empty. Used by the legacy city (`PLAIN_WS_CITIES`, aveiro) to show the hijacking/MitM case before TLS is enabled |

Both: `127.0.0.1:8080/stub_status` only on loopback. Switching a city = `deploy_edge.sh <city> --tls`
and pointing its stations at `wss://`.

## Failover

The upstream lists `<node>.incus:30900` for each node in `K3S_NODES`, with `max_fails=0`
(no passive health marking), `proxy_next_upstream error timeout`, `proxy_next_upstream_tries 2`,
`proxy_connect_timeout 2s`. The NodePort uses `externalTrafficPolicy: Local`, so a node without
a CSMS pod refuses the connection and nginx tries the other node at once. The access log shows
the chain: `"upstream":"A:30900, B:30900","upstream_status":"502, 101"`.
`max_fails=0` is deliberate: with long-lived WebSockets nginx only clears a peer's failure count when
a proxied connection ends, so after one outage (both nodes failing at once during a rollout) a
peer would be skipped for hours and most reconnects would get 502 "no live upstreams".

## Access log

`/var/log/evcharge/edge-pt.<city>.edge.json`, `log_format ... escape=json`. Fields: `ts`, `level`
(info below 400, warning below 500, else error), `service` (`edge`), `instance`, `event`
(`edge.access`), `msg`, `hier_id`, `srcip`, `http_status`, `tls`, `cert_verify`, `cert_dn`,
`cert_serial`, `upstream`, `upstream_status`, `duration_s`, `bytes_out`. nginx writes the line
when the request ends: for an accepted charger that is when its WebSocket closes, with the
whole session in `duration_s`; refusals are logged at once.

## Scripts

| Script | Purpose |
|---|---|
| `render.sh <city> --tls\|--plain [--upstreams "h:p ..."]` | prints the configuration from `nginx.conf.tmpl`; `EDGE_RATE`, `EDGE_BURST`, `EDGE_CONN` override the limits |
| `deploy_edge.sh <city> [--tls\|--plain] [--instance N] [--pki-skip] [--upstreams ...]` | installs nginx and the exporters (nginx exporter on :9113, node exporter on :9100), exports certificates with `pki/export.sh` (TLS only), pushes the configuration to `/etc/nginx/conf.d/evcharge.conf`, `nginx -t` (rolls back on failure), installs a systemd drop-in (`Restart=on-failure`, `RestartSec=5`, after `network-online.target`) so nginx keeps retrying when the K3s names do not resolve yet after a host reboot, reloads. Idempotent |
| `push_crl.sh [--instance N] [city ...]` | streams `/root/ca/crl.pem` from `lab-ca` to each edge's `/etc/evcharge/pki/crl.pem`, `nginx -t && systemctl reload nginx`; skips plain edges; exit 1 if any fails |
| `tests/run_edgetest.sh` | acceptance test in a throwaway instance `edgetest` with its own CA (keys generated inside it, instance deleted afterwards) |

## Limitations

- The edge-to-CSMS link is plain HTTP inside the lab; TLS with the edge certificate is future work.
- An expired CRL (7 days) makes nginx refuse every client certificate: run `pki/crl.sh` and
  `push_crl.sh` at least weekly.
- A reload applies a new CRL to new handshakes only; open WebSockets stay on the old worker, so
  the revoke flow also disconnects the charger through the CSMS.
- Limits act on handshakes, not on frames inside an open WebSocket (the CSMS limits those).
- Upstream names are resolved when nginx loads the configuration; a changed node address needs a reload.
