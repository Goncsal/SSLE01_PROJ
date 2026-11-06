"""Prometheus metric names, types and labels (docs/interfaces.md, section 7).

Services create their metrics through `make()` so names and label sets cannot
drift from the contract:

    from evcommon import metrics as m
    CONNECTED = m.make(m.CSMS_CONNECTED_CHARGERS)
    CONNECTED.labels(city="porto", station="st-003", hier_id="pt.porto.st-003").inc()

Scope:
  per_charger  label set includes the charger; scraped only by the city Prometheus
  per_station  bounded by the number of stations; may be scraped centrally
  aggregate    bounded by a small fixed set (cities, actions, reasons)

Counters are declared with their exposed name (ending in _total).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

MetricType = Literal["counter", "gauge", "histogram"]
Scope = Literal["per_charger", "per_station", "aggregate"]

CHARGER_LABELS = ("hier_id", "city", "station", "charger")
STATION_LABELS = ("hier_id", "city", "station")

LATENCY_BUCKETS = (0.001, 0.0025, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0)


@dataclass(frozen=True, slots=True)
class MetricSpec:
    name: str
    type: MetricType
    help: str
    labels: tuple[str, ...] = ()
    scope: Scope = "aggregate"
    buckets: tuple[float, ...] | None = None


def _c(name, help, labels=(), scope="aggregate"):
    assert name.endswith("_total"), name
    return MetricSpec(name, "counter", help, tuple(labels), scope)


def _g(name, help, labels=(), scope="aggregate"):
    return MetricSpec(name, "gauge", help, tuple(labels), scope)


def _h(name, help, labels=(), scope="aggregate", buckets=LATENCY_BUCKETS):
    return MetricSpec(name, "histogram", help, tuple(labels), scope, buckets)


# ---- csms (central Prometheus; never per charger) ---------------------------
CSMS_CONNECTED_CHARGERS = _g("evcharge_csms_connected_chargers",
    "Chargers with an open OCPP connection on this replica", STATION_LABELS, "per_station")
CSMS_CONNECTIONS_TOTAL = _c("evcharge_csms_connections_total",
    "WebSocket handshakes by outcome (accepted|rejected)", ("city", "result"))
CSMS_IDENTITY_REJECTIONS_TOTAL = _c("evcharge_csms_identity_rejections_total",
    "Handshakes refused by identity checks; reason = ident.* event suffix or duplicate_id",
    ("city", "reason"))
CSMS_DUPLICATE_ID_TOTAL = _c("evcharge_csms_duplicate_id_total",
    "Second connection for a connected id (outcome rejected|replaced)", ("city", "outcome"))
CSMS_OCPP_MESSAGES_TOTAL = _c("evcharge_csms_ocpp_messages_total",
    "OCPP CALL messages by action and direction (in = from charger)", ("city", "action", "direction"))
CSMS_OCPP_ERRORS_TOTAL = _c("evcharge_csms_ocpp_errors_total",
    "OCPP errors (invalid|not_implemented|before_boot|call_error|timeout)", ("city", "kind"))
CSMS_OCPP_HANDLE_SECONDS = _h("evcharge_csms_ocpp_handle_seconds",
    "Time to handle an incoming OCPP CALL", ("action",))
CSMS_RATE_LIMITED_TOTAL = _c("evcharge_csms_rate_limited_total",
    "Messages dropped by the per-connection rate limit", ("city",))
CSMS_CARD_AUTH_TOTAL = _c("evcharge_csms_card_auth_total",
    "Card authorisations by OCPP status (Accepted|Blocked|Expired|Invalid|ConcurrentTx|Error)",
    ("city", "result"))
CSMS_SESSIONS_ACTIVE = _g("evcharge_csms_sessions_active",
    "Open transactions on this replica", STATION_LABELS, "per_station")
CSMS_SESSIONS_STARTED_TOTAL = _c("evcharge_csms_sessions_started_total",
    "Transactions started", ("city",))
CSMS_SESSIONS_STOPPED_TOTAL = _c("evcharge_csms_sessions_stopped_total",
    "Transactions stopped by OCPP reason", ("city", "reason"))
CSMS_ENERGY_WH_TOTAL = _c("evcharge_csms_energy_wh_total",
    "Energy delivered as reported by chargers (sum of register deltas), Wh", STATION_LABELS, "per_station")
CSMS_POWER_WATTS = _g("evcharge_csms_power_watts",
    "Sum of the last reported Power.Active.Import of open transactions, W", STATION_LABELS, "per_station")
CSMS_METER_ANOMALIES_TOTAL = _c("evcharge_csms_meter_anomalies_total",
    "meter.anomaly events by reason", ("city", "reason"))
CSMS_COMMANDS_TOTAL = _c("evcharge_csms_commands_total",
    "Commands to chargers by command and result", ("command", "result"))
CSMS_DEPENDENCY_ERRORS_TOTAL = _c("evcharge_csms_dependency_errors_total",
    "Failed calls to registry|billing|fraud|database", ("dependency",))
CSMS_DEPENDENCY_SECONDS = _h("evcharge_csms_dependency_seconds",
    "Latency of calls to registry|billing|fraud", ("dependency",))
CSMS_EVENTS_DROPPED_TOTAL = _c("evcharge_csms_events_dropped_total",
    "Fraud events dropped because the outbound queue was full", ())

# ---- registry ---------------------------------------------------------------
REGISTRY_NODES = _g("evcharge_registry_nodes",
    "Registered elements by kind and state", ("city", "kind", "state"))
REGISTRY_STATION_INFO = _g("evcharge_registry_station_info",
    "Always 1; carries station coordinates for the Grafana Geomap",
    ("hier_id", "city", "station", "lat", "lon"), "per_station")
REGISTRY_WRITES_TOTAL = _c("evcharge_registry_writes_total",
    "Write requests by operator (admin or city) and result (ok|denied|invalid|not_found)",
    ("operator", "result"))
REGISTRY_QUARANTINES_TOTAL = _c("evcharge_registry_quarantines_total",
    "Quarantine and release actions", ("city", "action"))

# ---- billing ----------------------------------------------------------------
BILLING_AUTHORIZE_TOTAL = _c("evcharge_billing_authorize_total",
    "Authorisation answers by status", ("result",))
BILLING_SESSIONS_TOTAL = _c("evcharge_billing_sessions_total",
    "Session state transitions (opened|closed|flagged)", ("city", "status"))
BILLING_ENERGY_WH_TOTAL = _c("evcharge_billing_energy_wh_total",
    "Billed energy, Wh", ("city",))
BILLING_REVENUE_EUR_TOTAL = _c("evcharge_billing_revenue_eur_total",
    "Billed amount, EUR", ("city",))
BILLING_BLOCKED_CARDS = _g("evcharge_billing_blocked_cards",
    "Cards currently blocked", ())
BILLING_CARD_BLOCKS_TOTAL = _c("evcharge_billing_card_blocks_total",
    "Card blocks by source (wazuh|fraud|operator)", ("source",))

# ---- fraud (finalised in Phase 7) ------------------------------------------------
FRAUD_EVENTS_TOTAL = _c("evcharge_fraud_events_total",
    "Events received from the CSMS", ("event",))
FRAUD_ALERTS_TOTAL = _c("evcharge_fraud_alerts_total",
    "Fraud rules fired", ("city", "rule"))
FRAUD_ACTIONS_TOTAL = _c("evcharge_fraud_actions_total",
    "Responses requested (block_card|quarantine|flag_session)", ("action",))
FRAUD_HIGH_RISK_CARDS = _g("evcharge_fraud_high_risk_cards",
    "Cards whose score is above the alert threshold", ())

# ---- correlator (finalised in Phase 9) -------------------------------------------
CORRELATOR_SIGNALS_TOTAL = _c("evcharge_correlator_signals_total",
    "Signals received (alertmanager|wazuh)", ("source",))
CORRELATOR_INCIDENTS_OPEN = _g("evcharge_correlator_incidents_open",
    "Open incidents", ())
CORRELATOR_INCIDENTS_TOTAL = _c("evcharge_correlator_incidents_total",
    "Incidents opened by kind (hijack|fraud|botnet|infra|other)", ("kind",))
CORRELATOR_TIME_TO_LOCATE_SECONDS = _h("evcharge_correlator_time_to_locate_seconds",
    "First signal to located root cause", (), buckets=(1, 2, 5, 10, 20, 30, 60, 120, 300))
CORRELATOR_TIME_TO_MITIGATE_SECONDS = _h("evcharge_correlator_time_to_mitigate_seconds",
    "First signal to mitigation", (), buckets=(1, 2, 5, 10, 20, 30, 60, 120, 300, 600))

# ---- simulator (station controller; city Prometheus only) -------------------
SIM_CHARGER_CONNECTED = _g("evcharge_sim_charger_connected",
    "1 while the charger's OCPP connection is open and booted", CHARGER_LABELS, "per_charger")
SIM_CHARGER_STATUS = _g("evcharge_sim_charger_status",
    "Connector 1 status as a code (see CHARGER_STATUS_CODES)", CHARGER_LABELS, "per_charger")
SIM_POWER_WATTS = _g("evcharge_sim_power_watts",
    "Power the charger reports to the CSMS, W", CHARGER_LABELS, "per_charger")
SIM_ENERGY_REGISTER_WH = _g("evcharge_sim_energy_register_wh",
    "Energy register the charger reports to the CSMS, Wh", CHARGER_LABELS, "per_charger")
SIM_OCPP_MESSAGES_SENT_TOTAL = _c("evcharge_sim_ocpp_messages_sent_total",
    "OCPP CALLs sent by the charger", CHARGER_LABELS + ("action",), "per_charger")
SIM_RECONNECTS_TOTAL = _c("evcharge_sim_reconnects_total",
    "Connection attempts after the first", CHARGER_LABELS, "per_charger")
SIM_SESSIONS_TOTAL = _c("evcharge_sim_sessions_total",
    "Driver sessions by result (completed|rejected|interrupted)", CHARGER_LABELS + ("result",), "per_charger")
SIM_OCPP_CALL_SECONDS = _h("evcharge_sim_ocpp_call_seconds",
    "Round trip of a CALL as seen by the charger", STATION_LABELS + ("action",), "per_station")
SIM_STATION_GRID_POWER_WATTS = _g("evcharge_sim_station_grid_power_watts",
    "True power drawn by the whole station at the grid connection (feeder meter), W",
    STATION_LABELS, "per_station")

# OCPP 1.6 ChargePointStatus -> evcharge_sim_charger_status value
CHARGER_STATUS_CODES = {
    "Available": 0, "Preparing": 1, "Charging": 2, "SuspendedEV": 3, "SuspendedEVSE": 4,
    "Finishing": 5, "Reserved": 6, "Unavailable": 7, "Faulted": 8,
}

ALL: tuple[MetricSpec, ...] = tuple(v for v in globals().values() if isinstance(v, MetricSpec))

# ---- recording rules evaluated in each city Prometheus and federated ---------
# Expressions: docs/interfaces.md section 7.3. Station-level rules carry the station's
# hier_id (built from city and station with label_join/label_replace).
CITY_RECORDING_RULES = (
    "city:chargers_connected:sum",              # by (city)
    "city:station_chargers:count",              # by (city, station, hier_id): chargers in the station
    "city:station_chargers_connected:sum",      # by (city, station, hier_id)
    "city:station_chargers_charging:sum",       # by (city, station, hier_id)
    "city:station_chargers_faulted:sum",        # by (city, station, hier_id)
    "city:power_watts:sum",                     # by (city)
    "city:grid_power_watts:sum",                # by (city): feeder meters, the truth
    "city:station_power_watts:sum",             # by (city, station, hier_id)
    "city:station_grid_power_watts:sum",        # by (city, station, hier_id)
    "city:station_power_gap_watts",             # grid minus reported, by station
    "city:ocpp_messages_sent:rate1m",           # by (city)
    "city:station_ocpp_messages_sent:rate1m",   # by (city, station, hier_id)
    "city:charger_msg_rate:max1m",              # by (city): busiest single charger
    "city:reconnects:rate5m",                   # by (city)
    "city:station_up:min",                      # by (city, station, hier_id)
    "city:edge_up:min",                         # by (city): nginx and its exporter answer
    "city:edge_ws_connections:sum",             # by (city)
    "city:edge_requests:rate1m",                # by (city)
)

# ---- alert names evaluated by the central Prometheus (section 14.3) -----------
ALERT_NAMES = (
    "CSMSDown", "RegistryDown", "BillingDown", "EdgeDown", "StationDown", "ChargerOffline",
    "ChargerFlapping", "DuplicateChargerID", "IdentityRejections", "AuthFailureBurst",
    "MsgRateAnomaly", "ChargerMsgFlood", "CityLoadSpike", "MeterUnderReporting",
    "MeterAnomalies",
)


def spec(name: str) -> MetricSpec:
    for s in ALL:
        if s.name == name:
            return s
    raise KeyError(name)


def make(s: MetricSpec, registry=None):
    """Create the prometheus_client object for a spec (registry=None: default)."""
    import prometheus_client as pc

    kwargs = {"labelnames": s.labels}
    if registry is not None:
        kwargs["registry"] = registry
    if s.type == "counter":
        return pc.Counter(s.name, s.help, **kwargs)
    if s.type == "gauge":
        return pc.Gauge(s.name, s.help, **kwargs)
    return pc.Histogram(s.name, s.help, buckets=s.buckets or LATENCY_BUCKETS, **kwargs)


def init_exposition() -> None:
    """Call once at startup: drops the *_created series (halves counter series)."""
    import prometheus_client as pc

    pc.disable_created_metrics()


def render(registry=None) -> tuple[bytes, str]:
    """Body and content type for a GET /metrics handler (no redirect, exact path)."""
    import prometheus_client as pc

    reg = registry if registry is not None else pc.REGISTRY
    return pc.generate_latest(reg), pc.CONTENT_TYPE_LATEST
