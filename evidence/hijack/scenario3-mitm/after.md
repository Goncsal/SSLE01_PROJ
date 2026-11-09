# Scenario 3 -- routed MitM on the Aveiro link, AFTER mitigation (defeated, not re-staged)

The BEFORE run (`../timeline.md`) already proved the attack works (46 rewritten
`MeterValues`, 48x Wazuh 110116). For the AFTER state, `mitm_aveiro.sh --run --after` was
attempted once with PASS A's mitigations already live (edge ACLs applied, Aveiro on mTLS,
`CSMS_ENFORCE_STATION_IP=true`); it failed at step 1, before the interceptor could even
install `iptables`, let alone attempt to redirect or rewrite anything -- see
`mitm-aveiro-after-run.log` in this directory. We did not re-stage it by weakening a
mitigation to force it further: doing so would require deliberately turning a defense back
off, which is the wrong way to demonstrate that the defenses hold. The result (no
interception possible) is certain from two independent causes, documented below instead,
each already verified separately this session.

## Cause 1 -- the edge ACL denies the interceptor, confirmed by the live failure

```
W: Failed to fetch http://deb.debian.org/debian/dists/trixie/InRelease  Cannot initiate
   the connection to deb.debian.org:80 ... - connect (101: Network is unreachable)
...
E: Unable to locate package iptables
```
`infra/08_acls.sh`'s rule model (`infra/08_acls.sh`, `build_city()`) only grants egress to
two roles on a city network: `STATIONS` (resolved from instances matching `^$city-st-
[0-9]+$`) and `EDGE` (resolved from `ipof "$city-edge"`). `aveiro-interceptor` -- an
instance `mitm_aveiro.sh` launches on the SAME `city-aveiro` network so it can reach the
real edge -- matches neither pattern, so it has NO source role in any egress-allow rule.
With `security.acls.default.egress.action=drop` (set for all three cities, see
`evidence/hijack/mitigations/acl-reachability.md`), every outbound packet from the
interceptor that isn't explicitly allowed is dropped by default -- including its own
`apt-get update`, which is why it cannot even install the `iptables` package needed to set
up the REDIRECT rule. The MitM infrastructure cannot stand up at all under the current ACLs.

(Before this ACL, scenario 3's BEFORE run did install `iptables` and intercept
successfully -- the ACL applied in between is the only thing that changed.)

## Cause 2 -- mTLS cert-pinning would also reject it, independent of the ACL

This was verified in detail earlier this session by reading the actual source, not
assumed: `mitmproxy==12.3.1`'s `tls_start_client` (`mitmproxy/addons/tlsconfig.py`)
unconditionally presents mitmproxy's OWN self-signed certificate to the client unless an
addon or an `ignore_hosts`/`allow_hosts` option says otherwise -- `mitm_aveiro.sh` sets
neither. The station's own OCPP TLS client (`sim/src/evsim/charger.py`,
`_build_ssl_context`) pins `ssl.CERT_REQUIRED` + `check_hostname=True` against the lab CA
only. So even if the ACL problem above did not exist (e.g. if the interceptor's egress were
somehow permitted), the station's TLS handshake with "the edge" (really the interceptor
impersonating it) would fail at the certificate-verification step, client-side, before any
WebSocket upgrade exists -- and mitmproxy's default `connection_strategy` is `lazy`, so it
would not even open an upstream connection to the real edge until a request arrives on an
already-established client connection, which never happens. This mechanism does not depend
on the ACL, on `CSMS_ENFORCE_STATION_IP`, or on the CSMS at all -- it is purely the
station's own certificate pinning refusing to trust a certificate it doesn't recognise.

## Honest summary

The BEFORE run is the proof the attack works on an unmitigated plain-`ws://` link. The
AFTER state blocks the exact same attack at two INDEPENDENT layers, confirmed respectively
by a live failure (the ACL) and by source-level analysis of both sides of the TLS handshake
(the cert pinning) -- either one alone would already stop it; staging a live AFTER
rewrite-and-detect run would require deliberately disabling one or both on purpose, which
contradicts the point of this evidence (that the mitigations hold as applied). No
`meter.anomaly` evidence is expected or claimed for an AFTER run because no run that
produces one is possible without first weakening a mitigation.
