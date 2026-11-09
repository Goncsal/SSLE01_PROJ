# Scenario 2 -- stolen certificate from another host (Porto), check 9 BEFORE

Capture window: `2026-11-07T12:34:06Z` -> `2026-11-07T12:36:16Z` (operator's `date -u` fences
around `bash poc/hijack/stolen_cert.sh --run --phase before`). Charger: `pt.porto.st-001.cp-1`.
Real station srcip `10.31.0.16`; rogue srcip `10.31.0.5` (stolen cert, same CN/serial as the
real station's certificate -- an exact copy, per `pki/export.sh`).

This run uses the FIXED script (poc/hijack/stolen_cert.sh), which stops the real station's
own evsim before connecting the rogue, specifically so there is no live connection for the
rogue's claim to collide with -- this isolates check 9 (`ident.ip_mismatch`) from check 10
(`charger.duplicate_id`, rogue_dupe.sh's scenario). Live `CSMS_ENFORCE_STATION_IP` at the time
of this run: **false** (not set in the `csms-edge` ConfigMap; settings.py default).

| t (UTC)      | source | event |
|---|---|---|
| 12:34:07.082 | csms   | `conn.close` for 10.31.0.16 (`reason=normal`) -- the real station's evsim stopped by the script, closing its own connection cleanly |
| 12:34:09 (access) | edge | 101 GET (session-end log line for the PREVIOUS long-lived real-station session, srcip 10.31.0.16, duration_s=903.8 -- nginx logs access entries on connection close, not open) |
| 12:34:13.434 | csms   | **ident.ip_mismatch**: cp-1 connects from 10.31.0.5, registry expects 10.31.0.16 -- logged as a **warning only** |
| 12:34:13.440 | csms   | `conn.open`, srcip 10.31.0.5, tls=true, cert_cn=pt.porto.st-001.cp-1, cert_serial matches the real station's -- **accepted** |
| 12:34:13.442-455 | csms | `charger.boot`, `charger.status` Available x2 -- the rogue's stolen-cert session proceeds normally, exactly like a legitimate connection |
| 12:36:13.442 | csms   | `conn.close` for 10.31.0.5 (`reason=normal`) -- the rogue's evsim finished its time-box |
| 12:36:14.161 | csms   | `conn.open` for 10.31.0.16 -- the real station reconnects (its own evsim restarted by the script's cleanup) |
| 12:36:14.164-179 | csms | `charger.boot`, `charger.status` Available x2 for the real station |
| 12:36:15 (access) | edge | 101 GET (session-end log line for the rogue's session, srcip 10.31.0.5, duration_s=120.0) |

**No `charger.duplicate_id` and no registry quarantine event appear anywhere in this window**
(explicitly checked: both CSMS replicas' logs for `pt.porto.st-001.cp-1` in the window,
grepped for `duplicate_id`/`quarantin`, zero matches) -- confirming check 10 did not fire,
unlike scenario 1, because the real station was not simultaneously connected.

## What this demonstrates

With `CSMS_ENFORCE_STATION_IP=false` (the honest limitation DESIGN-hijacking.md calls out:
mTLS proves possession of a key, not location), a certificate copied to another host in the
same city passes every cert check (6-8: verified, CN matches, serial matches the registry's
record) and is **accepted**, with the address mismatch visible only as a logged warning
(`ident.ip_mismatch`, Wazuh 110104) -- not a refusal. BATCH 2's `stolen_cert.sh --phase after`
(after `CSMS_ENFORCE_STATION_IP` is flipped to `true`, mitigation 1 below) is expected to
instead refuse this same scenario with a 403 at the handshake.
