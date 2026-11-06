"""Request and response bodies of the HTTP APIs (docs/interfaces.md, section 4).

All timestamps are timezone-aware UTC datetimes, serialised as ISO 8601.
Bodies reject unknown fields (extra="forbid") unless marked as a stub.
"""

from __future__ import annotations

import datetime as dt
import re
from typing import Annotated, Any, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field

from . import hierid

# ---- shared field types -----------------------------------------------------

CARD_REGEX = r"^[0-9A-F]{8,20}$"
_CARD_RE = re.compile(CARD_REGEX)


def _hier_id(v: str) -> str:
    hierid.parse(v)
    return v


def _of_kind(kind: str):
    def check(v: str) -> str:
        if hierid.parse(v).kind != kind:
            raise ValueError(f"expected a {kind} hier-id")
        return v
    return check


def _prefix(v: str) -> str:
    if not hierid.is_prefix(v):
        raise ValueError("not a hier-id prefix")
    return v


def _card(v: str) -> str:
    if not _CARD_RE.match(v):
        raise ValueError("card id must be 8-20 upper-case hex characters")
    return v


HierIdStr = Annotated[str, AfterValidator(_hier_id)]
ChargerIdStr = Annotated[str, AfterValidator(_of_kind("charger"))]
StationIdStr = Annotated[str, AfterValidator(_of_kind("station"))]
PrefixStr = Annotated[str, AfterValidator(_prefix)]
CardId = Annotated[str, AfterValidator(_card)]
UtcDatetime = dt.datetime

NodeKind = Literal["city", "station", "charger", "edge", "central"]
NodeState = Literal["active", "quarantined", "retired"]
OcppAuthStatus = Literal["Accepted", "Blocked", "Expired", "Invalid", "ConcurrentTx"]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Health(_Strict):
    status: Literal["ok", "degraded"] = "ok"
    service: str
    version: str
    checks: dict[str, str] = Field(default_factory=dict)


class AuditMeta(_Strict):
    """Who asked for an action and why. Carried by every response action."""
    reason: str = Field(min_length=1, max_length=200)
    source: Literal["wazuh", "fraud", "correlator", "operator", "test"] = "operator"
    incident_id: str | None = Field(default=None, max_length=64)


# ---- registry ---------------------------------------------------------------

class NodeIn(_Strict):
    """Body of PUT /nodes/{hier_id}. Kind, parent and city come from the id."""
    name: str | None = Field(default=None, max_length=120)
    lat: float | None = Field(default=None, ge=-90, le=90)
    lon: float | None = Field(default=None, ge=-180, le=180)
    max_kw: float | None = Field(default=None, gt=0, le=400)
    connectors: int | None = Field(default=None, ge=1, le=4)
    vendor: str | None = Field(default=None, max_length=40)
    model: str | None = Field(default=None, max_length=40)
    serial: str | None = Field(default=None, max_length=40)
    cert_serial: str | None = Field(default=None, pattern=r"^[0-9A-F]{2,64}$")
    host: str | None = Field(default=None, max_length=64)
    ip: str | None = Field(default=None, max_length=45)
    network: str | None = Field(default=None, max_length=64)
    dns: str | None = Field(default=None, max_length=253)
    metrics_target: str | None = Field(default=None, max_length=253)


class NodePatch(NodeIn):
    """Body of PATCH /nodes/{hier_id}: only the fields present are changed."""


class Node(NodeIn):
    model_config = ConfigDict(extra="forbid", from_attributes=True)
    hier_id: HierIdStr
    kind: NodeKind
    parent: str | None = None
    city: str | None = None
    station: str | None = None
    charger: str | None = None
    state: NodeState = "active"
    effective_state: NodeState = "active"   # quarantined if any ancestor is
    state_reason: str | None = None
    state_source: str | None = None
    state_changed_at: UtcDatetime | None = None
    created_at: UtcDatetime
    updated_at: UtcDatetime
    updated_by: str


class NodeList(_Strict):
    count: int
    items: list[Node]


class QuarantineRequest(AuditMeta):
    pass


class LocateMatch(_Strict):
    hier_id: HierIdStr
    kind: NodeKind
    state: NodeState
    effective_state: NodeState
    path: list[str]                  # city -> ... -> element
    operator: str                    # who is responsible: city name, or "noc" for central
    edge: str | None = None          # edge gateway serving it
    station: str | None = None       # hosting station (chargers and stations)
    lat: float | None = None
    lon: float | None = None
    host: str | None = None          # Incus instance / K8s node
    ip: str | None = None
    network: str | None = None
    dns: str | None = None


class LocateResponse(_Strict):
    query: str
    count: int
    truncated: bool = False
    matches: list[LocateMatch]


class WhoAmI(_Strict):
    operator: str                    # "admin" or a city name
    scope: str                       # "pt" or "pt.<city>"


class PromSdTarget(_Strict):
    targets: list[str]
    labels: dict[str, str]


# ---- billing ----------------------------------------------------------------

class AuthorizeRequest(_Strict):
    card: CardId
    hier_id: ChargerIdStr


class AuthorizeResponse(_Strict):
    card: str
    status: OcppAuthStatus
    expiry_date: UtcDatetime | None = None


