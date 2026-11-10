# Botnet threat — PoC evidence

A fleet of compromised charge points beacons to a command-and-control (C2) host, then on
command floods the CSMS with OCPP messages and surges the grid by starting full-power
sessions at once. This folder holds the evidence captured during one end-to-end live run.

## Scenario as run

- 6 stations compromised across all three cities (`pt.{porto,lisboa,aveiro}.st-001` and
  `.st-002`), each beaconing every 15 s to the lab C2 `c2.incus:8099`.
- C2 schedule: idle 30 s → flood 60 s (20 Hz) → surge 120 s → stop 30 s.
- Response: bulk quarantine of the 6 stations (registry quarantine → CSMS Inoperative +
  disconnect), the most disruptive response in the project.
- **ACL concession:** the C2 sits on the central bridge, which a station is not normally
  allowed to reach. The city egress ACLs were removed for the run so the beacon could land;
  with the ACLs applied (their normal state) the same beacon is dropped at egress instead —
  a weaker but earlier signal. The ACLs were re-applied at the end.

## What fired (`alerts/wazuh-alerts.jsonl`)

| Rule | Count | Meaning |
|------|-------|---------|
| 110303 | 6 | per-station `host.new_outbound` to the C2 (one per compromised station) |
| 110305 | 6 | established-connection diff on each station (`ss`) |
| 110300 | 6 | flood: charge point over its OCPP message rate |
| 110301 | 2 | flood: connection kicked by the CSMS token bucket |
| 110405 | 6 | response audit: `registry.quarantine` on each station |

Prometheus (`prom/flood-surge-alerts.json`, `prom/city_grid_power_watts_sum-range.json`):
- `MsgRateAnomaly` and `ChargerMsgFlood` fired (aveiro, lisboa) during the flood.
- Surge clearly visible in `city:grid_power_watts:sum`: peaks **lisboa 207 kW / aveiro
  166 kW / porto 156 kW**, 5–8× the pre-attack baseline.

## Collateral (`collateral.txt`)

Bulk quarantine forced **21 CSMS session-stops** in the window (85 → 106) and drove porto's
charging power to 0 W (its live sessions terminated). The blast radius is every driver
mid-session on a quarantined station — the honest cost of a station-wide response to a
per-charger compromise.

## Findings (carry into the report §5.3 post-mortem)

1. **110304 (cross-station fleet correlation) did NOT fire** despite all six 110303 beacons
   to the same C2. This is the "locate the fleet in one alert" signal, and its absence is
   the main detection gap of the run. Suspected cause: the manager's rule-matching state is
   partitioned per agent/thread, so a frequency rule that must correlate different stations
   (different agents) can miss them; a single-thread setting was applied but is not yet
   proven against concurrent live agents. To fix and re-demo before the final run.
2. **CityLoadSpike (110308) did not reach firing state** — the quarantine cut the surge
   short before the alert's sustained-dwell window elapsed. The response beat the alert;
   either shorten the dwell or accept that the faster per-charger flood signals carry it.
3. **Collateral instrument gap** — the sim's `interrupted` counter does not count
   CSMS-forced stops (it logs them as normal stops), so the central CSMS stopped-session
   delta and the charging-power collapse are the valid collateral signals, not the sim
   counter.

## Files

- `alerts/wazuh-alerts.jsonl` — all botnet + response Wazuh alerts from the run window.
- `prom/city_grid_power_watts_sum-range.json` — the per-city grid-power surge (range query).
- `prom/flood-surge-alerts.json` — Prometheus flood/surge alert state at capture.
- `prom/collateral-before.txt`, `collateral.txt` — before/after snapshots and the analysis.
