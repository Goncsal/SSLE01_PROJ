# Hijacking PoC (Phase 6, threat 1: signal/identity hijacking)

Design background: the Phase 6 hijacking design note. Interfaces:
`docs/interfaces.md` v6 sections 5.2 (identity checks), 9.2 (simulator), 11 (edge), 12
(PKI), 16 (city ACLs).

Every script here defaults to `--dry-run`: it prints the exact commands and target
names it would use and changes nothing. A live run additionally needs `--run` *and*
the environment variable `EVCHARGE_BASELINE_DONE=1` (set only by the matching
`W6-run` card, once the baseline recording is captured). All scripts are time-boxed
(`--duration`, default 120 s) and delete every temporary Incus instance, network and
route they create on exit and on `INT`/`TERM`.

## Scenarios

### 1. `rogue_dupe.sh` -- rogue duplicate id (Porto, TLS city)

A throwaway `<city>-rogue` container (image `evcharge-city`) on the target city
network reuses a real charger id against the city edge:

- **(a) no client certificate** -- the rogue presents only `ca.crt`. Expected:
  refused before the OCPP upgrade, `ident.no_cert` (110105).
- **(b) stolen certificate** -- the real station's certificate and key are copied
  into the rogue straight from `lab-ca` (`pki/export.sh`), while the real station
  stays connected. Expected: accepted (mTLS proves key possession, not location),
  `ident.ip_mismatch` (110104); whichever of the rogue/real station the CSMS sees
  connect second gets `charger.duplicate_id` (110100); the composite rule fires if
  both land within 300 s of each other: **110130** (duplicate id + a foreign source
  IP -- the rogue is the duplicate) or **110131** (reverse order, where the event's
  `srcip` may be the real station).

```
rogue_dupe.sh [--city porto] [--station pt.porto.st-001] [--run] [--duration N]
```

### 2. `stolen_cert.sh` -- the focused stolen-cert-from-another-host case

The same copy-and-connect step as 1(b) on its own, used for the
`CSMS_ENFORCE_STATION_IP` before/after. This script does **not** change that
setting; the RUN card toggles it (ConfigMap `csms-edge`, CSMS restart) and calls
this script twice with `--phase before` / `--phase after` to label the expected
outcome:

- **before** (`CSMS_ENFORCE_STATION_IP=false`): accepted, `ident.ip_mismatch`
  (110104) logged only.
- **after** (`CSMS_ENFORCE_STATION_IP=true`): refused, 403 at the handshake.

```
stolen_cert.sh [--city porto] [--station pt.porto.st-001] [--phase before|after] [--run] [--duration N]
```

### 3. `mitm_aveiro.sh` -- routed MitM on the plain-`ws` Aveiro link

Builds the interceptor the way Guide 5 built its MitM lab
(`~/Desktop/ssle/practical/guides/g5/06_mitm_setup.sh`, `07_mitm_http.sh`,
`09_cleanup_mitm.sh`): a dedicated network with **no host IPv4 address**
(`ipv4.address=none`), so the interceptor is the station's only route. **No ARP
spoofing on the shared city bridge** -- the same lab-safety choice Guide 5 made, so
the attack is contained to one station and never touches the shared `city-aveiro`
bridge's other members.

Topology: the chosen Aveiro station's single NIC moves onto the new isolated
network; the interceptor gets two NICs, one on that isolated network (the
station's new gateway) and one on `city-aveiro` (reaches the edge normally, like
any other station). A transparent `iptables` `REDIRECT` sends the station's plain
`ws://` traffic to `mitmdump` (mitmproxy, in a venv **inside** the interceptor --
never on the host, as Guide 5 does), running a small addon that rewrites the
`Energy.Active.Import.Register` value in OCPP `MeterValues` frames, so the CSMS
sees physically inconsistent readings. Expected: `meter.anomaly` (110116, logged
only in a plain city).

