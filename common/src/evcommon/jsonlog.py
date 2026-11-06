"""JSON event logs shared by every service (docs/interfaces.md, section 6).

One JSON object per line, flat, written to ${LOG_DIR}/<service>-<instance>.json
and to stdout. Wazuh reads the files with log_format json; field names such as
`srcip` are chosen so the Wazuh JSON decoder maps them to its static fields.

    log = setup_logging("csms")
    log.event("charger.duplicate_id", "id already connected",
              level="warning", hier_id="pt.porto.st-003.cp-2",
              srcip="10.31.0.66", other_srcip="10.31.0.13")
"""

from __future__ import annotations

import datetime as _dt
import enum
import json
import logging
import logging.handlers
import re
import sys
from collections.abc import Iterable
from pathlib import Path

from . import config, hierid

SCHEMA_VERSION = 1
BASE_FIELDS = ("ts", "level", "service", "instance", "event", "msg")
LEVELS = {"debug": logging.DEBUG, "info": logging.INFO, "warning": logging.WARNING,
          "error": logging.ERROR, "critical": logging.CRITICAL}
_FIELD_RE = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
_LOGGER_NAME = "evcharge"

# The event vocabulary. Keep in sync with docs/interfaces.md section 6.3.
EVENTS: dict[str, str] = {
    # lifecycle, any service
    "service.start": "process started (version, listen addresses; never secrets)",
    "service.stop": "process stopping",
    "service.log": "warning or error from a library logger",
    "dependency.error": "a peer service or database failed (dependency, reason)",
    "api.auth_failed": "HTTP API call with a missing or wrong bearer token",
    # csms: connections and charger identity
    "conn.open": "OCPP WebSocket accepted",
    "conn.close": "OCPP WebSocket closed",
    "conn.replaced": "a dead connection for the same id was replaced by a new one",
    "conn.kicked": "the CSMS closed a connection (rate limit, quarantine, command)",
    "ident.bad_id": "OCPP path is not a charger hier-id",
    "ident.unknown": "charger id not in the registry",
    "ident.quarantined": "quarantined charger tried to connect",
    "ident.no_cert": "no verified client certificate in a TLS city",
    "ident.cert_mismatch": "certificate CN differs from the claimed charger id",
    "ident.cert_serial_mismatch": "certificate serial differs from the registry",
    "ident.city_mismatch": "charger id of city A arrived through the edge of city B",
    "ident.ip_mismatch": "charger connected from an IP that is not its station's",
    "ident.untrusted_peer": "direct connection from a non-edge address",
    "ident.spoofed_header": "identity headers sent by a non-edge peer",
    "charger.duplicate_id": "a second live connection claims an id already connected",
    "charger.boot": "BootNotification accepted",
    "charger.boot_mismatch": "BootNotification serial differs from the registry",
    "charger.status": "connector status changed",
    "charger.fault": "connector reported Faulted",
    "ocpp.invalid": "malformed, unsupported or out-of-order OCPP message",
    "ocpp.rate_limited": "charger exceeded the per-connection message rate",
    # csms: cards, sessions, meters, commands
    "auth.accept": "card authorised",
    "auth.reject": "card refused (Blocked, Invalid, Expired, ConcurrentTx)",
    "session.start": "transaction started",
    "session.reject": "StartTransaction refused for the card",
    "session.stop": "transaction stopped",
    "meter.anomaly": "meter reading inconsistent with physics or history",
    "command.sent": "command sent to a charger",
    "command.result": "charger answered a command (or it timed out)",
    # registry
    "registry.write": "element created, updated or deleted",
    "registry.write_denied": "token tried to write outside its delegated subtree",
    "registry.quarantine": "element quarantined",
    "registry.release": "element released from quarantine",
    # billing
    "billing.session_closed": "session priced and closed",
    "billing.session_flagged": "session flagged for review",
    "billing.card_blocked": "card blocked",
    "billing.card_unblocked": "card unblocked",
    # fraud (finalised in Phase 7)
    "fraud.alert": "fraud rule fired",
    "fraud.action": "fraud service requested a response",
    # correlator (finalised in Phase 9)
    "incident.open": "new incident",
    "incident.update": "incident gained signals or was located/mitigated",
    "incident.close": "incident closed",
    # edge gateway (written by nginx with the same base fields, not by this module)
    "edge.access": "one request through the city edge (a WebSocket is logged when it closes)",
    # station host watcher and active responses (section 15)
    "host.new_outbound": "station opened an outbound connection to a destination not allowed",
    "ar.action": "an active response acted, was refused by a guard, or failed",
    "ar.evidence": "an active response's evidence snapshot taken before it acted",
    # simulator (the station controller's own log)
    "sim.start": "station simulator started",
    "sim.connect": "charger connected and booted",
    "sim.disconnect": "charger connection lost",
    "sim.rejected": "CSMS refused the WebSocket handshake",
    "sim.command": "charger received a command from the CSMS",
    "sim.auth_rejected": "card refused at the charger",
    "sim.session_start": "charging session started",
    "sim.session_stop": "charging session stopped",
}

_extra_events: set[str] = set()


def register_events(names: Iterable[str]) -> None:
    """Allow events not in the shared vocabulary. Every use must be reported
    as a contract deviation so the vocabulary can be updated."""
    for name in names:
        if not re.match(r"^[a-z]+(\.[a-z_]+)+$", name):
            raise ValueError(f"bad event name: {name!r}")
        _extra_events.add(name)


def is_known_event(name: str) -> bool:
    return name in EVENTS or name in _extra_events


