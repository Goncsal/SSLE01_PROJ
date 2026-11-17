# Active responses

Principle (contract 15.5): isolate, block and keep evidence. Nothing is deleted, nothing
irreversible happens without a person. Three custom scripts plus the stock `firewall-drop` (110121, 110307).
Each script is a single file, standard library only, and answers the Wazuh 4.x protocol
(one JSON message on stdin, `check_keys` handshake on `add`, always exit 0).

| Script | Runs on | Rules | Does |
|---|---|---|---|
| `quarantine-charger` | manager, `/var/ossec/framework/python/bin/python3` | 110303 | evidence, registry quarantine, CSMS stop/Inoperative/disconnect, revocation request; target is a charger or a station |
| `block-card` | manager | 110210 | evidence, billing card block, session flag |
| `evcharge-edge-drop` | agents (`location all`), `python3` | 110132 | `iptables` DROP of `srcip` at the city edge, removed after 600 s |

Install (done by the manager deploy): copy to `/var/ossec/active-response/bin/`, mode 0750,
`root:wazuh`; the edge script goes to the agents' `active-response/bin/` too.

## What each one does, and does not do

`quarantine-charger` (alert `data.hier_id`, a charger id `pt.c.st-001.cp-1` or a station id `pt.c.st-001`):
1. evidence into `evcharge-ar.json` as `ar.evidence` lines: CSMS `GET /connections/{id}` (station:
   `GET /connections?prefix={id}`) and registry `GET /nodes/{id}`, before anything changes;
2. registry `POST /quarantine/{id}` (`source=wazuh`, `incident_id` = alert id). For a station this
   is one call: the registry gives every charger below it the effective state `quarantined`;
3. station only: registry `GET /locate/{station}?kind=charger` lists its chargers (fallback CSMS
   `GET /connections?prefix={station}`);
4. per charger: CSMS `remote-stop` (409 and 404 are fine), `change-availability` `Inoperative`, `disconnect`;
5. one line per charger in `/var/ossec/logs/evcharge-revocation-requests.json`.
Steps are independent: if the registry is down the chargers are still disconnected, and the
`ar.action` line is `error` with the failed steps (rule 110411). It does not revoke the
certificate, release the quarantine, or delete anything. The responder tokens cannot.

`block-card` (alert `data.card`, regex `^[0-9A-F]{8,20}$`): evidence `GET /sessions?card=`,
`POST /cards/{card}/block`, `POST /sessions/{id}/flag` if the alert has `transaction_id`.
No unblock.

`evcharge-edge-drop`: silent no-op unless `/etc/evcharge/role` is `edge` and `srcip` lies in
`/etc/evcharge/city-net` (CIDR, one per line, written at agent install). On an edge it refuses
non-private addresses, the edge's own addresses and default gateway, and anything in the
optional `/etc/evcharge/ar-whitelist`. Rules go into `INPUT` and `FORWARD`, checked with `-C`
first so a repeat does not stack; `delete` removes them. Log: `/var/log/evcharge/ar-edge.json`.

## Guards

| Guard | quarantine-charger | block-card |
|---|---|---|
| id format | charger or station hier-id only (`refused invalid_id`; the raw value is sanitised, never copied into `hier_id`) | card regex (`invalid_card`) |
| allow-list | `/var/ossec/etc/evcharge/quarantine-allowlist` (`allowlisted`) | `/var/ossec/etc/evcharge/card-allowlist`, text export of CDB `evcharge-card-allowlist` |
| rate | 5 distinct targets (a station counts once) per 10 min | 10 distinct cards per hour |
| HTTP | 5 s per call, 30 s total, one fallback address | same |

The rate state is a JSON file under `/var/ossec/var/run/` guarded by `flock`. A repeat of an
id already counted in the window is allowed (the calls are idempotent) and takes no new
budget, so a persistent alert on one charger cannot starve a real second incident, while a
flood of forged lines naming many chargers is stopped at the limit (`refused`, rule 110411).
If the state file cannot be used the response fails closed (`error`).

Configuration: `/var/ossec/etc/evcharge/ar.env` (`REGISTRY_URL`, `CSMS_API_URL`,
`BILLING_URL`, and the three `*_RESPONDER_TOKEN`; optional `<URL var>_FALLBACK`, otherwise
`k3s-master.incus` is replaced by `k3s-node1.incus`). Only isolate-only responder tokens,
never an admin token. Tokens are never logged or printed.

## Audit

`ar.action` lines (contract 6): `ar_command`, `target`, `hier_id`/`card`/`srcip`, `rule_id`,
`alert_id`, `result` (`ok`/`refused`/`error`), `reason`. The evidence lines use the extra
event `ar.evidence` (`kind`, `http_status`, `detail` = compact JSON, 1800 chars max) so they
do not trigger rules 110410/110411.

## Undo

- Quarantine: registry `POST /quarantine/{id}/release` (the same id that was quarantined: the station
  id releases the whole station) with the admin or city token, then CSMS `change-availability`
  `Operative` per charger. After a revocation: `pki/issue.sh <id> --force`, registry
  `PATCH cert_serial`, `pki/export.sh`, restart the station (contract 12.5).
- Card: billing `POST /cards/{card}/unblock` (admin token). Flagged sessions stay flagged for
  the review.
- Firewall: `iptables -D INPUT -s <ip> -j DROP` and the same for `FORWARD` (or wait 600 s).

## Revocation queue

The manager has no access to the CA on purpose. On the host:

    pki/process_revocations.sh --list     # pending requests
    pki/process_revocations.sh            # shows them, asks, runs pki/revoke.sh <id> each
    pki/process_revocations.sh --yes      # no question

Requests are read with `incus exec wazuh-container`; processed request ids
(`<alert_id>|<hier_id>`) go to `lab-ca:/root/ca/processed-requests`, so a rerun does nothing.
A forged log line can at most isolate a charger, never destroy its credentials. Check the
timestamps before confirming: `revoke.sh` revokes the charger's *current* certificate.

## Collateral damage (for the report)

- A beaconing station is quarantined as a whole: its drivers lose service until an operator
  reviews and releases. Chosen on purpose: the host is compromised, not one charger, and the
  cost is one station, not the fleet (rate guard).
- A blocked card hits its real owner; the cloned-card case cannot tell the two apart.
- The edge drop is wider than one device if several share an address (NAT); it expires after
  600 s and never touches gateways, edges, nodes or the manager.
- A false positive in rules or a flood of forged lines is bounded by the rate guards and the
  allow-lists; the worst case is five isolated targets per ten minutes, all reversible.

## Tests

    python3 -m pytest -q wazuh/active-response/tests

Fake HTTP servers stand in for registry, CSMS and billing; `iptables` and `ip` are mocked.
Manual: `<script> --self-test [FILE]` parses a sample alert and prints the plan (no config
needed); `<script> --dry-run [FILE]` applies the real guards and prints the URLs, sending and
writing nothing.