`--after` documents (and, with `--run`, exercises) the post-mitigation case: once
Aveiro is switched to mTLS (`gateway/deploy_edge.sh aveiro --tls`, station certs
re-issued), the station only trusts the lab CA's edge certificate. The interceptor
has no key the station will accept, so it cannot terminate or rewrite the link --
`mitmdump` sees opaque TLS bytes, there is nothing to tag, and `meter.anomaly`
stops. This is the G5 finding restated for OCPP: correct TLS validation defeats a
network MitM.

```
mitm_aveiro.sh [--station pt.aveiro.st-001] [--after] [--run] [--duration N]
```

The isolated network's addresses are picked at run time (a free `/24` built from
random octets), never written into this repo as literals -- the same convention
the city ACL script (`infra/08_acls.sh`, section 16) uses for the same reason.

### `run.sh` -- orchestrates all three

Runs the three scenarios above in order (rogue duplicate id, then the focused
stolen-cert case `--phase before`, then the Aveiro MitM), printing a banner before
each (what, expected detection, expected response) with a pause between them, and
relies on each sub-script's own cleanup.

```
run.sh [--run] [--duration N]
```

### `selfcheck.sh`

Runs every script above on its `--dry-run` path, checks each exits 0 and prints a
plan, and checks that `--run` is refused without `EVCHARGE_BASELINE_DONE=1`. Exits
non-zero if anything failed. Creates and changes nothing.

## Before/after steps the RUN card performs, in order

1. **Baseline**, nothing from this PoC has run yet. `EVCHARGE_BASELINE_DONE` unset.
2. Run `run.sh --run` (or the individual scripts) for the **before** state of every
   mitigation: `CSMS_ENFORCE_STATION_IP=false`, no city ACLs applied, Aveiro plain.
   Capture evidence (CSMS/edge JSON lines, alerts, registry state) per
   `evidence/hijack/`.
3. **Mitigation 1**: flip `CSMS_ENFORCE_STATION_IP` to `true` in the `csms-edge`
   ConfigMap, restart the CSMS. Re-run `stolen_cert.sh --run --phase after`.
4. **Mitigation 2**: `infra/08_acls.sh apply` on the city networks. Re-attempt a
   direct rogue-to-NodePort connection (outside this PoC's owned scripts, per
   section 16) to show it is stopped at the network instead of reaching
   `ident.untrusted_peer`.
5. **Mitigation 3**: `gateway/deploy_edge.sh aveiro --tls`, re-issue the Aveiro
   station certificates, update `sim.env` to `wss://`. Re-run
   `mitm_aveiro.sh --run --after` to show the interceptor can no longer read or
   rewrite the link.
6. Process the queued revocation (`pki/process_revocations.sh`) and release any
   quarantined charger once the operator is done, per `DESIGN-hijacking.md`
   "Response and justification".

## Lab-safety notes

- **Routed MitM, not ARP spoofing**: the isolated network has no host IPv4 address,
  so the interceptor is the only path between the moved station and the rest of
  `city-aveiro`; the shared bridge and its other members are never touched.
- **Time-box**: every live scenario takes `--duration` (default 120 s) and the
  evsim runs inside it are bounded the same way; nothing runs unattended.
- **Cleanup**: each script registers an undo action per resource it creates
  (instance, network device, route, systemd unit state) and runs them in reverse
  order on exit, success, failure, `INT` or `TERM` -- modelled on
  `09_cleanup_mitm.sh`.
- **Scope**: the rogue/interceptor act only on the configured city network; nothing
  outside the lab is targeted; no secrets or lab addresses are written into this
  repo (names only, resolved at run time, same convention as `infra/08_acls.sh`).
- **G5 gotcha reused**: `mitmdump`, not the interactive `mitmproxy` TUI, so the
  scripts run unattended; the addon lives in a venv inside the interceptor, never
  on the host, so the host's own Python environment is untouched.
