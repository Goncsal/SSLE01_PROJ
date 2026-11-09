# Queued certificate revocations -- processed, re-issued, redeployed

`pki/process_revocations.sh --list` found 3 pending requests, all queued by
`quarantine-charger` active responses during this session's scenario runs (not just
scenario 1 -- two of the three are leftover from an EARLIER bug in `mitm_aveiro.sh`, since
fixed, where the station's normally-running `evsim` systemd service auto-started on
reboot and collided with the script's own direct `evsim`, producing a duplicate-id +
composite hijack on `pt.aveiro.st-001.cp-1`/`.cp-2` before that bug was fixed):

| hier_id | rule | reason |
|---|---|---|
| `pt.porto.st-001.cp-1` | 110131 | scenario 1 (rogue_dupe), IP mismatch then duplicate connection |
| `pt.aveiro.st-001.cp-1` | 110131 | pre-fix `mitm_aveiro.sh` run, same composite pattern |
| `pt.aveiro.st-001.cp-2` | 110130 | pre-fix `mitm_aveiro.sh` run, duplicate then IP mismatch |

All three are the REAL station's own certificate (the "stolen" cert in scenario 1 was a
byte-identical copy of the real `pt.porto.st-001.cp-1` cert; the aveiro pair is just the
real station's own certs flagged by the pre-fix bug) -- revoking without re-issuing would
have permanently locked these three real chargers out.

## Processed

`bash pki/process_revocations.sh --yes`: all three revoked, CRL regenerated and pushed to
all three edges each time (output captured below). 30/30 remained connected immediately
after revocation (long-lived WebSocket connections aren't re-checked against the CRL
until their next handshake, so no immediate drop -- but the next reconnect of any of these
three would have been refused without the next step).

```
ok   CRL regenerated
aveiro: crl pushed, nginx reloaded
lisboa: crl pushed, nginx reloaded
porto: crl pushed, nginx reloaded
ok   pt.porto.st-001.cp-1 revoked (serial 38F55AD199936C32CCC02A35B10B30BE76F55DE9)
... (same CRL push, x3) ...
ok   pt.aveiro.st-001.cp-1 revoked (serial 56B01D4B0C1827D0683446CC2CA9D917E5E094DB)
... (same CRL push, x3) ...
ok   pt.aveiro.st-001.cp-2 revoked (serial 78216F9328CDDAC5BBC6975BBE1100881849222D)
```

## Re-issued and redeployed

`pki/issue.sh <hier_id> --force` for each (new serials), `pki/export.sh <hier_id>
<instance> --group 10001` (the `evcharge` group's GID, matching the existing file
ownership on every station so `evsim.service`'s `User=evcharge` can read the key) to
deploy into `/etc/evcharge/pki` on the real station, then `systemctl restart evsim`:

| hier_id | new serial |
|---|---|
| `pt.porto.st-001.cp-1` | `71B4F1559427757C255814F435B87994A2CAA9A5` |
| `pt.aveiro.st-001.cp-1` | `1CC609688946233097992C693ABE8F3535C47440` |
| `pt.aveiro.st-001.cp-2` | `1E26BD1AE9FE87EE624B6E0A614848A8B8E3B4FB` |

## A real snag, caught by the health gate, and how it was resolved

The first restart attempt dropped all three chargers to a 403 refusal (confirmed in each
station's own `evsim` log: `sim.rejected ... http_status:403`), and the platform's total
connection count fell from 30 to 27-28. Root cause, confirmed directly: the registry's own
node record for each charger still carried the OLD (now-revoked) certificate's serial in
its `cert_serial` field (`GET /nodes/pt.porto.st-001.cp-1` showed the pre-reissue serial)
-- so identity check 8 (`csms/src/evcsms/identity.py`, "the certificate the registry has
on record") refused the NEW cert as a mismatch. This is expected/correct CSMS behaviour,
not a bug: re-issuing a cert changes its serial, and the registry's recorded serial has to
be updated too, by design, as a deliberate extra check (a stolen-but-not-yet-revoked old
cert wouldn't help an attacker if the registry is also updated to a new serial, since the
registry update is a separate operator action from the cert file itself).

Fixed with `PATCH /nodes/<hier_id>` (`{"cert_serial": "<new serial>"}`, `REGISTRY_ADMIN_TOKEN`)
for each of the three chargers. The CSMS also caches registry lookups for
`CSMS_REGISTRY_CACHE_S` (30s default) per replica, so the very next retry (immediately
after the PATCH) still hit a stale cached entry for two of the three; waiting out that
30s window and restarting `evsim` once more resolved it cleanly. Final state after the
second restart: all three reconnected with their NEW serials, 30/30 total, 0 quarantined,
0 pending revocation requests (`pki/process_revocations.sh --list`).

**Takeaway for the report**: a revoke+reissue of a station's own certificate (the correct
incident response once its identity has been confirmed stolen) is not just "swap the file"
-- the registry's own `cert_serial` record must be updated in the same action, or the
re-issued, legitimate certificate is refused exactly as if it were still the compromised
one. This was caught and fixed here by the health gate (checking 30/30 after every
platform-mutating step) rather than left for the operator to discover later.