class SessionStart(_Strict):
    transaction_id: int = Field(ge=1)
    card: CardId
    hier_id: ChargerIdStr
    connector_id: int = Field(ge=1)
    meter_start_wh: int = Field(ge=0)
    started_at: UtcDatetime


class SessionStop(_Strict):
    meter_stop_wh: int = Field(ge=0)
    stopped_at: UtcDatetime
    reason: str = "Local"


class Session(_Strict):
    transaction_id: int
    card: str
    hier_id: str
    city: str
    connector_id: int
    tariff_id: str
    started_at: UtcDatetime
    stopped_at: UtcDatetime | None = None
    meter_start_wh: int
    meter_stop_wh: int | None = None
    energy_wh: int | None = None
    cost_eur: float | None = None
    status: Literal["open", "closed", "flagged"]
    flag_reason: str | None = None
    stop_reason: str | None = None


class SessionList(_Strict):
    count: int
    items: list[Session]


class SessionFlag(AuditMeta):
    pass


class CardIn(_Strict):
    card: CardId
    holder: str = Field(max_length=64)
    home_city: str
    tariff_id: str = "standard"
    expiry_date: UtcDatetime | None = None


class Card(CardIn):
    status: Literal["active", "blocked", "expired"]
    blocked_reason: str | None = None
    blocked_at: UtcDatetime | None = None


class CardBlockRequest(AuditMeta):
    pass


class Tariff(_Strict):
    tariff_id: str
    price_per_kwh_eur: float
    price_per_min_eur: float = 0.0
    session_fee_eur: float = 0.0
    currency: Literal["EUR"] = "EUR"


# ---- csms -------------------------------------------------------------------

class Connection(_Strict):
    hier_id: ChargerIdStr
    city: str
    station: str
    pod: str
    src_ip: str
    edge: str | None = None
    cert_cn: str | None = None
    cert_serial: str | None = None
    tls: bool
    since: UtcDatetime
    last_seen: UtcDatetime
    booted: bool = False
    status: str | None = None           # last connector-1 status
    active_transaction_id: int | None = None


class ConnectionList(_Strict):
    count: int
    items: list[Connection]


class CommandBase(_Strict):
    reason: str | None = Field(default=None, max_length=200)
    source: Literal["wazuh", "fraud", "correlator", "operator", "test"] = "operator"
    incident_id: str | None = Field(default=None, max_length=64)


class RemoteStopRequest(CommandBase):
    transaction_id: int | None = None   # None: the charger's open transaction


class ChangeAvailabilityRequest(CommandBase):
    connector_id: int = Field(default=0, ge=0)
    type: Literal["Operative", "Inoperative"]


class ResetRequest(CommandBase):
    type: Literal["Soft", "Hard"] = "Soft"


class DisconnectRequest(CommandBase):
    pass


CommandName = Literal["remote-stop", "change-availability", "reset", "disconnect"]
CommandStatus = Literal["Accepted", "Rejected", "Scheduled", "NotConnected", "Timeout", "Error"]


class CommandResult(_Strict):
    cmd_id: str
    hier_id: str
    command: CommandName
    status: CommandStatus
    pod: str | None = None
    detail: str | None = None


# ---- fraud (stub: finalised in Phase 7) -------------------------------------------

class FraudEvent(BaseModel):
    """POST /events on the fraud service, sent by the CSMS (best effort)."""
    model_config = ConfigDict(extra="allow")
    event: Literal["session.start", "session.stop", "meter.anomaly"]
    ts: UtcDatetime
    hier_id: ChargerIdStr
    city: str
    station: str
    card: str | None = None
    transaction_id: int | None = None
    max_kw: float | None = None
    meter_start_wh: int | None = None
    meter_stop_wh: int | None = None
    energy_wh: int | None = None
    duration_s: float | None = None
    peak_power_w: float | None = None
    reason: str | None = None
    srcip: str | None = None


class FraudAlert(_Strict):
    """One fraud finding (section 4.4): stored by the fraud service, returned by GET /alerts and
    posted to the correlator's POST /signals/fraud."""
    alert_id: str
    ts: UtcDatetime
    rule: str                              # impossible_travel|over_energy|meter_backwards|peer_deviation|grid_gap|card_velocity
    severity: Literal["info", "warning", "critical"]
    card: str | None = None
    hier_id: str | None = None
    other_hier_id: str | None = None       # impossible_travel: the earlier session's charger
    city: str | None = None
    score: float = Field(ge=0, le=1)
    detail: str
    evidence: dict[str, Any] = Field(default_factory=dict)


# ---- correlator (stub: finalised in Phase 9) ---------------------------------------

class RootCause(BaseModel):
    model_config = ConfigDict(extra="allow")
    key: Literal["hier_id", "srcip", "card"]
    value: str
    hier_id: str | None = None          # located element (via registry /locate)
    explanation: str | None = None


class Incident(BaseModel):
    model_config = ConfigDict(extra="allow")
    incident_id: str
    kind: Literal["hijack", "fraud", "botnet", "infra", "other"]
    status: Literal["open", "mitigated", "closed"]
    root_cause: RootCause | None = None
    signals: list[dict[str, Any]] = Field(default_factory=list)
    t_first_signal: UtcDatetime
    t_located: UtcDatetime | None = None
    t_mitigated: UtcDatetime | None = None
