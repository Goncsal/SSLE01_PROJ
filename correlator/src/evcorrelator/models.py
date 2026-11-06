"""Models of the correlator (docs/interfaces.md, section 4.5).

`evcommon.models` only holds a stub of `Incident` and `RootCause` (and no `FraudAlert`
yet), so the finished shapes of section 4.5 live here. They are supersets of the stub:
an `Incident` serialised here validates against `evcommon.models.Incident`.
"""

from __future__ import annotations

import datetime as dt
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

Kind = Literal["hijack", "fraud", "botnet", "infra", "other"]
Severity = Literal["info", "warning", "critical"]
Source = Literal["prometheus", "wazuh", "fraud"]
KeyType = Literal["hier_id", "srcip", "card"]
Status = Literal["open", "mitigated", "closed"]

SEVERITY_RANK = {"info": 0, "warning": 1, "critical": 2}
KINDS = ("hijack", "fraud", "botnet", "infra")


class Signal(BaseModel):
    """One alert from any source in the common shape of section 4.5."""

    model_config = ConfigDict(extra="ignore")

    signal_id: str = ""
    source: Source
    key_type: KeyType                    # the primary join key of this signal
    key: str
    hier_id: str | None = None
    other_hier_id: str | None = None     # fraud: the second charger of impossible_travel
    srcip: str | None = None
    card: str | None = None
    city: str | None = None
    kind: Kind
    severity: Severity
    name: str = ""                       # alertname, Wazuh rule id or fraud rule
    ts: dt.datetime                      # when it happened, according to the source clock
    last_ts: dt.datetime | None = None   # last repetition folded into this signal
    count: int = 1
    response: bool = False               # the signal is itself a mitigation (section 4.5)
    dedupe_key: str = ""
    raw: dict[str, Any] = Field(default_factory=dict)

    def model_post_init(self, __context) -> None:
        if self.last_ts is None:
            self.last_ts = self.ts

    @property
    def hier_ids(self) -> list[str]:
        return [h for h in (self.hier_id, self.other_hier_id) if h]


class FraudAlert(BaseModel):
    """Section 4.4. Local copy until `evcommon.models` has it; unknown fields are ignored."""

    model_config = ConfigDict(extra="ignore")

    alert_id: str = Field(max_length=100)
    ts: dt.datetime
    rule: str = Field(max_length=60)
    severity: Severity
    card: str | None = Field(default=None, max_length=40)
    hier_id: str | None = Field(default=None, max_length=100)
    other_hier_id: str | None = Field(default=None, max_length=100)
    city: str | None = Field(default=None, max_length=60)
    score: float | None = None
    detail: str | None = Field(default=None, max_length=1000)
    evidence: dict[str, Any] = Field(default_factory=dict)


class RootCause(BaseModel):
    model_config = ConfigDict(extra="allow")

    key_type: KeyType
    key: str
    hier_id: str | None = None           # the located element
    host: str | None = None
    ip: str | None = None
    city: str | None = None
    edge: str | None = None
    explanation: str = ""


class Incident(BaseModel):
    model_config = ConfigDict(extra="allow")

    incident_id: str
    kind: Kind
    status: Status
    severity: Severity
    root_cause: RootCause | None = None
    signals: list[dict[str, Any]] = Field(default_factory=list)
    keys: dict[str, list[str]] = Field(default_factory=dict)
    t_first_signal: dt.datetime
    t_located: dt.datetime | None = None
    t_mitigated: dt.datetime | None = None
    t_closed: dt.datetime | None = None
    summary: str = ""


class MitigatedRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    action: str = Field(max_length=200)
    by: str = Field(max_length=100)
    ts: dt.datetime | None = None


class CloseRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    by: str = Field(max_length=100)
    note: str | None = Field(default=None, max_length=500)
