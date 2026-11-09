# Mitigation 2b -- edge ACL reachability evidence

`infra/08_acls.sh apply --run` (all three cities, `EVCHARGE_BASELINE_DONE=1`). Tests below
run from `porto-st-002` via a short Python TCP-connect probe (`incus exec porto-st-002 --
python3 -c '...'`), read-only, captured during PASS A on 2026-11-07.

## BEFORE (captured live, prior to applying any ACL)

```
station -> CSMS NodePort 10.255.255.80:30900  : ok: reachable
```
(The first `apply --run` attempt that session actually failed outright with an incus YAML
error -- see the `infra/08_acls.sh` fix below -- so the platform briefly had
`security.acls.default.{e,in}gress.action=drop` set with NO ACL object attached and NO
enforcement in effect; that half-applied state was reverted with `infra/08_acls.sh remove
--run` before anything was retried, confirmed back to the pre-apply "BEFORE" state above,
30/30 connected throughout.)

## AFTER (current state, re-run for this evidence capture)

```
station -> CSMS NodePort 10.255.255.80:30900   : blocked (expected): timed out
station -> registry NodePort 10.255.255.80:30801: blocked (expected): timed out
station -> its own edge, 443 (edge.porto.pt.lab): ok: reachable (expected)
station -> Wazuh manager, 1514                  : ok: reachable (expected)
```
`infra/08_acls.sh check` (all three cities): `ok  <city>: 14 rules equal the allow list
(default action drop, both directions)` for aveiro, lisboa and porto.

This matches `DESIGN-hijacking.md` mitigation 2's claim directly: "before, a rogue station
can open a socket straight to the CSMS NodePort ... after, the packet never leaves the city
network" -- confirmed, the NodePort route that scenario 1/2's rogue containers used is now
closed, while the legitimate station-to-edge and station-to-Wazuh paths the platform needs
are untouched.

## Known gap -- NOT enforced: station-to-peer-station traffic on the SAME city bridge

```
station -> a peer station, port 443  : ConnectionRefusedError, 0.0s (NOT a timeout)
station -> a peer station, port 12345: ConnectionRefusedError, 0.0s (NOT a timeout)
```
A `ConnectionRefusedError` at 0.0s means the TCP SYN reached the peer instance and was
actively rejected there (no listener) -- NOT dropped by the ACL (which would show as a
silent timeout, like the NodePort tests above). `infra/08_acls.sh check`'s own
"no rule may allow a station to reach ... a peer" assertion only checks that the generated
rule LIST contains no explicit allow for that path; it does not (and structurally cannot,
from the host) verify that the default-drop is actually being enforced on the wire for this
specific kind of traffic.

Root cause, confirmed read-only on the host: `lsmod | grep br_netfilter` found nothing, and
`/proc/sys/net/bridge/` does not exist -- the `br_netfilter` kernel module is not loaded.
Without it, traffic between two ports on the SAME Incus bridge (city-porto, city-aveiro,
city-lisboa are each one bridge, and every station+edge in a city sits on the same one) is
switched at layer 2 and never traverses the host's netfilter/nftables hooks at all, so the
ACL (which is implemented via nftables) cannot see or filter it. Traffic that has to be
ROUTED off the bridge -- to the K3s NodePort (10.255.255.80, a different subnet) -- does
traverse netfilter normally and is correctly filtered, which is exactly what both NodePort
tests above confirm.

**Net effect**: the mitigation fully delivers its stated, primary purpose (closing the
direct-NodePort shortcut a rogue container used in scenarios 1/2) and does not break any
legitimate path. It does NOT additionally stop one station instance from directly reaching
another station instance (or the edge on an unlisted port) within the same city -- that
would require loading `br_netfilter` and enabling
`net.bridge.bridge-nf-call-iptables`/`ip6tables` host-wide, which affects every bridge on
this host (including K3s's own CNI bridges) and was not attempted here: a host-kernel-module
change is outside the scope of this PASS, carries its own risk to K3s networking, and is a
decision for a human, not something to toggle inside a lab-config pass. Flagging this
clearly rather than silently treating "no peer-station rule in the YAML" as proof it's
blocked on the wire.
