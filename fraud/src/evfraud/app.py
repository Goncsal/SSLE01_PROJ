"""The fraud HTTP API (docs/interfaces.md, section 4.4)."""

from __future__ import annotations

import datetime as dt
import hmac
from contextlib import asynccontextmanager

from evcommon import jsonlog, metrics
from evcommon.models import CARD_REGEX, FraudEvent, Health, HierIdStr
from fastapi import APIRouter, BackgroundTasks, Depends, FastAPI, Path, Query, Request, Response
from fastapi import HTTPException
from sqlalchemy import text

from . import __version__
from .actions import Geo, Responder
from .db import init_db
from .grid import GridPoller
from .scorer import Raised, Scorer
from .settings import Settings


def create_app(settings: Settings, *, geo=None, responder: Responder | None = None) -> FastAPI:
    """`geo` and `responder` can be replaced (tests); by default they talk to the peers in
    the settings."""
    sessions = init_db(settings.database_url)
    geo = geo or Geo(settings)
    responder = responder or Responder(settings)
    scorer = Scorer(sessions, settings, geo)
    poller: GridPoller | None = None

    def act(raised: list[Raised]) -> None:
        """Run the response of each new alert; then tell the correlator about criticals."""
        for r in raised:
            txs = (scorer.card_transactions(r.finding.card, r.alert.ts)
                   if r.finding.action == "block_card" and r.finding.card else None)
            responder.run(r.alert, r.finding, txs)
            responder.notify_correlator(r.alert)

    @asynccontextmanager
    async def lifespan(_app):
        nonlocal poller
        if settings.prometheus_url:
            poller = GridPoller(scorer, settings, act)
            poller.start()
        yield
        if poller:
            poller.stop()

    app = FastAPI(title="EV fraud", version=__version__, lifespan=lifespan)
    app.state.settings, app.state.scorer, app.state.act = settings, scorer, act
    log = jsonlog.get_logger
    token = settings.api_token.encode()

    def get_db():
        with sessions() as db:
            yield db

    def require_token(request: Request) -> None:
        """401 unless the bearer token matches (constant-time comparison)."""
        scheme, _, given = (request.headers.get("authorization") or "").partition(" ")
        if scheme.lower() == "bearer" and given and hmac.compare_digest(given.strip().encode(), token):
            return
        log().event("api.auth_failed", "missing or invalid bearer token", level="warning",
                    srcip=request.client.host if request.client else "unknown",
                    path=request.url.path, method=request.method)
        raise HTTPException(401, "missing or invalid token", headers={"WWW-Authenticate": "Bearer"})

    api = APIRouter(dependencies=[Depends(require_token)])

    @app.get("/health", response_model=Health)
    def health(db=Depends(get_db)):
        try:
            db.execute(text("SELECT 1"))
            check = "ok"
        except Exception:
            check = "error"
        return Health(status="ok" if check == "ok" else "degraded", service="fraud",
                      version=__version__, checks={"db": check, "mode": "dry_run" if settings.dry_run else "live"})

    @app.get("/metrics")
    def prometheus_metrics():
        body, content_type = metrics.render()
        return Response(body, media_type=content_type)

    @api.post("/events", status_code=202)
    def post_events(event: FraudEvent, background: BackgroundTasks):
        raised = scorer.ingest(event)
        if raised:
            background.add_task(act, raised)       # after the 202 is sent: a slow peer never blocks the CSMS
        return {"accepted": True}

    @api.get("/alerts")
    def get_alerts(since: dt.datetime | None = None, card: str | None = Query(None, pattern=CARD_REGEX),
                   hier_id: str | None = None, limit: int = Query(100, ge=1, le=1000)):
        items = scorer.list_alerts(since, card, hier_id, limit)
        return {"count": len(items), "items": [a.model_dump(mode="json") for a in items]}

    @api.get("/scores/cards/{card}")
    def get_card_score(card: str = Path(pattern=CARD_REGEX)):
        out = scorer.card_score(card)
        out["last_seen"] = out["last_seen"].isoformat().replace("+00:00", "Z") if out["last_seen"] else None
        return out

    @api.get("/scores/chargers/{hier_id}")
    def get_charger_score(hier_id: HierIdStr):
        return scorer.charger_score(hier_id)

    app.include_router(api)
    return app
