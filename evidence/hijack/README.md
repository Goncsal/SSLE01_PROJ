# W6 -- signal/identity hijacking: evidence index and summary

Lab-only, time-boxed PoC runs against the EV-charging-network platform (Porto/Lisboa are
TLS cities, Aveiro is the legacy plain-`ws://` city). Three exploitation paths, each with a
BEFORE run (the attack succeeds, detected) and an AFTER run (a mitigation blocks it), tied
to the detection/response contract (`docs/interfaces.md` v6 §5.2, §15).

## Scenario 1 -- rogue duplicate id (Porto, TLS city)

`poc/hijack/rogue_dupe.sh`. A rogue container on the same city network, with a certificate
stolen (copied) from the real `pt.porto.st-001.cp-1`, connects WHILE the real station stays
up -> two connections for one identity.

- **Detection**: `ident.ip_mismatch` (110104, Wazuh level 7) then `charger.duplicate_id`
  (109100 on the CSMS side -> the correlation window produces composite **110131** (level
  12, "a second host holds the identity", MITRE T1036/T0849).
- **Response**: Wazuh-sourced active response on the real CSMS: `remote-stop` (rejected --
  nothing was charging), `change-availability` (accepted), `disconnect` (accepted) ->
  registry `quarantine-charger` on the charger id.
- **Collateral damage, observed directly**: the quarantine applies to the whole charger id,
  so the REAL station is also refused (`ident.quarantined`) until released -- visible in
  the evidence as two separate 403 lines for the real station's own srcip.
- Evidence: `scenario1-rogue-dupe/` (csms/, edge/, wazuh/, ar/, timeline.md, verify-output.txt).

## Scenario 2 -- stolen certificate from another host (Porto), check 9 before/after

`poc/hijack/stolen_cert.sh`. Isolates check 9 (`CSMS_ENFORCE_STATION_IP`) from scenario 1's
duplicate-id mechanism by stopping the real station's own connection first, so the rogue's
claim has nothing to collide with.

- **BEFORE** (`CSMS_ENFORCE_STATION_IP=false`): the rogue is **accepted** from a foreign
  srcip; `ident.ip_mismatch` is logged as a warning only. mTLS proves possession of a key,
  not location -- the honest limitation the design note calls out.
- **AFTER** (`CSMS_ENFORCE_STATION_IP=true`, mitigation 1): the exact same rogue is
  **refused, 403**, at the handshake -- confirmed from both the edge and both CSMS
  replicas' logs, two refusal attempts, zero `conn.open` for the rogue's srcip ever.
- Evidence: `scenario2-stolen-cert/{csms,edge,timeline.md}` (before) and
  `scenario2-stolen-cert/after/` (after, with a clock-skew note -- see below).

## Scenario 3 -- routed MitM on the Aveiro plain-ws link, before/after

`poc/hijack/mitm_aveiro.sh`. A routed interceptor (no ARP spoofing; `ipv4.address=none` on
its own isolated network, so it is the station's only gateway) sits between the station and
its edge, rewrites `Energy.Active.Import.Register` in live `MeterValues` frames by 5x.

- **BEFORE** (plain `ws://`): 46 rewritten `MeterValues`, CSMS's own `over_energy`/
  `stop_mismatch` checks catch the physically-impossible readings, **meter.anomaly**
  (110116, MITRE T1557/T1565.002) fires **48 times** across both of the station's
  chargers. The CSMS never proves a MitM directly -- it infers it from physics, exactly as
  the design note frames it.
- **AFTER** (Aveiro switched to mTLS, mitigation 3): defeated at TWO independent layers,
  neither of which needed the other: the edge ACL (mitigation 2) denies the interceptor
  all egress on the city network (it matches neither the `STATIONS` nor `EDGE` ACL role),
  confirmed live -- the interceptor could not even `apt-get install iptables`; separately,
  mTLS cert-pinning on the station's own OCPP client would reject the interceptor's
  substituted certificate even if the ACL problem didn't exist, confirmed from
  `mitmproxy`'s and `evsim`'s own source. Not re-staged live (that would require turning a
  mitigation back off on purpose); documented instead.
- Evidence: `scenario3-mitm/{csms,wazuh,mitmdump,timeline.md}` (before) and
  `scenario3-mitm/after.md` (after, analysis).

## The three mitigations

1. **`CSMS_ENFORCE_STATION_IP` false -> true** (ConfigMap `csms-edge`, CSMS rolled):
   stolen-cert-from-another-host changes from logged-only to a 403 refusal. Cost: a
   station whose address changes must have its registry record updated first (confirmed
   directly during the certificate revocation response below).
2. **City network ACLs** (`infra/08_acls.sh`, default-drop + an explicit allow list):
   closes the direct-NodePort shortcut a rogue container used in scenarios 1/2, confirmed
   live (`evidence/hijack/mitigations/acl-reachability.md`). Found and fixed a real script
   bug in the process (an invalid `name:` field broke ACL creation on this incus version).
   **Known gap**: does not stop station-to-peer-station traffic on the same city bridge --
   the host lacks the `br_netfilter` kernel module, so same-bridge traffic bypasses
   nftables filtering entirely; a human decision (loading it affects every bridge on the
   host, including K3s's own).
3. **Aveiro plain -> mTLS**: applied, proven (scenario 3 AFTER), then reverted to plain
   per the user's final decision -- kept as a one-time proof that it closes the MitM path,
   not as the platform's final state. `CSMS_ENFORCE_STATION_IP` and the ACLs were kept.

## Incident response exercised: certificate revocation + re-issue

Three queued revocation requests (from scenario 1 and from an earlier, now-fixed
`mitm_aveiro.sh` bug) were processed: revoked, CRLs regenerated and pushed, fresh certs
issued and redeployed to the three real chargers involved. A real operational snag was
caught by the health gate and fixed: re-issuing a certificate changes its serial, and the
**registry's own `cert_serial` record** must be updated too (identity check 8), or the new,
legitimate certificate is refused exactly like the compromised one would be. Full trail:
`revocation/revocation-and-reissue.md`.

## Limitations found (for the report)

- **Quarantine collateral damage**: `quarantine-charger` isolates the whole charger id, so
  the genuine station is locked out alongside a stolen identity until an operator releases
  it -- by design (stated in the design note), observed directly in scenario 1's evidence.
- **ACL same-bridge gap**: the default-drop ACLs correctly block routed (inter-subnet)
  traffic (the NodePort shortcut) but not same-bridge (L2) traffic between two instances on
  the same city network, because this host has no `br_netfilter` loaded. The generated ACL
  correctly contains no allow rule for peer-to-peer station traffic, but that alone doesn't
  prove it's blocked on the wire -- it isn't, here.
- **Two-layer MitM defense**: mitigations 2 and 3 independently defeat scenario 3's
  AFTER case; this is a feature (defense in depth), but means the AAfter demonstration
  could not isolate "mTLS alone" without first disabling the ACL, and vice versa.
- **`CSMS_ENFORCE_STATION_IP` requires registered IPs**: both for normal operation (a
  station whose DHCP address changes needs re-registration first, as the design note
  already states) and, newly observed this session, for certificate rotation (the
  registry's `cert_serial`, not just `ip`, must stay in sync with what a station actually
  presents).
- **Clock skew on the K3s VMs**: `k3s-master`/`k3s-node1` were found running ~2h34m behind
  the host and every regular Incus container during PASS B (not present during the
  earlier BEFORE captures, so it developed mid-session). All embedded CSMS log timestamps
  in `scenario2-stolen-cert/after/` are on this skewed clock; noted explicitly there so the
  raw files aren't misread. An operational finding, not something fixed here.

## Evidence index

```
evidence/hijack/
  scenario1-rogue-dupe/        csms/ edge/ wazuh/ ar/ timeline.md verify-output.txt
  scenario2-stolen-cert/       csms/ edge/ timeline.md           (before, enforce-IP=false)
  scenario2-stolen-cert/after/ csms-*.jsonl edge-*.jsonl timeline.md stolen-cert-after-run.log
  scenario3-mitm/              csms/ wazuh/ mitmdump/ timeline.md (before, plain ws)
  scenario3-mitm/after.md                                        (after, analysis)
  mitigations/acl-reachability.md
  revocation/revocation-and-reissue.md
```
