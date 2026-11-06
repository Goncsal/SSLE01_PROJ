"""The registry HTTP API (docs/interfaces.md, section 4.1)."""

from __future__ import annotations

import ipaddress
from collections import Counter

from evcommon import hierid, jsonlog, metrics
from evcommon.models import (
    Health, LocateResponse, Node, NodeIn, NodeKind, NodeList, NodePatch, NodeState,
    PromSdTarget, QuarantineRequest, WhoAmI,
)
from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from sqlalchemy import select, text, delete as sql_delete

from . import __version__, service
from .auth import Principal, identify
from .db import NodeRow, QuarantineLogRow, init_db
from .settings import Settings

NODES = metrics.make(metrics.REGISTRY_NODES)
STATION_INFO = metrics.make(metrics.REGISTRY_STATION_INFO)
WRITES = metrics.make(metrics.REGISTRY_WRITES_TOTAL)
QUARANTINES = metrics.make(metrics.REGISTRY_QUARANTINES_TOTAL)

# NodeIn columns copied on PUT (absent = null) and PATCH (only those sent)
FIELDS = tuple(NodeIn.model_fields)


def create_app(settings: Settings) -> FastAPI:
    app = FastAPI(title="EV registry", version=__version__)
    app.state.settings = settings
    app.state.sessions = init_db(settings.database_url)

    # ---- dependencies ------------------------------------------------------
    def get_db():
        with app.state.sessions() as db:
            yield db

    def client_ip(request: Request) -> str:
        return request.client.host if request.client else "unknown"

    def reject_auth(request: Request):
        jsonlog.get_logger().event(
            "api.auth_failed", "missing or invalid bearer token", level="warning",
            srcip=client_ip(request), path=request.url.path, method=request.method)
        raise HTTPException(401, "missing or invalid token", headers={"WWW-Authenticate": "Bearer"})

    def principal(request: Request) -> Principal:
        who = identify(settings, request.headers.get("authorization"))
        if who is None:
            reject_auth(request)
        return who

    def check_scope(request: Request, who: Principal, hier_id: str, isolate: bool = False) -> None:
        """Parse the id (422) and enforce delegation (403). `isolate` = quarantine, which the
        responder token may do on any element."""
        try:
            hierid.parse(hier_id)
        except ValueError as e:
            WRITES.labels(operator=who.operator, result="invalid").inc()
            raise HTTPException(422, str(e)) from e
        if not (who.may_isolate(hier_id) if isolate else who.may_write(hier_id)):
            WRITES.labels(operator=who.operator, result="denied").inc()
            jsonlog.get_logger().event(
                "registry.write_denied", f"{who.operator} may not write {hier_id}", level="warning",
                operator=who.operator, hier_id=hier_id, srcip=client_ip(request))
            raise HTTPException(403, f"token of {who.operator!r} may not write {hier_id}")

    def get_row(db, hier_id: str, who: Principal) -> NodeRow:
        row = db.get(NodeRow, hier_id)
        if row is None:
            WRITES.labels(operator=who.operator, result="not_found").inc()
            raise HTTPException(404, f"{hier_id} not found")
        return row

    def wrote(who: Principal, db) -> None:
        WRITES.labels(operator=who.operator, result="ok").inc()
        refresh_metrics(db)

    def refresh_metrics(db) -> None:
        """Rebuild the gauges from the table (after each write and at startup)."""
        counts = Counter((r.city or "central", r.kind, r.state)
                         for r in db.scalars(select(NodeRow)))
        NODES.clear()
        for (city, kind, state), n in counts.items():
            NODES.labels(city=city, kind=kind, state=state).set(n)
        STATION_INFO.clear()
        for r in db.scalars(select(NodeRow).where(NodeRow.kind == "station")):
            if r.lat is not None and r.lon is not None:
                STATION_INFO.labels(hier_id=r.hier_id, city=r.city, station=r.station,
                                    lat=str(r.lat), lon=str(r.lon)).set(1)

    with app.state.sessions() as db:
        refresh_metrics(db)

    # ---- health and metrics ---------------------------------------------
    @app.get("/health", response_model=Health)
    def health(db=Depends(get_db)):
        try:
            db.execute(text("SELECT 1"))
            check = "ok"
        except Exception:
            check = "error"
        return Health(status="ok" if check == "ok" else "degraded", service="registry",
                      version=__version__, checks={"db": check})

    @app.get("/metrics")
    def prometheus_metrics():
        body, content_type = metrics.render()
        return Response(body, media_type=content_type)

    @app.get("/whoami", response_model=WhoAmI)
    def whoami(who: Principal = Depends(principal)):
        return WhoAmI(operator=who.operator, scope=who.scope)

    # ---- nodes -----------------------------------------------------------
    @app.get("/nodes", response_model=NodeList)
    def list_nodes(prefix: str | None = None, kind: NodeKind | None = None,
                   state: NodeState | None = None, limit: int = Query(500, ge=1, le=5000),
                   db=Depends(get_db)):
        stmt = select(NodeRow).order_by(NodeRow.hier_id).limit(limit)
        if prefix:
            if not hierid.is_prefix(prefix):
                raise HTTPException(422, "not a hier-id prefix")
            stmt = stmt.where(service.under(prefix))
        if kind:
            stmt = stmt.where(NodeRow.kind == kind)
        if state:
            stmt = stmt.where(NodeRow.state == state)
        quarantined = service.quarantined_ids(db)
        items = [service.to_node(r, quarantined) for r in db.scalars(stmt)]
        return NodeList(count=len(items), items=items)

    @app.get("/nodes/{hier_id}", response_model=Node)
    def get_node(hier_id: str, db=Depends(get_db)):
        row = db.get(NodeRow, hier_id)
        if row is None:
            raise HTTPException(404, f"{hier_id} not found")
        return service.to_node(row, service.quarantined_ids(db))

    @app.put("/nodes/{hier_id}", response_model=Node)
    def put_node(hier_id: str, body: NodeIn, request: Request, response: Response,
                 who: Principal = Depends(principal), db=Depends(get_db)):
        check_scope(request, who, hier_id)
        hid = hierid.parse(hier_id)
        parent = str(hid.parent) if hid.parent else None
        if parent and db.get(NodeRow, parent) is None:
            raise HTTPException(409, f"parent {parent} does not exist")
        values = body.model_dump()
        row = db.get(NodeRow, hier_id)
        op = "update"
        if row is None:
            op = "create"
            row = NodeRow(hier_id=hier_id, kind=hid.kind, parent=parent, city=hid.city,
                          station=hid.station, charger=hid.charger, created_at=service.now())
            db.add(row)
            response.status_code = 201
        for name in FIELDS:
            setattr(row, name, values[name])
        row.updated_at, row.updated_by = service.now(), who.operator
        db.commit()
        jsonlog.get_logger().event("registry.write", f"{op} {hier_id}", hier_id=hier_id,
                                   op=op, operator=who.operator)
        wrote(who, db)
        return service.to_node(row, service.quarantined_ids(db))

    @app.patch("/nodes/{hier_id}", response_model=Node)
    def patch_node(hier_id: str, body: NodePatch, request: Request,
                   who: Principal = Depends(principal), db=Depends(get_db)):
        check_scope(request, who, hier_id)
        row = get_row(db, hier_id, who)
        for name, value in body.model_dump(exclude_unset=True).items():
            setattr(row, name, value)
        row.updated_at, row.updated_by = service.now(), who.operator
        db.commit()
        jsonlog.get_logger().event("registry.write", f"update {hier_id}", hier_id=hier_id,
                                   op="update", operator=who.operator)
        wrote(who, db)
        return service.to_node(row, service.quarantined_ids(db))

    @app.delete("/nodes/{hier_id}", status_code=204)
    def delete_node(hier_id: str, request: Request, recursive: bool = False,
                    who: Principal = Depends(principal), db=Depends(get_db)):
        check_scope(request, who, hier_id)
        get_row(db, hier_id, who)
        if recursive:
            db.execute(sql_delete(NodeRow).where(service.under(hier_id)))
        else:
            if db.scalar(select(NodeRow.hier_id).where(NodeRow.parent == hier_id).limit(1)):
                raise HTTPException(409, f"{hier_id} has children; use recursive=true")
            db.execute(sql_delete(NodeRow).where(NodeRow.hier_id == hier_id))
        db.commit()
        jsonlog.get_logger().event("registry.write", f"delete {hier_id}", hier_id=hier_id,
                                   op="delete", operator=who.operator)
        wrote(who, db)
        return Response(status_code=204)

    # ---- quarantine ------------------------------------------------------
    def set_quarantine(hier_id: str, body: QuarantineRequest, request: Request,
                       who: Principal, db, action: str) -> Node:
        check_scope(request, who, hier_id, isolate=action == "quarantine")
        row = get_row(db, hier_id, who)
        target = "quarantined" if action == "quarantine" else "active"
        if row.state != target and (action == "quarantine" or row.state == "quarantined"):
            row.state = target
            row.state_reason = body.reason if action == "quarantine" else None
            row.state_source = body.source if action == "quarantine" else None
            row.state_changed_at = row.updated_at = service.now()
            row.updated_by = who.operator
            db.add(QuarantineLogRow(
                hier_id=hier_id, action=action, reason=body.reason, source=body.source,
                operator=who.operator, incident_id=body.incident_id, at=row.state_changed_at))
            db.commit()
            jsonlog.get_logger().event(
                f"registry.{action}", f"{action} {hier_id}",
                level="warning" if action == "quarantine" else "info",
                hier_id=hier_id, reason=body.reason, source=body.source,
                operator=who.operator, incident_id=body.incident_id)
            QUARANTINES.labels(city=row.city or "central", action=action).inc()
            wrote(who, db)
        return service.to_node(row, service.quarantined_ids(db))

    @app.post("/quarantine/{hier_id}", response_model=Node)
    def quarantine(hier_id: str, body: QuarantineRequest, request: Request,
                   who: Principal = Depends(principal), db=Depends(get_db)):
        return set_quarantine(hier_id, body, request, who, db, "quarantine")

    @app.post("/quarantine/{hier_id}/release", response_model=Node)
    def release(hier_id: str, body: QuarantineRequest, request: Request,
                who: Principal = Depends(principal), db=Depends(get_db)):
        return set_quarantine(hier_id, body, request, who, db, "release")

    @app.get("/quarantine", response_model=NodeList)
    def list_quarantine(prefix: str | None = None, db=Depends(get_db)):
        stmt = select(NodeRow).where(NodeRow.state == "quarantined").order_by(NodeRow.hier_id)
        if prefix:
            if not hierid.is_prefix(prefix):
                raise HTTPException(422, "not a hier-id prefix")
            stmt = stmt.where(service.under(prefix))
        quarantined = service.quarantined_ids(db)
        items = [service.to_node(r, quarantined) for r in db.scalars(stmt)]
        return NodeList(count=len(items), items=items)

    # ---- locate ----------------------------------------------------------
    def locate_response(db, query: str, rows: list[NodeRow]) -> LocateResponse:
        truncated = len(rows) > service.LOCATE_LIMIT
        matches = service.locate_matches(db, rows[:service.LOCATE_LIMIT])
        return LocateResponse(query=query, count=len(matches), truncated=truncated, matches=matches)

    # declared before /locate/{prefix} so "ip" is not taken as a prefix
    @app.get("/locate/ip/{ip}", response_model=LocateResponse)
    def locate_ip(ip: str, db=Depends(get_db)):
        try:
            ipaddress.ip_address(ip)
        except ValueError as e:
            raise HTTPException(422, "not an IP address") from e
        hosts = list(db.scalars(select(NodeRow).where(NodeRow.ip == ip)))
        stations = [r.hier_id for r in hosts if r.kind == "station"]
        chargers = list(db.scalars(select(NodeRow).where(NodeRow.parent.in_(stations)))) if stations else []
        rows = sorted({r.hier_id: r for r in hosts + chargers}.values(), key=lambda r: r.hier_id)
        return locate_response(db, ip, rows)

    @app.get("/locate/{prefix}", response_model=LocateResponse)
    def locate(prefix: str, kind: NodeKind | None = None, db=Depends(get_db)):
        if not hierid.is_prefix(prefix):
            raise HTTPException(422, "not a hier-id prefix")
        return locate_response(db, prefix, service.subtree(db, prefix, kind))

    # ---- Prometheus service discovery --------------------------------------
    @app.get("/sd/prometheus", response_model=list[PromSdTarget])
    def sd_prometheus(city: str | None = None, kind: NodeKind | None = None, db=Depends(get_db)):
        stmt = select(NodeRow).where(NodeRow.metrics_target.is_not(None)).order_by(NodeRow.hier_id)
        if city:
            stmt = stmt.where(NodeRow.city == city)
        if kind:
            stmt = stmt.where(NodeRow.kind == kind)
        out = []
        for r in db.scalars(stmt):
            labels = {"hier_id": r.hier_id}
            if r.city:
                labels["city"] = r.city
            if r.kind == "station":
                labels["station"] = r.station
            out.append(PromSdTarget(targets=[r.metrics_target], labels=labels))
        return out

    return app
