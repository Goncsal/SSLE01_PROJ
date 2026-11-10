"""Responses and peers: billing, registry and CSMS with isolate-only tokens (section 4.4).

    block_card   billing POST /cards/{card}/block, then POST /sessions/{id}/flag for the
                 card's recent sessions
    flag_session billing POST /sessions/{id}/flag
    quarantine   registry POST /quarantine/{id}, then for each connected charger below it
                 CSMS change-availability Inoperative and disconnect, then billing
                 POST /sessions/{id}/flag for the station's recent sessions (review, not reversal)

With FRAUD_DRY_RUN the intended action is logged (`fraud.action`, result=dry_run) and no
API is called, the correlator included. The registry client also serves the station
coordinates for impossible_travel (cached; registry reads need no token).
"""

from __future__ import annotations

import collections
import threading
import time

import httpx
from evcommon import jsonlog, metrics

from .rules import Finding
from .settings import Settings

ACTIONS = metrics.make(metrics.FRAUD_ACTIONS_TOTAL)
METRIC_ACTION = {"block_card": "block_card", "flag_session": "flag_session", "quarantine": "quarantine"}
GEO_RETRY_S = 60.0
REASON_MAX = 200
MAX_FLAGS = 200                # sessions flagged by one quarantine


def _reason(rule: str, detail: str) -> str:
    return f"fraud rule {rule}: {detail}"[:REASON_MAX]


class Geo:
    """Station coordinates from the registry, cached (a miss is retried after a minute)."""

    def __init__(self, settings: Settings, client: httpx.Client | None = None):
        self.cfg = settings
        self._client = client or (httpx.Client(base_url=settings.registry_url.rstrip("/"),
                                               timeout=settings.http_timeout_s)
                                  if settings.registry_url else None)
        self._cache: dict[str, tuple[float, float] | None] = {}
        self._failed_at: dict[str, float] = {}
        self._lock = threading.Lock()

    def __call__(self, station_id: str) -> tuple[float, float] | None:
        if self._client is None:
            return None
        with self._lock:
            if station_id in self._cache:
                return self._cache[station_id]
            if time.monotonic() - self._failed_at.get(station_id, -1e9) < GEO_RETRY_S:
                return None
        coords = self._fetch(station_id) or self._fetch(".".join(station_id.split(".")[:2]))
        with self._lock:
            if coords:
                self._cache[station_id] = coords
            else:
                self._failed_at[station_id] = time.monotonic()
        return coords

    def _fetch(self, hier_id: str) -> tuple[float, float] | None:
        try:
            r = self._client.get(f"/nodes/{hier_id}")
            if r.status_code == 404:
                return None
            r.raise_for_status()
            body = r.json()
        except (httpx.HTTPError, ValueError) as exc:
            jsonlog.get_logger().event("dependency.error", "registry lookup failed", level="warning",
                                       dependency="registry", reason=type(exc).__name__)
            return None
        if body.get("lat") is None or body.get("lon") is None:
            return None
        return float(body["lat"]), float(body["lon"])


