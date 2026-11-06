"""The CSMS HTTP API (section 4.2): connections, commands, /health and /metrics."""

from __future__ import annotations

import hmac

from evcommon import hierid
from evcommon.models import (ChangeAvailabilityRequest, CommandResult, Connection, ConnectionList,
                             DisconnectRequest, Health, RemoteStopRequest, ResetRequest)
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse

from . import __version__
from .bus import CommandRefused

COMMAND_HTTP_STATUS = {"NotConnected": 404, "Timeout": 504, "Error": 502}


def create_app(ctx, bus) -> FastAPI:
    """Build the API around a running CSMS context and a command bus."""
    app = FastAPI(title="CSMS", version=__version__)
    token = ctx.settings.api_token.encode()
    responder = ctx.settings.responder_token.encode()

    def srcip(request: Request):
        return request.client.host if request.client else None

    def require_token(request: Request) -> None:
        """Bearer token check in constant time; failures are logged for Wazuh.
        Sets request.state.operator to "api" or "responder"."""
        header = request.headers.get("Authorization", "")
        supplied = header[7:].encode() if header.startswith("Bearer ") else b""
        is_api = hmac.compare_digest(supplied, token)
        is_responder = bool(responder) and hmac.compare_digest(supplied, responder)   # no early exit
        if not (is_api or is_responder):
            ctx.log.event("api.auth_failed", "missing or wrong API token", level="warning",
                          srcip=srcip(request), path=request.url.path, method=request.method)
            raise HTTPException(401, "invalid or missing bearer token")
        request.state.operator = "api" if is_api else "responder"

    def scope(request: Request, allowed: bool = False) -> None:
        """403 for the responder token on anything but its isolate-only calls. Called after
        the path and body are validated (422 before 403, section 4.0)."""
        if request.state.operator == "responder" and not allowed:
            ctx.log.event("api.auth_failed", "responder token out of scope", level="warning",
                          reason="scope", operator="responder", srcip=srcip(request),
                          path=request.url.path, method=request.method)
            raise HTTPException(403, "responder token may only isolate")

    def charger_id(hier_id: str) -> str:
        if not hierid.is_valid(hier_id, "charger"):
            raise HTTPException(422, "not a charger hier-id")
        return hier_id

    # ---- open endpoints -----------------------------------------------------------------

    @app.get("/health", response_model=Health)
    async def health():
        db_ok = await ctx.db.ping()
        return Health(status="ok" if db_ok else "degraded", service="csms", version=__version__,
                      checks={"db": "ok" if db_ok else "error"})

    @app.get("/metrics")
    async def metrics():
        body, content_type = ctx.metrics.render()
        return Response(body, media_type=content_type)

    # ---- connections ---------------------------------------------------------------------

    @app.get("/connections", response_model=ConnectionList, dependencies=[Depends(require_token)])
    async def list_connections(request: Request, prefix: str = ""):
        scope(request, allowed=True)
        if prefix and not hierid.is_prefix(prefix):
            raise HTTPException(422, "prefix is not a hier-id prefix")
        rows = await ctx.db.list_connections()
        items = [Connection(**row) for row in rows if not prefix or hierid.is_under(row["hier_id"], prefix)]
        return ConnectionList(count=len(items), items=items)

    @app.get("/connections/{hier_id}", response_model=Connection,
             dependencies=[Depends(require_token)])
    async def get_connection(hier_id: str, request: Request):
        scope(request, allowed=True)
        rows = await ctx.db.list_connections(charger_id(hier_id))
        if not rows:
            raise HTTPException(404, "charger is not connected")
        return Connection(**rows[0])

    # ---- commands --------------------------------------------------------------------------

    async def run_command(request: Request, hier_id: str, command: str, body, args: dict,
                          allowed: bool = False) -> JSONResponse:
        charger_id(hier_id)
        scope(request, allowed)
        meta = {"reason": body.reason, "source": body.source, "incident_id": body.incident_id}
        try:
            result: CommandResult = await bus.execute(hier_id, command, args, meta)
            code = COMMAND_HTTP_STATUS.get(result.status, 200)
        except CommandRefused as refusal:
            result, code = refusal.result, 409
        return JSONResponse(result.model_dump(mode="json"), status_code=code)

    @app.post("/commands/{hier_id}/remote-stop", response_model=CommandResult,
              dependencies=[Depends(require_token)])
    async def remote_stop(hier_id: str, request: Request, body: RemoteStopRequest = RemoteStopRequest()):
        return await run_command(request, hier_id, "remote-stop", body,
                                 {"transaction_id": body.transaction_id}, allowed=True)

    @app.post("/commands/{hier_id}/change-availability", response_model=CommandResult,
              dependencies=[Depends(require_token)])
    async def change_availability(hier_id: str, request: Request, body: ChangeAvailabilityRequest):
        return await run_command(request, hier_id, "change-availability", body,
                                 {"connector_id": body.connector_id, "type": body.type},
                                 allowed=body.type == "Inoperative")

    @app.post("/commands/{hier_id}/reset", response_model=CommandResult,
              dependencies=[Depends(require_token)])
    async def reset(hier_id: str, request: Request, body: ResetRequest = ResetRequest()):
        return await run_command(request, hier_id, "reset", body, {"type": body.type})

    @app.post("/commands/{hier_id}/disconnect", response_model=CommandResult,
              dependencies=[Depends(require_token)])
    async def disconnect(hier_id: str, request: Request, body: DisconnectRequest = DisconnectRequest()):
        return await run_command(request, hier_id, "disconnect", body, {}, allowed=True)

    return app