def _iso(ts: float) -> str:
    d = _dt.datetime.fromtimestamp(ts, tz=_dt.UTC)
    return d.strftime("%Y-%m-%dT%H:%M:%S.") + f"{d.microsecond // 1000:03d}Z"


def _coerce(value):
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, enum.Enum):
        return value.value
    if isinstance(value, _dt.datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=_dt.UTC)
        return _iso(value.timestamp())
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_coerce(v) for v in value]
    return str(value)


def build_line(service: str, instance: str, event: str, msg: str, level: str,
               fields: dict, created: float | None = None) -> dict:
    """The dict written for one event (exposed for tests and other writers)."""
    line = {
        "ts": _iso(created if created is not None else _dt.datetime.now(_dt.UTC).timestamp()),
        "level": level,
        "service": service,
        "instance": instance,
        "event": event,
        "msg": msg,
    }
    hid = fields.get("hier_id")
    if isinstance(hid, str):
        try:
            for k, v in hierid.parse(hid).labels().items():
                fields.setdefault(k, v)
        except ValueError:
            pass
    for key, val in fields.items():
        if key in BASE_FIELDS:
            raise ValueError(f"field {key!r} is reserved")
        if not _FIELD_RE.match(key):
            raise ValueError(f"bad field name {key!r}: use snake_case")
        val = _coerce(val)
        if val is not None:
            line[key] = val
    return line


class JsonFormatter(logging.Formatter):
    def __init__(self, service: str, instance: str):
        super().__init__()
        self.service = service
        self.instance = instance

    def format(self, record: logging.LogRecord) -> str:
        level = record.levelname.lower()
        ev = getattr(record, "ev", None)
        if ev is not None:
            line = build_line(self.service, self.instance, ev["event"], record.getMessage(),
                              level, dict(ev["fields"]), record.created)
        else:
            fields = {"logger": record.name}
            if record.exc_info:
                fields["exc"] = self.formatException(record.exc_info)[-2000:]
            line = build_line(self.service, self.instance, "service.log", record.getMessage(),
                              level, fields, record.created)
        return json.dumps(line, separators=(",", ":"), ensure_ascii=False)


class EventLogger:
    """Thin wrapper that only emits events from the vocabulary."""

    def __init__(self, logger: logging.Logger, service: str, instance: str, strict: bool = True):
        self._logger = logger
        self.service = service
        self.instance = instance
        self.strict = strict

    def event(self, event: str, msg: str = "", *, level: str = "info", **fields) -> None:
        if self.strict and not is_known_event(event):
            raise ValueError(f"unknown log event {event!r}; add it to the contract")
        lvl = LEVELS.get(level)
        if lvl is None:
            raise ValueError(f"bad level {level!r}")
        # validate eagerly so mistakes surface in unit tests, not in production logs
        build_line(self.service, self.instance, event, msg, level, dict(fields), 0.0)
        self._logger.log(lvl, msg, extra={"ev": {"event": event, "fields": fields}})

    def info(self, event: str, msg: str = "", **fields) -> None:
        self.event(event, msg, level="info", **fields)

    def warning(self, event: str, msg: str = "", **fields) -> None:
        self.event(event, msg, level="warning", **fields)

    def error(self, event: str, msg: str = "", **fields) -> None:
        self.event(event, msg, level="error", **fields)

    @property
    def logger(self) -> logging.Logger:
        return self._logger


_current: EventLogger | None = None


def log_path(service: str, instance: str, log_dir: Path) -> Path:
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", instance)
    return log_dir / f"{service}-{safe}.json"


def setup_logging(service: str, instance: str | None = None, log_dir: str | Path | None = None,
                  level: str | None = None, stdout: bool | None = None,
                  to_file: bool = True) -> EventLogger:
    """Configure the process-wide event logger. Call once at startup.

    Defaults come from LOG_DIR, LOG_LEVEL, LOG_STDOUT, LOG_MAX_BYTES and INSTANCE_ID.
    """
    global _current
    instance = instance or config.instance_id()
    level = (level or config.get_str("LOG_LEVEL", "info")).lower()
    if level not in LEVELS:
        raise config.ConfigError(f"LOG_LEVEL must be one of {sorted(LEVELS)}")
    stdout = config.get_bool("LOG_STDOUT", True) if stdout is None else stdout
    fmt = JsonFormatter(service, instance)

    handlers: list[logging.Handler] = []
    if to_file:
        directory = Path(log_dir) if log_dir is not None else config.log_dir()
        directory.mkdir(parents=True, exist_ok=True)
        fh = logging.handlers.RotatingFileHandler(
            log_path(service, instance, directory),
            maxBytes=config.get_int("LOG_MAX_BYTES", 50 * 1024 * 1024),
            backupCount=3, encoding="utf-8")
        handlers.append(fh)
    if stdout:
        handlers.append(logging.StreamHandler(sys.stdout))
    for h in handlers:
        h.setFormatter(fmt)

    ours = logging.getLogger(_LOGGER_NAME)
    for h in list(ours.handlers):
        ours.removeHandler(h)
        h.close()
    ours.setLevel(LEVELS[level])
    ours.propagate = False
    for h in handlers:
        ours.addHandler(h)

    # library loggers (uvicorn, ocpp, websockets, httpx): warnings and errors only
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
    root.setLevel(logging.WARNING)
    for h in handlers:
        root.addHandler(h)
    for noisy in ("ocpp", "websockets", "httpx", "httpcore", "uvicorn.access"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    _current = EventLogger(ours, service, instance)
    return _current


def get_logger() -> EventLogger:
    """The logger set up by setup_logging(), or a stdout-only fallback."""
    global _current
    if _current is None:
        _current = setup_logging("unconfigured", instance="local", to_file=False)
    return _current
