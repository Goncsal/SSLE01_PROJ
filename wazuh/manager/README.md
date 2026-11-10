# Wazuh manager configuration

Everything the SIEM manager (`wazuh-container`, Wazuh 4.14.9) needs for the EV charging
network: decoder, rules, CDB lists, active-response commands, the correlator hook and
the credentials files. The course-guide rules in `local_rules.xml` are not touched.

## What is installed where

| Repo | Manager |
|---|---|
| `wazuh/decoders/evcharge_decoders.xml` | `/var/ossec/etc/decoders/` |
| `wazuh/rules/evcharge_{base,hijack,fraud,botnet,infra}.xml` | `/var/ossec/etc/rules/` |
| `wazuh/lists/evcharge-*` | `/var/ossec/etc/lists/` (compiled at restart) |
| `wazuh/active-response/{quarantine-charger,block-card,evcharge-edge-drop}` | `/var/ossec/active-response/bin/` |
| `wazuh/integrations/custom-correlator` | `/var/ossec/integrations/` |
| `ossec-evcharge.xml` | block between `<!-- evcharge begin -->` and `<!-- evcharge end -->` at the end of `ossec.conf` |
| `ruleset-lines.xml` | three `<list>` lines and one `<decoder_exclude>` line inside the existing `<ruleset>`, each tagged `<!-- evcharge -->` |
| generated | `etc/authd.pass` (0640 root:wazuh), `etc/evcharge/ar.env` (0640 root:wazuh), `etc/evcharge/card-allowlist`, `etc/evcharge/quarantine-allowlist` (created empty, never overwritten) |

Notes on the two parts outside the block:
- A second `<ruleset>` element replaces the lists of the first one (the guide's
  reputation list would stop loading), so lists go into the existing `<ruleset>`.
- The stock `0170-nginx_decoders.xml` is excluded because its `nginx-errorlog-ip` child
  matches the limit lines first; `evcharge_decoders.xml` carries the parent and the IP
  decoder unchanged and puts `evcharge-nginx-limit` before them. `decoded_as` reports
  the parent name, so rule 110306 is a child of the stock parent 31300 and tests the
  fields (`limit_kind`, `zone`, `url`) that only the evcharge child sets.
- `use_password` of the existing `<auth>` is switched to `yes` (tagged `evcharge-auth`).

Wazuh tries sibling rules from the highest level down (level 0 first), not in file order.
The catch-alls 110090-110092 therefore exclude the events of the level 2 rules.

## Deploy

```
wazuh/manager/deploy_manager.sh
```
Needs only `incus`. Appends `WAZUH_ENROLL_PASSWORD` to `secrets/platform.env` if missing,
pushes changed files only, rebuilds the white list from `incus list`, writes `ar.env`
from `secrets/platform.env` (missing tokens are warned about and left out), backs up
every replaced file to `/var/ossec/etc/evcharge/backup/<time>/`, runs
`wazuh-analysisd -t` and restarts `wazuh-manager` only if it passes; otherwise the files
touched in that run are restored. A second run changes nothing and does not restart.

Rule 110304 (fan-in of 3 stations to one destination) needs `<global_frequency>yes</global_frequency>`:
frequency rules count per agent by default and each station is its own agent, so it never fired live.
`logtest_driver.py` replays lines marked `"_agent":"<name>"` as forwarded by that agent to cover this.

The same applies to the CSMS-fed correlations 110302, 110308, 110112, 110130, 110131, 110201, 110203,
110206 and 110401 (pods on two nodes = two agents); they carry `global_frequency` and have `_agent` cases.

## Test

```
wazuh/tests/run_logtest.sh [block ...]
```
Replays `wazuh/tests/<block>.jsonl` through the manager's `wazuh-logtest` and compares
with `<block>.expect` (`line  rule-id|none|reset`). `{"_session":"reset"}` lines start a
new logtest session; frequency and composite rules are sequences inside one session.
Every rule has a positive and a negative case. Exit status 1 on any mismatch.
The list `evcharge-ip-exceptions` holds one spare id (`pt.spare.st-999.cp-1`) so the
whitelist 110013 can be shown silent.

