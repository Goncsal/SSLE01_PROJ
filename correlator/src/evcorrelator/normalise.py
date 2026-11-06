"""Each source -> the common signal shape (docs/interfaces.md, section 4.5).

Everything here is untrusted input: values are only ever read, never evaluated; ids,
addresses and cards that do not match their grammar are dropped (the signal then has
fewer join keys); unknown fields are ignored; what is kept in `raw` is size-bounded.
"""

from __future__ import annotations

import datetime as dt
import ipaddress
import re
from typing import Any

from evcommon import hierid
from evcommon.models import CARD_REGEX

from .models import FraudAlert, Signal, SEVERITY_RANK
from .timeutil import parse_ts, utcnow

MAX_ALERTS_PER_BATCH = 200
MAX_RAW_KEYS = 40
MAX_STR = 200
_CARD_RE = re.compile(CARD_REGEX)
_CITY_RE = hierid.CITY_RE
_KINDS = ("hijack", "fraud", "botnet", "infra")


# ---- validated key fields ---------------------------------------------------

def clean_hier_id(value: Any) -> str | None:
    return value if isinstance(value, str) and len(value) <= 100 and hierid.is_prefix(value) else None


def clean_srcip(value: Any) -> str | None:
    if not isinstance(value, str) or len(value) > 45:
        return None
    try:
        return str(ipaddress.ip_address(value.strip()))
    except ValueError:
        return None


def clean_card(value: Any) -> str | None:
    return value if isinstance(value, str) and _CARD_RE.match(value) else None


def clean_city(value: Any) -> str | None:
    return value if isinstance(value, str) and len(value) <= hierid.MAX_CITY_LEN and _CITY_RE.match(value) else None


def _bound(value: Any, depth: int = 0) -> Any:
    """Copy of a JSON value with bounded depth, key count and string length."""
    if isinstance(value, str):
        return value[:MAX_STR]
    if isinstance(value, (bool, int, float)) or value is None:
        return value
    if depth >= 3:
        return None
    if isinstance(value, dict):
        return {str(k)[:60]: _bound(v, depth + 1) for k, v in list(value.items())[:MAX_RAW_KEYS]}
    if isinstance(value, list):
        return [_bound(v, depth + 1) for v in value[:MAX_RAW_KEYS]]
    return None


def _severity_from_label(value: Any) -> str:
    return value if isinstance(value, str) and value in SEVERITY_RANK else "warning"


def _primary_key(hier_id: str | None, srcip: str | None, card: str | None) -> tuple[str, str] | None:
    """The first join key present, in the order of section 4.5: hier_id, srcip, card."""
    for key_type, key in (("hier_id", hier_id), ("srcip", srcip), ("card", card)):
        if key:
            return key_type, key
    return None


# ---- Prometheus (Alertmanager webhook v4) ---------------------------------------

def from_alertmanager(payload: Any, now: dt.datetime | None = None) -> list[Signal]:
    """Firing alerts of an Alertmanager v4 webhook. Resolved alerts carry no new evidence."""
    now = now or utcnow()
    alerts = payload.get("alerts") if isinstance(payload, dict) else None
    out: list[Signal] = []
    for alert in (alerts if isinstance(alerts, list) else [])[:MAX_ALERTS_PER_BATCH]:
        if not isinstance(alert, dict) or alert.get("status", "firing") != "firing":
            continue
        labels = alert.get("labels") if isinstance(alert.get("labels"), dict) else {}
        name = str(labels.get("alertname", ""))[:80]
        city = clean_city(labels.get("city"))
        hier_id = clean_hier_id(labels.get("hier_id")) or (hierid.city(city) if city and city != "central" else None)
        key = _primary_key(hier_id, None, None)
        if not name or key is None:
            continue                                  # nothing to join on
        threat = labels.get("threat")
        ts = parse_ts(alert.get("startsAt")) or now
        stable = sorted((str(k), str(v)) for k, v in labels.items())
        out.append(Signal(
            signal_id=f"prom-{len(out)}-{int(ts.timestamp())}",
            source="prometheus", key_type=key[0], key=key[1], hier_id=hier_id, city=city,
            kind=threat if threat in _KINDS else "other",
            severity=_severity_from_label(labels.get("severity")),
            name=name, ts=ts,
            dedupe_key="prom|" + name + "|" + "|".join(f"{k}={v}" for k, v in stable),
            raw={"labels": _bound(labels),
                 "annotations": _bound(alert.get("annotations") if isinstance(alert.get("annotations"), dict) else {})},
        ))
    return out


