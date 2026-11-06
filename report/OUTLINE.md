# Project 1 report — outline and figure plan

Working outline for the SSLE Project 1 report (EV charging network). Article/thesis style,
the brief's exact 7-section order. Target length ≈ 8 pages (the 19/20 reference was ~8),
so every section is deliberately tight. This file is the plan; the prose is written from
the two writing cards (`.ai/tasks/W11-sec1-4.md`, `.ai/tasks/W11-sec5-7.md`).

Everything here is grounded in what was actually built: `docs/interfaces.md` v6, the three
`DESIGN-*.md` notes, and the Phase 1–5 evidence. Numbers that already exist are marked
**[have]**; numbers that need the live threat runs (Phases 6–9) or the scalability runs are
marked **[run]**. Figures that the **user** must produce (draw or screenshot) are marked
**[user]**.

Deliverable file: **`report/report.md`** (single Markdown file, article style). Sections 1–4
are written now; sections 5–7 are appended after the runs. The two writers never edit the
file at the same time (5–7 starts only after 1–4 is merged and the runs are done), so one
file is safe. Export to PDF is the user's step.

---

## Section 1 — Introduction  [write now]

**Purpose:** frame the system and the problem, and state what the report will show.

Subsections / points:
- 1.1 The system in one paragraph: a regional EV charging operator running OCPP 1.6J charge
  points across three cities (Porto, Lisboa, Aveiro), a central CSMS on K3s, billing,
  fraud scoring, Prometheus + Wazuh, under one hierarchical naming scheme.
- 1.2 Why it is critical infrastructure and why monitoring matters (grid dispatch, billing
  integrity, availability to drivers). One sentence placing it in the L1 syllabus.
- 1.3 Scope and three threats chosen (signal/identity hijacking, fraud, botnet) — name them,
  note all three are on the syllabus list.