## Rules (61)

| Block | IDs | Count |
|---|---|---|
| base | 110000, 110010-110015, 110090-110092 (catch-alls are in `evcharge_infra.xml`, counted there) | 7 + 3 |
| hijack | 110100-110116, 110120-110123, 110130, 110131 | 23 |
| fraud | 110200, 110201, 110203-110209 | 9 |
| botnet | 110300-110308 | 9 |
| infra | 110400-110406, 110410-110412 and the catch-alls | 13 |

Differences from the contract table: 110406 (`command.result` with `source` wazuh) is
split from 110405 (registry quarantine/release); 110412 keeps `ar.evidence` lines quiet;
110305 also alerts on the first output of a new station.

## Tuning after the live run (2026-11-06)

- Wazuh counts a frequency rule **per agent**. The two CSMS pods are two agents, so 110110 now
  uses 5 within 60 s (a scan of 10 or more ids spread over both pods reaches it).
- 110203 and 110206 use 120 s windows. The simulated drivers are city-local (a card is only
  used in its home city; see sim/README), so one card in two cities within two minutes is a
  clone and these rules no longer fire on honest traffic.
- **Fraud-service alerts and the block-card response (2026-11-09).** A live run showed 110207
  (any `impossible_travel` alert, 200 km/h) blocking honest roaming cards, and the response
  ran for real while the fraud service was in dry run (dry run only gates the service's own
  API calls). 110207 is now alert-only. The response is attached to 110210, a child that also
  requires the log line's `auto_action` to be `block_card` (overlapping sessions or speed at
  `FRAUD_BLOCK_KMH`) and `dry_run` to be `false`; the fraud service writes both fields on every
  `fraud.alert`. A line without them never blocks. 110203 and 110206 key on the CSMS logs, which carry no
  dry-run flag, so they are alert-only too and 110210 is the single block path.
- The `station-sockets` command is `netwatch.py --sockets`: only destinations outside the
  allow file, so queue sizes, pids and ephemeral ports no longer change the output (the raw
  `ss` output made 110305 fire about 250 times per hour).
- **110304 cross-agent fix (2026-11-10).** `analysisd.rule_matching_threads` defaults to one
  thread per core; events are assigned to a thread by agent, and frequency/composite rule
  state (the `if_matched_sid` counters) lives per thread, not globally. 110304 correlates
  three *different* stations (three different agents) within one window, so it could silently
  miss agents hashed to a different thread even though logtest (one pseudo-agent, no thread
  split) always showed it firing. `deploy_manager.sh` now installs
  `analysisd.rule_matching_threads=1` in `local_internal_options.conf` (single-threaded rule
  matching; the lab's ~21 agents need none of the parallelism). `wazuh/tests/botnet.expect`
  gained a cross-city positive case (porto+lisboa+aveiro stations, one dstip) alongside the
  existing same-city one. Logtest cannot exercise the real multi-agent/multi-thread path
  (it has no agent concept), so this remains unverified against genuinely concurrent agents;
  watch 110304 the first time it must fire live in W8.
- **Correlator outbox (2026-11-10).** `evcharge-correlator-outbox.json` grows unbounded
  (capped only at 10 MB) while no correlator is deployed to receive the forwarded alerts.
  Backed up to a timestamped `.bak-<UTC>` copy next to it and truncated to empty before the
  W9 replay; `custom-correlator` always opens it in append mode per call, so truncating it
  out of band is safe and does not race a concurrent writer.

## Roll back

`incus exec wazuh-container -- cp -a /var/ossec/etc/evcharge/backup/<time>/<file> <path>`
for single files, or delete the tagged lines and the marked block from `ossec.conf`,
remove `/var/ossec/etc/{rules,decoders}/evcharge_*` and restart `wazuh-manager`.
