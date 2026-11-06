"""The isolate-only responder token (section 4.2)."""

import asyncio

import httpx
import pytest
from evcommon import config
from evcsms.settings import Settings
from helpers import API_TOKEN, CHARGER, connected, running

RESPONDER = "responder-token-0123456789"


def responder_api(srv):
    return httpx.AsyncClient(base_url=f"http://127.0.0.1:{srv.settings.api_port}",
                             headers={"Authorization": f"Bearer {RESPONDER}"})


def test_responder_allowed_commands(tmp_path):
    async def go():
        async with running(tmp_path, CSMS_RESPONDER_TOKEN=RESPONDER) as srv, responder_api(srv) as r:
            async with connected(srv) as cp:
                assert (await r.get("/connections")).status_code == 200
                assert (await r.get(f"/connections/{CHARGER}")).status_code == 200
                body = {"connector_id": 0, "type": "Inoperative", "reason": "quarantine", "source": "wazuh"}
                resp = await r.post(f"/commands/{CHARGER}/change-availability", json=body)
                assert resp.status_code == 200 and resp.json()["status"] == "Accepted"
                await cp.start_tx()
                resp = await r.post(f"/commands/{CHARGER}/remote-stop", json={})
                assert resp.status_code == 200
                resp = await r.post(f"/commands/{CHARGER}/disconnect", json={"reason": "t"})
                assert resp.status_code == 200
                await cp._connection.wait_closed()
            assert not [e for e in srv.events("api.auth_failed")]
    asyncio.run(go())


def test_responder_refused_commands(tmp_path):
    async def go():
        async with running(tmp_path, CSMS_RESPONDER_TOKEN=RESPONDER) as srv, responder_api(srv) as r:
            async with connected(srv):
                operative = {"connector_id": 0, "type": "Operative"}
                tries = [
                    await r.post(f"/commands/{CHARGER}/reset", json={}),
                    await r.post(f"/commands/{CHARGER}/reset", json={"type": "Hard"}),
                    await r.post(f"/commands/{CHARGER}/change-availability", json=operative),
                ]
                assert [t.status_code for t in tries] == [403] * 3
                assert (await r.get("/connections")).status_code == 200      # still connected
                # 422 before 403
                assert (await r.post("/commands/nonsense/reset", json={})).status_code == 422
                assert (await r.post(f"/commands/{CHARGER}/change-availability",
                                     json={"type": "Bogus"})).status_code == 422
            lines = [e for e in srv.events("api.auth_failed") if e.get("reason") == "scope"]
            assert len(lines) == 3 and lines[0]["operator"] == "responder"
            assert lines[0]["path"].endswith("/reset") and lines[0]["method"] == "POST"
            assert not srv.events("command.sent")
    asyncio.run(go())


def test_api_token_unchanged_and_401(tmp_path):
    async def go():
        async with running(tmp_path, CSMS_RESPONDER_TOKEN=RESPONDER) as srv, srv.api as api:
            async with connected(srv):
                body = {"connector_id": 0, "type": "Operative"}
                assert (await api.post(f"/commands/{CHARGER}/change-availability", json=body)).status_code == 200
            base = f"http://127.0.0.1:{srv.settings.api_port}"
            async with httpx.AsyncClient(base_url=base) as anon:
                assert (await anon.post(f"/commands/{CHARGER}/reset", json={})).status_code == 401
    asyncio.run(go())


def test_feature_off_without_token(tmp_path):
    async def go():
        async with running(tmp_path) as srv, responder_api(srv) as r:
            assert (await r.get("/connections")).status_code == 401
    asyncio.run(go())


def test_responder_token_rules():
    ok = {"CSMS_API_TOKEN": API_TOKEN}
    assert Settings.from_env(ok).responder_token == ""
    assert Settings.from_env({**ok, "CSMS_RESPONDER_TOKEN": RESPONDER}).responder_token == RESPONDER
    for bad in ("short", API_TOKEN):
        with pytest.raises(config.ConfigError):
            Settings.from_env({**ok, "CSMS_RESPONDER_TOKEN": bad})
