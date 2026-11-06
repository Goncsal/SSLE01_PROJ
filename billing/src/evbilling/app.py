"""The billing HTTP API (docs/interfaces.md, section 4.3)."""

from __future__ import annotations

import datetime as dt
import hmac

from evcommon import hierid, jsonlog, metrics
from evcommon.models import (
    CARD_REGEX, AuthorizeRequest, AuthorizeResponse, Card, CardBlockRequest, CardIn, Health,
    Session, SessionFlag, SessionList, SessionStart, SessionStop, Tariff,
)
from fastapi import APIRouter, Depends, FastAPI, HTTPException, Path, Query, Request, Response
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from . import __version__, service
from .db import BlockedRow, CardRow, TariffRow, TransactionRow, init_db
from .settings import Settings

AUTHORIZE = metrics.make(metrics.BILLING_AUTHORIZE_TOTAL)
SESSIONS = metrics.make(metrics.BILLING_SESSIONS_TOTAL)
ENERGY = metrics.make(metrics.BILLING_ENERGY_WH_TOTAL)
REVENUE = metrics.make(metrics.BILLING_REVENUE_EUR_TOTAL)
BLOCKED_CARDS = metrics.make(metrics.BILLING_BLOCKED_CARDS)
CARD_BLOCKS = metrics.make(metrics.BILLING_CARD_BLOCKS_TOTAL)

CardPath = Path(pattern=CARD_REGEX)


def aware(value: dt.datetime) -> dt.datetime:
    """Treat a timestamp without a zone as UTC."""
    return value if value.tzinfo else value.replace(tzinfo=dt.UTC)


