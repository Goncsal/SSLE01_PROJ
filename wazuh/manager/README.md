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
| `ruleset-lines.xml` | two `<list>` lines and one `<decoder_exclude>` line inside the existing `<ruleset>`, each tagged `<!-- evcharge -->` |
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

Frequency rules count per agent unless `<global_frequency>yes</global_frequency>` is set. The CSMS
pods and the stations are separate agents, so 110132, 110304, 110308 and 110401 carry it and have
`_agent` cases in the logtest files. `deploy_manager.sh` also sets `analysisd.rule_matching_threads=1`
(`local_internal_options.conf`) so cross-agent counters live in one thread.

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

## Rules (36) and responses

| Block | IDs |
|---|---|
| base | 110000, 110010-110015 (level 0 whitelists: 110010, 110012, 110013, 110014) |
| hijack | 110100, 110101 (any identity refusal, reason = event name), 110104, 110120, 110121, 110132 |
| fraud | 110200, 110204, 110207, 110208, 110209, 110210 |
| botnet | 110300, 110301, 110303, 110304, 110306, 110307, 110308 |
| infra | 110400, 110401, 110405, 110406, 110410, 110411, 110412 and catch-alls 110090-110092 |

| Response | Triggered by | Acts on |
|---|---|---|
| `evcharge-edge-drop` (all agents, 600 s) | 110132: 3 x `ident.ip_mismatch` from one `srcip` in 60 s | `iptables` DROP of `srcip` on the edge of that city |
| `block-card` (manager) | 110210: `fraud.alert` with `auto_action=block_card`, `dry_run=false` | billing card block |
| `quarantine-charger` (manager) | 110303: `host.new_outbound` outside the allow list; 110304: the third station of the botnet pattern (matches 110304 instead of 110303); `hier_id` is the **station** | registry quarantine of the station, CSMS stop and disconnect for each of its chargers |
| `firewall-drop` (edge, 600 s) | 110121, 110307 | source address of the edge log line |

The fraud service quarantines on its own (`power_gap`); that audit is visible as 110208 (alert),
110405 (registry quarantine) and 110209 (billing), so there is no second Wazuh response for it.
110300 is level 7 so the correlator receives it. The correlator gets every alert of level 7 or more
in group `evcharge`, plus 110405, 110410 and 110209 (response audit, levels 4-5).

Outbound whitelist for 110303: `netwatch.py` already drops destinations in `/etc/evcharge/netwatch-allow.txt`
on the station; 110012 ignores package managers (apt, apt-get, http, https, gpgv). The manager alert
history held 110303 only from botnet runs (process `python`, destinations 10.255.255.x), so nothing
else is whitelisted. Add a station's honest destination to its allow file, not here.

## Roll back

`incus exec wazuh-container -- cp -a /var/ossec/etc/evcharge/backup/<time>/<file> <path>`
for single files, or delete the tagged lines and the marked block from `ossec.conf`,
remove `/var/ossec/etc/{rules,decoders}/evcharge_*` and restart `wazuh-manager`.