# ---- Wazuh ---------------------------------------------------------------------

def _wazuh_severity(level: int) -> str:
    return "critical" if level >= 10 else "warning" if level >= 7 else "info"


def is_response(event: str | None, result: str | None) -> bool:
    """A log event that is itself a response: it sets t_mitigated when joined (section 4.5)."""
    return (event == "ar.action" and result == "ok") or event in ("registry.quarantine", "billing.card_blocked")


def from_wazuh(alert: Any, now: dt.datetime | None = None) -> Signal | None:
    """One Wazuh alert (JSON as written to alerts.json: `rule`, `data`, `timestamp`, `id`)."""
    now = now or utcnow()
    if not isinstance(alert, dict):
        return None
    rule = alert.get("rule") if isinstance(alert.get("rule"), dict) else {}
    data = alert.get("data") if isinstance(alert.get("data"), dict) else {}
    hier_id = clean_hier_id(data.get("hier_id"))
    srcip = clean_srcip(data.get("srcip"))
    card = clean_card(data.get("card"))
    key = _primary_key(hier_id, srcip, card)
    if key is None:
        return None
    try:
        level = int(rule.get("level", 0))
    except (TypeError, ValueError):
        level = 0
    groups = rule.get("groups") if isinstance(rule.get("groups"), list) else []
    kind = next((g for g in groups if g in _KINDS), "other")
    rule_id = str(rule.get("id", ""))[:20]
    event = data.get("event") if isinstance(data.get("event"), str) else None
    result = data.get("result") if isinstance(data.get("result"), str) else None
    # the log line's own time is closest to the event; the alert time is the manager's
    ts = parse_ts(data.get("ts")) or parse_ts(alert.get("timestamp")) or now
    response = is_response(event, result)
    return Signal(
        signal_id=str(alert.get("id", ""))[:40],
        source="wazuh", key_type=key[0], key=key[1],
        hier_id=hier_id, srcip=srcip, card=card, city=clean_city(data.get("city")),
        kind=kind, severity=_wazuh_severity(level), name=rule_id or (event or "")[:60],
        ts=ts, response=response,
        dedupe_key=f"wazuh|{rule_id}|{hier_id}|{srcip}|{card}",
        raw={"rule": _bound({k: rule.get(k) for k in ("id", "level", "description", "groups")}),
             "data": _bound(data), "id": _bound(alert.get("id"))},
    )


# ---- fraud service --------------------------------------------------------------

def from_fraud(alert: FraudAlert) -> Signal | None:
    hier_id = clean_hier_id(alert.hier_id)
    other = clean_hier_id(alert.other_hier_id)
    card = clean_card(alert.card)
    key = _primary_key(hier_id, None, card)
    if key is None:
        return None
    return Signal(
        signal_id=alert.alert_id, source="fraud", key_type=key[0], key=key[1],
        hier_id=hier_id, other_hier_id=other, card=card, city=clean_city(alert.city),
        kind="fraud", severity=alert.severity, name=alert.rule, ts=alert.ts,
        dedupe_key=f"fraud|{alert.rule}|{hier_id}|{other}|{card}",
        raw={"rule": alert.rule, "score": alert.score, "detail": alert.detail,
             "evidence": _bound(alert.evidence)},
    )