def create_app(settings: Settings) -> FastAPI:
    app = FastAPI(title="EV billing", version=__version__)
    app.state.settings = settings
    app.state.sessions = init_db(settings.database_url)
    log = jsonlog.get_logger
    token = settings.api_token.encode()
    responder = settings.responder_token.encode()

    with app.state.sessions() as db:
        service.seed(db, settings.seed_roster)
        BLOCKED_CARDS.set(service.blocked_count(db))

    # ---- dependencies ------------------------------------------------------
    def get_db():
        with app.state.sessions() as db:
            yield db

    def require_token(request: Request) -> None:
        """401 unless the bearer token matches (constant-time comparison)."""
        scheme, _, given = (request.headers.get("authorization") or "").partition(" ")
        supplied = given.strip().encode()
        is_api = hmac.compare_digest(supplied, token)
        is_responder = bool(responder) and hmac.compare_digest(supplied, responder)   # no early exit
        if scheme.lower() == "bearer" and given and (is_api or is_responder):
            request.state.operator = "api" if is_api else "responder"
            return
        log().event("api.auth_failed", "missing or invalid bearer token", level="warning",
                    srcip=request.client.host if request.client else "unknown",
                    path=request.url.path, method=request.method)
        raise HTTPException(401, "missing or invalid token", headers={"WWW-Authenticate": "Bearer"})

    def scope(request: Request, allowed: bool = False) -> None:
        """403 for the responder token on anything but its isolate-only calls. Called after
        the path and body are validated (422 before 403, section 4.0)."""
        if request.state.operator == "responder" and not allowed:
            log().event("api.auth_failed", "responder token out of scope", level="warning",
                        reason="scope", operator="responder",
                        srcip=request.client.host if request.client else "unknown",
                        path=request.url.path, method=request.method)
            raise HTTPException(403, "responder token may only isolate")

    api = APIRouter(dependencies=[Depends(require_token)])

    def get_transaction(db, transaction_id: int) -> TransactionRow:
        row = db.get(TransactionRow, transaction_id)
        if row is None:
            raise HTTPException(404, f"transaction {transaction_id} not found")
        return row

    def get_card(db, card: str) -> CardRow:
        row = db.get(CardRow, card)
        if row is None:
            raise HTTPException(404, f"card {card} not found")
        return row

    def flag(db, row: TransactionRow, reason: str, source: str, incident_id: str | None = None) -> None:
        """Mark a session as flagged (the caller commits)."""
        row.status, row.flag_reason = "flagged", reason
        SESSIONS.labels(city=row.city, status="flagged").inc()
        log().event("billing.session_flagged", f"session {row.transaction_id} flagged", level="warning",
                    transaction_id=row.transaction_id, card=row.card, hier_id=row.hier_id,
                    reason=reason, source=source, incident_id=incident_id)

    # ---- health and metrics ------------------------------------------------
    @app.get("/health", response_model=Health)
    def health(db=Depends(get_db)):
        try:
            db.execute(text("SELECT 1"))
            check = "ok"
        except Exception:
            check = "error"
        return Health(status="ok" if check == "ok" else "degraded", service="billing",
                      version=__version__, checks={"db": check})

    @app.get("/metrics")
    def prometheus_metrics():
        body, content_type = metrics.render()
        return Response(body, media_type=content_type)

    # ---- authorisation -----------------------------------------------------
    @api.post("/authorize", response_model=AuthorizeResponse)
    def authorize(body: AuthorizeRequest, request: Request, db=Depends(get_db)):
        scope(request)
        row = db.get(CardRow, body.card)
        if row is None:
            status = "Invalid"
        elif row.status == "blocked":
            status = "Blocked"
        elif row.status == "expired" or (row.expiry_date and row.expiry_date <= service.now()):
            status = "Expired"
        else:
            status = "Accepted"
        AUTHORIZE.labels(result=status).inc()
        return AuthorizeResponse(card=body.card, status=status, expiry_date=row.expiry_date if row else None)

    # ---- sessions ----------------------------------------------------------
    @api.post("/sessions", response_model=Session)
    def start_session(body: SessionStart, request: Request, response: Response, db=Depends(get_db)):
        scope(request)
        def existing() -> TransactionRow | None:
            row = db.get(TransactionRow, body.transaction_id)
            if row and (row.card != body.card or row.hier_id != body.hier_id):
                raise HTTPException(409, f"transaction {body.transaction_id} belongs to another card or charger")
            return row

        row = existing()
        if row is None:
            card = db.get(CardRow, body.card)   # unknown card: the CSMS decided, price as standard
            row = TransactionRow(
                transaction_id=body.transaction_id, card=body.card, hier_id=body.hier_id,
                city=hierid.parse(body.hier_id).city, connector_id=body.connector_id,
                tariff_id=card.tariff_id if card else service.DEFAULT_TARIFF,
                started_at=aware(body.started_at), meter_start_wh=body.meter_start_wh, status="open")
            db.add(row)
            try:
                db.commit()
                response.status_code = 201
                SESSIONS.labels(city=row.city, status="opened").inc()
            except IntegrityError:      # a concurrent request with the same id won
                db.rollback()
                row = existing()
        return service.to_session(row)

    @api.post("/sessions/{transaction_id}/stop", response_model=Session)
    def stop_session(transaction_id: int, body: SessionStop, request: Request, db=Depends(get_db)):
        scope(request)
        row = get_transaction(db, transaction_id)
        if row.stopped_at is not None:
            return service.to_session(row)          # already closed: idempotent
        energy = body.meter_stop_wh - row.meter_start_wh
        backwards = energy < 0
        row.stopped_at, row.meter_stop_wh = aware(body.stopped_at), body.meter_stop_wh
        row.energy_wh, row.stop_reason = max(energy, 0), body.reason
        row.cost_eur = service.price(db.get(TariffRow, row.tariff_id), row.energy_wh,
                                     row.started_at, row.stopped_at)
        if row.status == "open":
            row.status = "closed"
            SESSIONS.labels(city=row.city, status="closed").inc()
        if backwards and row.status != "flagged":
            flag(db, row, "meter_backwards", "billing")
        db.commit()
        ENERGY.labels(city=row.city).inc(row.energy_wh)
        REVENUE.labels(city=row.city).inc(float(row.cost_eur))
        if not backwards:
            log().event("billing.session_closed", f"session {transaction_id} closed",
                        transaction_id=transaction_id, card=row.card, hier_id=row.hier_id,
                        energy_wh=row.energy_wh, cost_eur=float(row.cost_eur))
        return service.to_session(row)

    @api.post("/sessions/{transaction_id}/flag", response_model=Session)
    def flag_session(transaction_id: int, body: SessionFlag, request: Request, db=Depends(get_db)):
        scope(request, allowed=True)
        row = get_transaction(db, transaction_id)
        if row.status != "flagged":                 # idempotent
            flag(db, row, body.reason, body.source, body.incident_id)
            db.commit()
        return service.to_session(row)

    @api.get("/sessions", response_model=SessionList)
    def list_sessions(request: Request, card: str | None = Query(None, pattern=CARD_REGEX), hier_id: str | None = None,
                      status: str | None = Query(None, pattern="^(open|closed|flagged)$"),
                      since: dt.datetime | None = None, limit: int = Query(100, ge=1, le=1000),
                      db=Depends(get_db)):
        scope(request, allowed=True)
        stmt = select(TransactionRow).order_by(TransactionRow.started_at.desc(),
                                               TransactionRow.transaction_id.desc()).limit(limit)
        if card:
            stmt = stmt.where(TransactionRow.card == card)
        if hier_id:
            if not hierid.is_prefix(hier_id):
                raise HTTPException(422, "not a hier-id prefix")
            stmt = stmt.where((TransactionRow.hier_id == hier_id) | TransactionRow.hier_id.startswith(hier_id + "."))
        if status:
            stmt = stmt.where(TransactionRow.status == status)
        if since:
            stmt = stmt.where(TransactionRow.started_at >= aware(since))
        items = [service.to_session(r) for r in db.scalars(stmt)]
        return SessionList(count=len(items), items=items)

    # ---- cards and tariffs -------------------------------------------------
    @api.get("/cards/{card}", response_model=Card)
    def read_card(request: Request, card: str = CardPath, db=Depends(get_db)):
        scope(request, allowed=True)
        return service.to_card(db, get_card(db, card))

    @api.post("/cards", response_model=Card, status_code=201)
    def create_card(body: CardIn, request: Request, db=Depends(get_db)):
        scope(request)
        if db.get(TariffRow, body.tariff_id) is None:
            raise HTTPException(422, f"unknown tariff {body.tariff_id!r}")
        if db.get(CardRow, body.card) is not None:
            raise HTTPException(409, f"card {body.card} already exists")
        row = CardRow(card=body.card, holder=body.holder, home_city=body.home_city,
                      tariff_id=body.tariff_id, status="active", expiry_date=body.expiry_date,
                      created_at=service.now())
        db.add(row)
        db.commit()
        return service.to_card(db, row)

    @api.post("/cards/{card}/block", response_model=Card)
    def block_card(body: CardBlockRequest, request: Request, card: str = CardPath, db=Depends(get_db)):
        scope(request, allowed=True)
        row = get_card(db, card)
        if row.status != "blocked":                 # idempotent
            row.status = "blocked"
            db.add(BlockedRow(card=card, reason=body.reason, source=body.source,
                              incident_id=body.incident_id, blocked_at=service.now()))
            db.commit()
            CARD_BLOCKS.labels(source=body.source).inc()
            BLOCKED_CARDS.set(service.blocked_count(db))
            log().event("billing.card_blocked", f"card {card} blocked", level="warning", card=card,
                        reason=body.reason, source=body.source, incident_id=body.incident_id)
        return service.to_card(db, row)

    @api.post("/cards/{card}/unblock", response_model=Card)
    def unblock_card(body: CardBlockRequest, request: Request, card: str = CardPath, db=Depends(get_db)):
        scope(request)
        row = get_card(db, card)
        if row.status == "blocked":
            row.status = "active"
            for block in db.scalars(select(BlockedRow).where(BlockedRow.card == card,
                                                             BlockedRow.unblocked_at.is_(None))):
                block.unblocked_at, block.unblocked_by = service.now(), body.source
            db.commit()
            BLOCKED_CARDS.set(service.blocked_count(db))
            log().event("billing.card_unblocked", f"card {card} unblocked", card=card,
                        reason=body.reason, source=body.source, incident_id=body.incident_id)
        return service.to_card(db, row)

    @api.get("/tariffs", response_model=list[Tariff])
    def list_tariffs(request: Request, db=Depends(get_db)):
        scope(request)
        return [service.to_tariff(t) for t in db.scalars(select(TariffRow).order_by(TariffRow.tariff_id))]

    app.include_router(api)
    return app