- 1.4 Contributions / what this report demonstrates, phrased as a short list: one consistent
  hierarchical identity end-to-end; cross-signal correlation into one incident with a located
  root cause; a measured bandwidth-optimised federated monitoring design; quantitative
  scalability; honest limitations. (These are the grader's differentiators — foreground them.)
- 1.5 One-sentence roadmap of the remaining sections.

Figures: none (optionally reuse Fig. A here by reference).

---

## Section 2 — Relevant state of the art  [write now]

**Purpose:** show awareness of the tool landscape and the domain, and justify the choices.
The 19/20 reference explicitly compared tools; the grader rewards it (rubric F).

Subsections / points:
- 2.1 Monitoring stacks: **Prometheus vs Zabbix vs Datadog** — pull model + PromQL + federation
  vs Zabbix's agent/server polling vs Datadog's hosted SaaS; why Prometheus fits a
  self-hosted, per-site, bandwidth-sensitive lab (federation, recording rules, no external
  dependency, cost). Justify federation as the mechanism for the bandwidth bonus.
- 2.2 SIEM / detection: **Wazuh vs OSSEC vs Falco vs Splunk** — Wazuh as an OSSEC fork with
  decoders/rules + active response + MITRE mapping; Falco as syscall/eBPF runtime detection
  (host-level, complementary, no log correlation); Splunk as heavy commercial SIEM. Why Wazuh:
  custom decoders/rules on JSON logs, built-in active responses, free, already in the course
  (G4). One line on Snort/Suricata as network IDS we do not use (and why — encrypted OCPP).
- 2.3 EV-charging / OCPP security context: OCPP 1.6J has no built-in security profile (the
  OCPP 2.0.1 Security Profiles and the OCA security whitepaper added mTLS and signed
  messages); ISO 15118 Plug&Charge for contract certificates; known classes of attack on
  public charging (identity spoofing, meter/transaction fraud, firmware/botnet compromise).
  2–4 references. This motivates the mTLS edge, the cert=hier-id binding and the legacy
  plain-`ws` city.
- 2.4 Frameworks used to structure the work: **MITRE ATT&CK (Enterprise) and ATT&CK for ICS**
  for mapping rules; **FCAPS** (fault + accounting) for the operational workflow; the
  attack–defense tree for risk. One or two lines each.

Figures: none (a small comparison table is optional, Table SoA, if space allows — keep it to
3–4 rows).

References to assemble (writer gathers, user verifies): OCPP 1.6/2.0.1 spec and OCA security
whitepaper; ISO 15118; Prometheus and Wazuh docs; MITRE ATT&CK + ATT&CK for ICS; one or two
papers/CVEs on EV-charger security. Keep the list short and real; do not pad.

---

## Section 3 — Service architecture used  [write now]

**Purpose:** describe the system as built, with the naming scheme as the spine. This is where
the hierarchical-naming / physical-vs-logical / delegation answers to the brief live.

Subsections / points:
- 3.1 Overview: the central platform (K3s: `csms` ×2–4 HPA, `registry`, `billing`, `fraud`,
  `correlator`, Postgres StatefulSet+PVC) vs the three city sites (Incus networks, each an
  nginx edge + per-city Prometheus + simulated stations). Reference Fig. A.
- 3.2 **Hierarchical naming** `pt.<city>.<station>.<charger>` and the "one string everywhere"
  table (cert CN/SAN = OCPP path = registry key = Prometheus labels = Wazuh agent name/field
  = DNS SAN = map label). This is rubric G and a top differentiator — give it room.
  Reference Fig. B. Cite `interfaces.md` §1.
- 3.3 **Physical vs logical**: central services are logical K8s pods; cities are "physical"
  sites on their own Incus networks; a charger has no address of its own and is reached
  through its station's IP — the IP locates the station, the hier-id + cert locate the
  charger. (Directly answers the brief's physical-vs-logical naming question.)
- 3.4 **Delegation**: the registry enforces per-city operator tokens (a city token writes only
  below `pt.<city>`; the NOC admin owns `pt` and `pt.central.*`); `/locate` returns the
  element, its subtree and its `operator`. Cite §1.3, §4.1.
- 3.5 Transport and identity: OCPP 1.6J over WebSocket; the edge terminates mTLS
  (`ssl_verify_client optional`, CRL) and forwards cert DN/serial/verify headers; the CSMS
  runs 10 handshake identity checks before the upgrade (cert CN = id, serial match, city
  match, duplicate-id guard). Aveiro is deliberately plain `ws://` (legacy site). Cite §5.2,
  §11, §12.
- 3.6 PKI: single-level lab CA in its own container, EC P-256, CN=hier-id, CRL; keys never
  leave `lab-ca`. Cite §12. One line on secrets management (per-service Postgres roles,
  responder-scoped tokens, secrets gitignored) — the reference project lacked this.
- 3.7 Data and APIs in brief: registry/CSMS/billing/fraud/correlator tables and the FCAPS
  Accounting coverage (billing: tariffs, sessions, cost). Keep to a short paragraph + pointer
  to `interfaces.md`.

Figures:
- **Fig. A — Architecture diagram** [user, redraw]. Source: the ASCII diagram in
  `.ai/HANDOFF.md §3` is the content; the user draws a clean version (central K3s box, Wazuh,
  three city networks with edge+prom+stations, attacker containers, the federation and
  wss+mTLS arrows). Caption names the physical-vs-logical split.
- **Fig. B — Naming scheme** [user, draw] or a typeset table. Source: `interfaces.md §1.2`
  "one string everywhere" table. Show the hier-id tree and the same string appearing in cert,
  OCPP path, registry, Prometheus, Wazuh, DNS.

---

## Section 4 — Risk management and mitigation  [write now]

**Purpose:** attack surfaces, the attack–defense tree, and the large-scale monitoring design
(including the bandwidth bonus and the baseline). Most of this is writable now from the
monitoring evidence.

Subsections / points:
- 4.1 **Attack surfaces**: enumerate — the OCPP WebSocket (identity spoofing, floods), the
  plain-`ws` legacy city (MitM), the RFID card path (cloned cards), the meter/accounting path
  (tamper), the station host (firmware/botnet, C2 egress), the registry delegation (token
  abuse), the city→central network (reaching NodePorts). Map each to a defence already built
  (mTLS + cert=id, 10 handshake checks, rate limits, fraud rules, netwatch, responder tokens,
  Incus ACLs). A compact table reads well here (Table 4.1).
- 4.2 **Attack–defense tree (ADTree)** [user draws in Excalidraw]. The report describes it;
  the figure is Fig. C. Specify its content (see the card and below) so the writer's prose and
  the user's drawing match. Root goal: "Disrupt or defraud the regional charging network."
  Three attacker sub-goals = the three threats; leaves = exploitation steps; green
  countermeasure nodes = the defences. Reference Fig. C.
- 4.3 **Large-scale monitoring design**: two tiers — a city Prometheus on each edge (5 s
  scrape, per-charger series, recording rules `city:*`) and a central Prometheus that
  federates only `{__name__=~"city:.*"}` every 30 s. Per-charger series never cross the
  inter-site link. Alertmanager inhibition (EdgeDown suppresses its city's dependent alerts).
  Cite §14.1–14.4. Reference Fig. D (map) and Fig. E (bandwidth).
- 4.4 **Baseline and thresholds**: how the `baseline:*` recording rules were computed
  (`mean + 3σ` over a measured window, load capped at 90% of installed capacity), and the
  actual window used. **[have]** basis `12h-20261106`, 12 h window 2026-11-05T23:01Z →
  2026-11-06T11:01Z, step 60 s; per-city upper bounds (e.g. porto grid 288.5 kW, msgs/s 6.43;
  lisboa auth10m 2.82). State plainly that the window is 12 h (contract allows ≥6 h; 24 h is
  the target) and that the simulator load is stationary so no hour-of-day bands. Cite §14.7,
  `monitoring/rules/baseline.yml`. The reference project had *ad hoc* thresholds — this is a
  differentiator (rubric C).
- 4.5 **Bandwidth bonus (per-site monitoring)** [have]: federated vs direct scrape, same load,
  30-min windows. Headline: **99.3% less inter-site traffic** (uncompressed 47.2 KB/min vs
  6.38 MB/min total; gzip 4.4 KB/min vs 963 KB/min = 99.5%); ~15.5 KB/city/min federated vs
  ~2.1 MB/city/min direct; 324 vs 55,427 samples/min; cost: central Prometheus RSS +12%, CPU
  5×. Scaling argument: direct grows with chargers, federated with `city:*` series (~54/city,
  independent of charger count). Cite §14.6, `evidence/phase4/bandwidth.txt`,
  `evidence/phase4/README.md`. Reference Fig. E. This is rubric J and a top differentiator.
- 4.6 **Network containment**: Incus network ACLs on the `city-*` networks (egress allow-list,
  stations cannot reach NodePorts/peers/other cities), applied in Phase 6, shown before/after.
  Cite §16, §14.8. (The before/after evidence is [run], but the design is writable now.)

Figures:
- **Fig. C — Attack–defense tree** [user, Excalidraw]. Content fully specified in the card and
  §4.2 here.
- **Fig. D — Grafana Geomap** [user, screenshot]. Source: `evcharge-map` dashboard; the
  "all green" shot + optionally the grey-station/red-ring shot. `evidence/phase4/screenshots/`
  has host-rendered versions with no basemap tiles — the user re-shoots in a browser with the
  basemap. Phase 4 README lists the exact steps.
- **Fig. E — Bandwidth chart** [user, screenshot] or a typeset table (Table 4.5). Source:
  `evcharge-bandwidth` dashboard, or just the table from `bandwidth.txt`. A table is safest if
  the screenshot has no tiles/data; prefer the table for the hard numbers and the screenshot
  as support.
- **Table 4.1 — Attack surfaces → defences** [have]. Typeset from §4.1.
- **Table 4.4 — Baseline thresholds per city** [have]. Typeset from `baseline.yml`.

---

## Section 5 — Approach to the three threats  [write after Phases 6–9 runs]

**Purpose:** one subsection per threat, each following the brief's required six points. This
section depends on the live threat runs producing `evidence/hijack|fraud|botnet/` and the
correlator timeline numbers. Structure is fixed now; numbers/screenshots are [run].

For **each** threat (5.1 hijacking, 5.2 fraud, 5.3 botnet) cover, in this order:
1. **What it is** — from the matching `DESIGN-*.md`.
2. **Why relevant to THIS system** — from the DESIGN note.
3. **How exploited** — the PoC paths (`poc/hijack|fraud|botnet/`), stated as lab-bounded.
4. **Monitoring & mitigation metrics** — the Prometheus alerts and Wazuh rule IDs that fire
   (listed in each DESIGN note and `interfaces.md §14.3, §15.4`), with MITRE IDs.
5. **Action-responses** — the Wazuh active responses (`quarantine-charger`, `block-card`,
   `evcharge-edge-drop`/`firewall-drop`) and why each (isolate + keep evidence; nothing
   irreversible auto-reverts). Cite §15.5.
6. **PoC post-mortem / practical validation** — the detect→locate→mitigate timeline with
   timestamps, the evidence captured, and the honest cost (collateral damage).

Per-threat specifics (so the writer knows the spine before the runs):
- 5.1 Hijacking: duplicate-id (TLS city, stolen cert), stolen-cert-from-another-host (the
  `CSMS_ENFORCE_STATION_IP` false→true before/after), MitM in plain Aveiro (before/after
  mTLS). Composite rules 110130/110131. Honest limit: mTLS proves key possession, not
  location; detection of MitM is inference from physics.
- 5.2 Fraud: cloned card (impossible travel) and meter tamper (`grid_gap` vs feeder meter).
  Two independent detectors (fraud service + Wazuh) joined by the correlator — foreground this
  (rubric H). Honest limit: the real card holder is blocked too; grid_gap trusts the feeder.
- 5.3 Botnet: C2 beacon detection (netwatch `host.new_outbound`, cross-station 110304), flood
  (rate limits, 110302), grid surge (`CityLoadSpike` + 110308). The bulk-quarantine
  collateral-damage table is the honest centrepiece (sessions interrupted, kWh not delivered).

Figures (all [run]/[user]): per threat — the composite alert JSON excerpt, the `ar.action`
lines, a Grafana panel (e.g. `CityLoadSpike` crossing the baseline band for botnet; feeder vs
reported power for fraud), and a Wazuh dashboard screenshot of the firing rule. See the
sec5-7 card for the exact evidence list per threat.

---

## Section 6 — Evaluation of the approaches and scalability  [write after eval runs]

**Purpose:** numbers. This is rubric I and a top differentiator; the reference project had
none. Mostly [run] (scalability harness), with the monitoring numbers already [have].

Subsections / points:
- 6.1 **Detection/response effectiveness**: per threat, did the rule fire, was it correlated
  into one incident, time to locate and time to mitigate (correlator
  `evcharge_correlator_time_to_locate_seconds` / `_time_to_mitigate_seconds`). **[have]** a
  first datapoint: registry `/locate` resolved a station in **0.013 s** (Phase 4); the
  end-to-end correlator timeline is [run].
- 6.2 **Monitoring scalability [have, extend with run]**: the bandwidth result (§4.5) restated
  as an evaluation with the scaling argument; central Prometheus head series, RSS, CPU under
  federated vs direct. Repeat at larger scales if the eval runs reach them.
- 6.3 **System scalability [run]**: the `eval/` runs at increasing charger counts (target
  30 → 300 → 1500 per HANDOFF) measuring CSMS CPU/memory and OCPP handle latency
  (`evcharge_csms_ocpp_handle_seconds` p50/p95), HPA replica behaviour, Prometheus series and
  scrape time, Wazuh events/s. **GAP:** only the 30-charger scale has been run so far; the
  harness and the larger scales are not done (see report-back). State honestly whatever scale
  was actually reached and the host limit (23 GB / 8 cores; Wazuh ~5 GB; ~50 MiB/station) as
  the ceiling.
- 6.4 **Resource budget [have]**: the Phase 4 RAM table (edges ~90 MiB each incl. city
  Prometheus, ~50 MiB/station, monitoring pods, K3s VMs) — shows the design fits the laptop
  and where it would stop. Cite `evidence/phase4/README.md`.
- 6.5 **False positives / tuning**: `ChargerFlapping` on a CSMS rollout restart; `ConcurrentTx`
  excluded from auth-burst; the ip-mismatch/card allow-lists. Honest, specific (rubric K
  overlap).

Figures/Tables: Table 6.3 (scalability: N chargers vs CPU/mem/latency/series) [run];
Table 6.2 / Fig. E reused for monitoring scaling; Table 6.4 (resource budget) [have]; a
latency plot [run] if the eval produces one.

---

## Section 7 — Conclusions  [write mostly now; finalise after runs]

**Purpose:** findings, why the solution is not perfect (this earns marks — brief + rubric K),
future work.

Subsections / points:
- 7.1 Main findings: the hierarchical identity made "locate the source" trivial; correlation
  turned parallel Prometheus/Wazuh signals into single incidents; federation cut inter-site
  traffic by ~99%; baselined thresholds beat ad hoc ones.
- 7.2 **Why it is not perfect** (be specific and own it):
  - mTLS proves key possession, not location; `CSMS_ENFORCE_STATION_IP` still fails if the
    attacker also takes the station's address; real control is on-device key storage +
    short-lived certs.
  - Responses cause collateral damage: quarantining a hijacked identity stops the genuine
    charger; blocking a cloned card blocks the real holder; bulk quarantine interrupts many
    legitimate drivers — measured, not hidden.
  - Detection gaps: MitM detection is inference from physics, not proof; `grid_gap` trusts the
    feeder meter; a per-station-address botnet defeats the same-dstip correlation; a slow flood
    under the rate limit evades 110300.
  - Lab artefacts: the C2 lives inside the lab (egress concession); edge→CSMS link is plain
    HTTP inside the lab; single-level CA; baseline window 12 h not 24 h; no promtool on host.
  - Scale actually reached vs the 1500 target (state it).
- 7.3 Future work: egress filtering + signed firmware; intermediate CA + short-lived certs;
  throttle-not-quarantine responses; hour-of-day baselines for real traffic; cross-checks
  against grid import totals; NetworkPolicy in addition to Incus ACLs; edge→CSMS TLS.

Figures: none.

---

## Figure/table master list

| ID | Title | Section | Status | Source |
|---|---|---|---|---|
| Fig. A | Architecture diagram | 3 | [user redraw] | `.ai/HANDOFF.md §3` ASCII |
| Fig. B | Hierarchical naming scheme | 3 | [user draw] / typeset | `interfaces.md §1.2` table |
| Fig. C | Attack–defense tree | 4 | [user Excalidraw] | spec in sec1-4 card + §4.2 |
| Fig. D | Grafana Geomap (all green; grey/red) | 4 (+5) | [user screenshot] | `evcharge-map`; phase4 README steps |
| Fig. E | Bandwidth: federated vs direct | 4 (+6) | [have, typeset] / [user screenshot] | `evidence/phase4/bandwidth.txt`, `evcharge-bandwidth` |
| Table 4.1 | Attack surfaces → defences | 4 | [have] | §4.1 synthesis |
| Table 4.4 | Baseline thresholds per city | 4 | [have] | `monitoring/rules/baseline.yml` |
| Fig. 5.x | Per-threat alert JSON + panel + Wazuh shot | 5 | [run]/[user] | `evidence/hijack|fraud|botnet/` |
| Table 5.3 | Botnet collateral-damage | 5 | [run] | `poc/botnet/measure_collateral.sh` output |
| Table 6.3 | Scalability (N chargers vs CPU/mem/latency/series) | 6 | [run] | `eval/` runs |
| Table 6.4 | Resource budget | 6 | [have] | `evidence/phase4/README.md` RAM table |

## What can be written now vs after the runs
- **Now (card W11-sec1-4):** Sections 1, 2, 3, 4 in full. Section 4 uses real monitoring /
  bandwidth / baseline numbers already in `evidence/phase4/` and `monitoring/rules/baseline.yml`.
- **After the runs (card W11-sec5-7):** Section 5 (needs Phases 6–9 threat evidence +
  correlator timeline), Section 6.1/6.3 (needs the scalability harness + correlator metrics),
  and the final tightening of Section 7.2/7.3 once the actual scale and collateral numbers
  exist. Section 7 can be drafted now and finalised after.
