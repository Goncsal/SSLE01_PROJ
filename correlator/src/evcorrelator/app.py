"""The correlator HTTP API (docs/interfaces.md, section 4.5)."""

from __future__ import annotations

import hmac
import json
import re

from evcommon import jsonlog, metrics
from evcommon.models import Health
from fastapi import FastAPI, HTTPException, Request, Response
from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool

from . import __version__, locate, normalise
from .engine import Clock, Engine
from .models import CloseRequest, FraudAlert, Incident, MitigatedRequest, Signal
from .settings import Settings
from .timeutil import parse_ts, utcnow

MAX_BODY = 1_000_000
STATUSES = ("open", "mitigated", "closed")
_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def create_app(settings: Settings, locator: locate.Locator | None = None, clock: Clock = utcnow) -> FastAPI:
    app = FastAPI(title="EV correlator", version=__version__)
    app.state.settings = settings
    history = _open_history(settings.database_url)
    locator = locator or locate.Locator(settings.registry_url)
    engine = Engine(settings.join_tolerance_s, settings.close_s, clock=clock,
                    on_close=history.save if history else None)
    app.state.engine = engine
    token = settings.api_token.encode()

    # ---- auth (401 first, then body validation) ------------------------------------
    def authenticate(request: Request) -> None:
        scheme, _, given = (request.headers.get("authorization") or "").partition(" ")
        if scheme.lower() == "bearer" and given and hmac.compare_digest(given.strip().encode(), token):
            return
        jsonlog.get_logger().event(
            "api.auth_failed", "missing or invalid bearer token", level="warning",
            srcip=request.client.host if request.client else "unknown",
            path=request.url.path, method=request.method)
        raise HTTPException(401, "missing or invalid token", headers={"WWW-Authenticate": "Bearer"})

    async def json_body(request: Request):
        raw = await request.body()
        if len(raw) > MAX_BODY:
            raise HTTPException(413, "body too large")
        try:
            return json.loads(raw)
        except ValueError as exc:
            raise HTTPException(422, "body is not valid JSON") from exc

    def incident_id(value: str) -> str:
        if not _UUID_RE.match(value):
            raise HTTPException(422, "incident_id must be a uuid")
        return value

    # ---- ingestion -------------------------------------------------------------------
    def ingest(signals: list[Signal]) -> int:
        """Join each signal, then walk the registry for every incident that gained evidence."""
        to_locate: dict[str, None] = {}
        for signal in signals:
            result = engine.ingest(signal)
            if result.incident_id and result.changed:
                to_locate[result.incident_id] = None
        for iid in to_locate:
            current = engine.signals_of(iid)
            if current:
                root_cause, located = locate.root_cause(current, locate.walk(current, locator))
                engine.set_root_cause(iid, root_cause, located)
        return len(signals)

    @app.get("/health", response_model=Health)
    def health():
        return Health(status="ok", service="correlator", version=__version__,
                      checks={"history": "ok" if history else "off"})

    @app.get("/metrics")
    def prometheus_metrics():
        body, content_type = metrics.render()
        return Response(body, media_type=content_type)

    @app.post("/signals/alertmanager", status_code=202)
    async def signals_alertmanager(request: Request):
        authenticate(request)
        payload = await json_body(request)
        signals = normalise.from_alertmanager(payload, clock())
        return {"accepted": await run_in_threadpool(ingest, signals)}

    @app.post("/signals/wazuh", status_code=202)
    async def signals_wazuh(request: Request):
        authenticate(request)
        signal = normalise.from_wazuh(await json_body(request), clock())
        return {"accepted": await run_in_threadpool(ingest, [signal]) if signal else 0}

    @app.post("/signals/fraud", status_code=202)
    async def signals_fraud(request: Request):
        authenticate(request)
        body = await json_body(request)
        try:
            alert = FraudAlert.model_validate(body)
        except ValidationError as exc:
            raise HTTPException(422, "not a FraudAlert: " + str(exc.errors()[0]["loc"])) from exc
        signal = normalise.from_fraud(alert)
        return {"accepted": await run_in_threadpool(ingest, [signal]) if signal else 0}

    # ---- incidents ---------------------------------------------------------------------
    @app.get("/incidents")
    def list_incidents(request: Request, status: str | None = None, since: str | None = None, limit: int = 100):
        authenticate(request)
        if status is not None and status not in STATUSES:
            raise HTTPException(422, f"status must be one of {', '.join(STATUSES)}")
        since_ts = None
        if since is not None:
            since_ts = parse_ts(since)
            if since_ts is None:
                raise HTTPException(422, "since must be an ISO 8601 time")
        if not 1 <= limit <= 500:
            raise HTTPException(422, "limit must be 1..500")
        items = engine.list(status, since_ts, limit)
        if history and status in (None, "closed") and len(items) < limit:
            known = {i.incident_id for i in items}
            items += [i for i in history.list(since_ts, limit) if i.incident_id not in known][: limit - len(items)]
        return {"count": len(items), "items": items}

    def find(iid: str) -> Incident:
        found = engine.get(iid) or (history.get(iid) if history else None)
        if found is None:
            raise HTTPException(404, "incident not found")
        return found

    @app.get("/incidents/{iid}")
    def get_incident(iid: str, request: Request):
        authenticate(request)
        return find(incident_id(iid))

    @app.post("/incidents/{iid}/mitigated")
    async def mitigated(iid: str, request: Request):
        authenticate(request)
        iid = incident_id(iid)
        try:
            body = MitigatedRequest.model_validate(await json_body(request))
        except ValidationError as exc:
            raise HTTPException(422, "invalid body") from exc
        if find(iid).status == "closed":
            raise HTTPException(409, "incident is closed")
        return engine.mitigated(iid, body.action, body.ts)

    @app.post("/incidents/{iid}/close")
    async def close(iid: str, request: Request):
        authenticate(request)
        iid = incident_id(iid)
        try:
            body = CloseRequest.model_validate(await json_body(request))
        except ValidationError as exc:
            raise HTTPException(422, "invalid body") from exc
        find(iid)
        return engine.close(iid, body.by, body.note)

    return app


def _open_history(url: str):
    if not url:
        return None
    try:
        from .history import History
        return History(url)
    except ImportError:
        jsonlog.get_logger().event("dependency.error", "DATABASE_URL set but sqlalchemy is not installed; "
                                   "closed-incident history disabled", level="error", dependency="database")
        return None
