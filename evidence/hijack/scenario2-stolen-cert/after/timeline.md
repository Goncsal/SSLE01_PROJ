# Scenario 2 -- stolen certificate from another host (Porto), check 9 AFTER

Capture window (host/operator clock): `2026-11-07T19:34:06Z` -> `2026-11-07T19:36:19Z`
(`bash poc/hijack/stolen_cert.sh --run --phase after`). `CSMS_ENFORCE_STATION_IP` was
`true` for this run (PASS A mitigation 1, kept on for the "after" demonstration).

**Clock-skew note, important for reading the raw files in this directory**: the two K3s
VMs (`k3s-master`, `k3s-node1`, where the CSMS pods run) were found to be running
**~2h34m19s behind** the host/operator clock at the time this evidence was captured
(`incus exec k3s-master -- date -u` -> `17:12:27Z` vs. host `19:46:46Z`; confirmed on both
VMs, and confirmed NOT present on any regular Incus container -- `porto-edge`,
`porto-st-001`, `wazuh-container` all matched the host clock exactly). This is why the
`csms-*.jsonl` files in this directory carry `"ts"` values around `17:00-17:02Z` while the
`edge-*.jsonl` and `stolen-cert-after-run.log` files carry the expected `19:34-19:36Z` --
both pairs of timestamps describe the SAME events; only the CSMS pods' embedded clock is
off. This was not present during the earlier BEFORE-state captures (scenario 1/2/3), so it
developed at some point during this session, not from the start. Flagged in
`evidence/hijack/README.md` as an operational finding for a
human to decide on (NTP on the K3s VMs, or periodic resync) -- not something fixed here.

## What happened, in order (times as logged; CSMS files are ~2h34m19s behind the others)

| t (edge/evsim clock) | t (CSMS clock) | source | event |
|---|---|---|---|
| 19:34:10 | -- | edge (access) | 101 GET (session-end line for the real station's PREVIOUS long session, srcip 10.31.0.16, duration_s=15829 -- logged on close, per nginx's own access-log timing) |
| -- | 16:59:52.206-245 | csms | the real station's evsim is stopped by the script (`session.stop reason=Reboot`, `charger.status Finishing/Available`, `conn.close`) |
| 19:34:19 | 17:00:00.575 | edge + csms | **403, `ident.ip_mismatch`**: rogue connects as `pt.porto.st-001.cp-1` from `10.31.0.29` (foreign srcip), registry expects `10.31.0.16` -- refused at the handshake, `CSMS_ENFORCE_STATION_IP=true` |
| 19:35:19 | 17:01:00.831 | edge + csms | second attempt, same **403 `ident.ip_mismatch`** refusal (evsim's `--reject-backoff`, confirmed in `stolen-cert-after-run.log`: two `sim.rejected` lines one minute apart) |
| -- | 17:02:02.077-119 | csms | the real station reconnects (`conn.open`, `charger.boot`, `charger.status` Available x2) -- the script's cleanup restarting its evsim |

**No `conn.open`, no `charger.boot`, and no `charger.status` line for srcip `10.31.0.29`
anywhere in the window** -- the rogue never got past the handshake, confirmed independently
from both the edge access log and both CSMS replicas' logs, and from the rogue's own `evsim`
log (`stolen-cert-after-run.log`: two `sim.rejected ... http_status:403` lines, then a clean
`sim.start stopped` at the end of its time-box, exit code 0).

## Contrast with the BEFORE capture (`../csms/`, `../edge/`, `../timeline.md`)

Same scenario, same stolen certificate, same foreign srcip pattern; the ONLY platform change
between the two runs is `CSMS_ENFORCE_STATION_IP` (`false` -> `true`, PASS A mitigation 1):

| | BEFORE | AFTER |
|---|---|---|
| `ident.ip_mismatch` | logged (warning) | logged (warning), same line |
| handshake result | **accepted** (`conn.open`, `tls:true`, boots, runs its full 120s) | **refused**, 403, no `conn.open` ever |
| edge access log | `101 GET` (upgrade succeeded) | `403 GET` (refused) |

This is the exact before/after story `DESIGN-hijacking.md` mitigation 1 and the runbook's
BATCH 1/2 structure are built around, now captured directly from both runs.
