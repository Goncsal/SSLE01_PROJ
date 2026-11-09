# Scenario 3 -- routed MitM on the Aveiro plain-ws link, meter tamper (BEFORE)

Capture window: `2026-11-07T14:57:12Z` -> `2026-11-07T14:59:56Z` (operator's `date -u` fences
around `bash poc/hijack/mitm_aveiro.sh --run`). Charger: `pt.aveiro.st-001.cp-1` (the interceptor
also rewrote cp-2's traffic in the same run -- see the Wazuh evidence below). Interceptor's
isolated-side address `10.240.50.1`; station's isolated-side address `10.240.50.10`; all
traffic the CSMS sees for this window carries srcip `10.33.0.241` (the interceptor's own
address on `city-aveiro`), not the station's real address -- a side effect of the routed-MitM
topology, logged by the CSMS but not itself the target signal here.

## What happened, in order

1. The interceptor's `/opt/mitm/meter_tag.py` addon loaded cleanly (confirmed in
   `mitmdump/mitm-aveiro-mitmdump.log`): `[14:57:45.905] mitm_meter_tag: addon loaded,
   SCALE=4.0`, right after mitmdump itself started listening (`Transparent Proxy listening
   at *:8080`).
2. The forced charging session (`--arrival-rate 3600 --meter-interval 5 --session-minutes
   14`, see `poc/hijack/mitm_aveiro.sh`) started within seconds and ran `MeterValues` every
   ~5s for the whole window.
3. The addon rewrote the `Energy.Active.Import.Register` field of each sampled
   `MeterValues` by 5x (`SCALE=4.0`, i.e. `value * (1 + SCALE)`) -- 46 rewrite confirmations
   logged in `mitm-aveiro-mitmdump.log` (`mitm_meter_tag: rewrote MeterValues register
   x5.0`), spanning `14:57:53.090` to `14:59:47.552`.
4. The CSMS's own `over_energy` check (`csms/src/evcsms/sessions.py`:
   `register_delta > max_kw * Δt_h * 1000 * CSMS_POWER_TOLERANCE(1.10) + 10`) caught the
   inflated deltas immediately: **24** `meter.anomaly` lines for `pt.aveiro.st-001.cp-1` in
   the CSMS JSON log for this window (23 `over_energy`, 1 `stop_mismatch`), e.g.:
   - `14:57:50.887`: `meter_wh=1072225` vs `prev_meter_wh=214416` (a single ~5x jump from a
     true-looking baseline -- the first sample after the rewrite kicked in)
   - `14:57:55.906` onward: subsequent samples continue climbing at the SAME inflated rate
     (`meter_wh` 1072375 -> 1073545 -> ... in ~145-150 Wh/5s steps, ~5x a plausible 22kW
     charger's real per-sample delta)
   - `14:59:45.505`: `stop_mismatch` -- the session's `StopTransaction` reports the TRUE
     final register (`215116`, the real physical reading, since that path isn't rewritten
     by the addon) which is LOWER than the last (rewritten) `MeterValues` reading
     (`1075450`) -- a second, independent anomaly signal from the same tamper.
5. **Wazuh rule 110116** (`wazuh/rules/evcharge_hijack.xml`, level 10, MITRE
   T1557/T1565.002, "Hijack: meter anomaly ... possible man in the middle") fired **48
   times** in the window -- across BOTH `pt.aveiro.st-001.cp-1` and `.cp-2` (both chargers
   got a forced session and both got rewritten), confirmed in
   `wazuh/alerts-110116.jsonl`.
6. Diagnostics captured live, before cleanup deleted the interceptor
   (`mitmdump/diagnostics-iptables-ss.txt`): the `iptables -t nat PREROUTING` REDIRECT rule
   on `eth1` shows non-zero packet/byte counters (traffic genuinely hit the redirect, not a
   routing dead end), and `ss -tlnp` confirms `mitmdump` was listening on `:8080`.

## What this demonstrates

A routed MitM on the plain-`ws://` Aveiro link can rewrite in-flight `MeterValues` without
the station or CSMS ever seeing a certificate error (there is none to see -- no TLS at all).
The CSMS cannot detect the MitM directly; it only detects the PHYSICAL CONSEQENCE of the
tamper (an energy delta no real charger at this rating could produce), exactly as
`DESIGN-hijacking.md` frames it: "inference from physics," not proof of an interception.
BATCH 2's `mitm_aveiro.sh --after` (once Aveiro is switched to mTLS, mitigation 3 below) is
expected to show the interceptor seeing only opaque TLS bytes and producing zero rewrites and
zero `meter.anomaly` for the same forced-session traffic.