class Responder:
    def __init__(self, settings: Settings, transport: httpx.BaseTransport | None = None):
        self.cfg = settings
        self._transport = transport
        self._times: collections.deque[float] = collections.deque()
        self._lock = threading.Lock()

    # ---- helpers ---------------------------------------------------------------------------------

    def _client(self, url: str, token: str) -> httpx.Client:
        return httpx.Client(base_url=url.rstrip("/"), timeout=self.cfg.http_timeout_s,
                            headers={"Authorization": f"Bearer {token}"}, transport=self._transport)

    def _allow(self) -> bool:
        """At most FRAUD_MAX_ACTIONS_H automatic block/quarantine actions per hour."""
        now = time.monotonic()
        with self._lock:
            while self._times and now - self._times[0] > 3600:
                self._times.popleft()
            if len(self._times) >= self.cfg.max_actions_h:
                return False
            self._times.append(now)
            return True

    def _log(self, finding: Finding, action: str, target: str, result: str, **extra) -> str:
        jsonlog.get_logger().event(
            "fraud.action", f"{action} {target}: {result}", level="warning", rule=finding.rule,
            target=target, result=result, action=action, **extra)
        return result

    # ---- entry point -----------------------------------------------------------------------------

    def run(self, alert, finding: Finding, transactions: list[int] | None = None) -> str | None:
        """Carry out (or, in dry run, announce) the action a finding asks for."""
        if finding.action is None:
            return None
        target = {"block_card": finding.card, "flag_session": str(finding.transaction_id),
                  "quarantine": finding.hier_id}[finding.action]
        ACTIONS.labels(action=METRIC_ACTION[finding.action]).inc()
        if self.cfg.dry_run:
            return self._log(finding, finding.action, target, "dry_run")
        if finding.action != "flag_session" and not self._allow():
            return self._log(finding, finding.action, target, "rate_limited")
        try:
            if finding.action == "block_card":
                return self._block_card(finding, transactions or [])
            if finding.action == "flag_session":
                return self._flag_session(finding)
            return self._quarantine(finding, transactions or [])
        except httpx.HTTPError as exc:
            jsonlog.get_logger().event("dependency.error", "response call failed", level="warning",
                                       dependency=finding.action, reason=type(exc).__name__)
            return self._log(finding, finding.action, target, "error")

    # ---- actions ---------------------------------------------------------------------------------

    def _meta(self, finding: Finding) -> dict:
        return {"reason": _reason(finding.rule, finding.detail), "source": "fraud"}

    def _block_card(self, finding: Finding, transactions: list[int]) -> str:
        if not (self.cfg.billing_url and self.cfg.billing_responder_token):
            return self._log(finding, "block_card", finding.card, "not_configured")
        with self._client(self.cfg.billing_url, self.cfg.billing_responder_token) as c:
            r = c.post(f"/cards/{finding.card}/block", json=self._meta(finding))
            if r.status_code >= 400:
                return self._log(finding, "block_card", finding.card, "error", status=r.status_code)
            self._log(finding, "block_card", finding.card, "ok")
            for tid in transactions:
                fr = c.post(f"/sessions/{tid}/flag", json=self._meta(finding))
                self._log(finding, "flag_session", str(tid), "ok" if fr.status_code < 400 else "error")
        return "ok"

    def _flag_session(self, finding: Finding) -> str:
        target = str(finding.transaction_id)
        if finding.transaction_id is None:
            return self._log(finding, "flag_session", target, "skipped")
        if not (self.cfg.billing_url and self.cfg.billing_responder_token):
            return self._log(finding, "flag_session", target, "not_configured")
        with self._client(self.cfg.billing_url, self.cfg.billing_responder_token) as c:
            r = c.post(f"/sessions/{finding.transaction_id}/flag", json=self._meta(finding))
        # a 404 means billing never saw the session (it was not created or was lost)
        return self._log(finding, "flag_session", target, "ok" if r.status_code < 400 else "error",
                         status=r.status_code)

    def _quarantine(self, finding: Finding, transactions: list[int]) -> str:
        target = finding.hier_id
        if not (self.cfg.registry_url and self.cfg.registry_responder_token):
            return self._log(finding, "quarantine", target, "not_configured")
        meta = self._meta(finding)
        with self._client(self.cfg.registry_url, self.cfg.registry_responder_token) as c:
            r = c.post(f"/quarantine/{target}", json=meta)
        if r.status_code >= 400:
            return self._log(finding, "quarantine", target, "error", status=r.status_code)
        self._log(finding, "quarantine", target, "ok")
        if self.cfg.csms_api_url and self.cfg.csms_responder_token:
            self._isolate_connected(target, meta, finding.rule)
        self._flag_sessions(finding, transactions[:MAX_FLAGS])
        return "ok"

    def _flag_sessions(self, finding: Finding, transactions: list[int]) -> None:
        """Mark the station's recent sessions for billing review (nothing is reversed)."""
        if not transactions or not (self.cfg.billing_url and self.cfg.billing_responder_token):
            return
        try:
            with self._client(self.cfg.billing_url, self.cfg.billing_responder_token) as c:
                for tid in transactions:
                    fr = c.post(f"/sessions/{tid}/flag", json=self._meta(finding))
                    self._log(finding, "flag_session", str(tid), "ok" if fr.status_code < 400 else "error")
        except httpx.HTTPError as exc:
            jsonlog.get_logger().event("dependency.error", "session flag failed", level="warning",
                                       dependency="billing", reason=type(exc).__name__)

    def _isolate_connected(self, prefix: str, meta: dict, rule: str) -> None:
        """change-availability Inoperative, then disconnect, for every connected charger."""
        log = jsonlog.get_logger()
        with self._client(self.cfg.csms_api_url, self.cfg.csms_responder_token) as c:
            try:
                r = c.get("/connections", params={"prefix": prefix})
                r.raise_for_status()
                chargers = [x["hier_id"] for x in r.json().get("items", [])]
            except (httpx.HTTPError, ValueError) as exc:
                log.event("dependency.error", "csms connections lookup failed", level="warning",
                          dependency="csms", reason=type(exc).__name__)
                return
            for hid in chargers:
                for path, body in ((f"/commands/{hid}/change-availability",
                                    {**meta, "connector_id": 0, "type": "Inoperative"}),
                                   (f"/commands/{hid}/disconnect", dict(meta))):
                    try:
                        cr = c.post(path, json=body)
                        res = "ok" if cr.status_code < 400 else "error"
                    except httpx.HTTPError as exc:
                        res = "error"
                        log.event("dependency.error", "csms command failed", level="warning",
                                  dependency="csms", reason=type(exc).__name__)
                    log.event("fraud.action", f"{path.rsplit('/', 1)[1]} {hid}: {res}",
                              level="warning", rule=rule, target=hid, result=res,
                              action=path.rsplit("/", 1)[1])

    def notify_correlator(self, alert) -> None:
        """Post a critical alert to the correlator (best effort; nothing in dry run)."""
        if self.cfg.dry_run or alert.severity != "critical":
            return
        if not (self.cfg.correlator_url and self.cfg.correlator_api_token):
            return
        try:
            with self._client(self.cfg.correlator_url, self.cfg.correlator_api_token) as c:
                c.post("/signals/fraud", json=alert.model_dump(mode="json")).raise_for_status()
        except httpx.HTTPError as exc:
            jsonlog.get_logger().event("dependency.error", "correlator post failed", level="warning",
                                       dependency="correlator", reason=type(exc).__name__)
