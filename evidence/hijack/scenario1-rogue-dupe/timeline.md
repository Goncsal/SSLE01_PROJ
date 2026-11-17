# Scenario 1 — rogue duplicate id (Porto) — timeline

Capture window: `2026-11-07T12:12:37Z` -> `2026-11-07T12:15:03Z` (operator's `date -u` fences
around `bash poc/hijack/rogue_dupe.sh --run`). Charger: `pt.porto.st-001.cp-1`. Real station
srcip `10.31.0.16`; rogue srcip `10.31.0.32` (stolen cert, same CN/serial as the real station's
certificate — the point of the scenario).

| t (UTC)           | source   | event                                                                           |
|--------------------|----------|----------------------------------------------------------------------------------|
| 12:13:00.361       | csms     | **t_first_signal** — `ident.ip_mismatch`: cp-1 connects from 10.31.0.32, registry expects 10.31.0.16 |
| 12:13:00.406       | csms     | `charger.duplicate_id`, HTTP 409: the rogue's handshake refused, real connection (10.31.0.16) stays up |
| 12:13:02 (access)  | edge     | 409 GET /ocpp/pt.porto.st-001.cp-1 (rogue srcip 10.31.0.32) |
| 12:13:03.868       | wazuh    | rule 110104 fires (level 7) on the `ident.ip_mismatch` line |
| 12:13:03.908       | wazuh    | **t_located** — composite rule 110131 fires (level 12, frequency 2, `if_matched_sid` 110100 within the correlation window): "Hijack confirmed... a second host holds the identity" (MITRE T1036/T0849) |
| 12:13:01.919-02.016 | csms (ar) | wazuh-sourced active response, incident `1794053583.675175`: `remote-stop` -> Rejected (no active transaction), `change-availability` -> Accepted, `disconnect` -> Accepted |
| 12:13:02.012       | registry | **t_mitigated** — `registry.quarantine` on pt.porto.st-001.cp-1, `source=wazuh`, `incident_id=1794053583.675175` |
| 12:13:02.079-02.117 | csms (real station) | the real station's own connection (10.31.0.16) is kicked by the `disconnect` command: `charger.status` Unavailable -> `conn.kicked` -> `conn.close` |
| 12:13:04 (access)  | edge     | 101 GET (the real station reconnecting, 10.31.0.16) |
| 12:13:05 (access)  | edge     | 403 (10.31.0.16) — refused: the charger is now quarantined, so even the real station is locked out until released |
| 12:13:03.157       | csms     | `ident.quarantined`, 403, srcip 10.31.0.16 |
| 12:14:00.499       | csms     | `ident.quarantined`, 403, srcip 10.31.0.32 (rogue keeps retrying) |
| 12:14:02/05 (access)| edge     | further 403s, both srcips, while quarantined |
| 12:14:03.198       | csms     | `ident.quarantined`, 403, srcip 10.31.0.16 |
| 12:15:03.134       | csms     | `ident.quarantined`, 403, srcip 10.31.0.16 — end of the capture window |
| 12:18:44.031       | registry | `registry.release` on pt.porto.st-001.cp-1, `source=operator`, reason "W6 scenario-1 validation restore" (operator's manual release after the window, restoring 30/30 connected / 0 quarantined) |

## Notes

- `charger.duplicate_id`'s own `other_srcip` (10.31.0.16) and `ident.ip_mismatch`'s `expected`
  (10.31.0.16) both correctly identify the real station's address; `srcip` on both lines
  (10.31.0.32) is the rogue's.
- The edge's `cert_dn`/`cert_serial` for BOTH srcips are identical
  (`CN=pt.porto.st-001.cp-1,OU=porto,O=EVCharge Lab`, serial
  `38F55AD199936C32CCC02A35B10B30BE76F55DE9`) — direct confirmation that the rogue presented a
  byte-for-byte copy of the real station's certificate (the "stolen cert" step of
  `rogue_dupe.sh`), not a forged or mismatched one; this is exactly why checks 6-8
  (cert verify / CN / serial) pass and only checks 9-10 (ip_mismatch / duplicate_id) catch it.
- Because the quarantine (`effective_state` != active, check 4) applies to the whole charger
  id regardless of srcip, the real station is also refused (`ident.quarantined`) until the
  operator releases it — visible above as two 403 lines for 10.31.0.16 after the response
  fires. This is expected/by-design (quarantine-charger per docs/interfaces.md v6, not a bug),
  but it does mean the real station stays offline until the manual release.
