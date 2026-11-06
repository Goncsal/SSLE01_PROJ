"""The command API (section 4.2)."""

import asyncio

import httpx
from helpers import CHARGER, connected, running


def test_change_availability_reaches_the_charger(tmp_path):
    async def go():
        async with running(tmp_path) as srv, srv.api as api:
            async with connected(srv):
                body = {"connector_id": 0, "type": "Inoperative", "reason": "quarantine",
                        "source": "wazuh", "incident_id": "inc-1"}
                resp = await api.post(f"/commands/{CHARGER}/change-availability", json=body)
                assert resp.status_code == 200
                assert resp.json()["status"] == "Accepted" and resp.json()["pod"] == "test"
            sent = srv.events("command.sent")[0]
            assert sent["command"] == "change-availability" and sent["incident_id"] == "inc-1"
            assert srv.events("command.result")[0]["result"] == "Accepted"
    asyncio.run(go())


def test_remote_stop_without_transaction_is_409(tmp_path):
    async def go():
        async with running(tmp_path) as srv, srv.api as api:
            async with connected(srv):
                resp = await api.post(f"/commands/{CHARGER}/remote-stop", json={})
                assert resp.status_code == 409 and resp.json()["status"] == "Rejected"
    asyncio.run(go())


def test_remote_stop_with_transaction_is_accepted(tmp_path):
    async def go():
        async with running(tmp_path) as srv, srv.api as api:
            async with connected(srv) as cp:
                await cp.start_tx()
                resp = await api.post(f"/commands/{CHARGER}/remote-stop", json={})
                assert resp.status_code == 200 and resp.json()["status"] == "Accepted"
    asyncio.run(go())


def test_unknown_charger_is_404(tmp_path):
    async def go():
        async with running(tmp_path) as srv, srv.api as api:
            resp = await api.post("/commands/pt.porto.st-009.cp-1/disconnect", json={})
            assert resp.status_code == 404 and resp.json()["status"] == "NotConnected"
            resp = await api.post("/commands/nonsense/disconnect", json={})
            assert resp.status_code == 422
    asyncio.run(go())


def test_disconnect_closes_with_1008_and_drops_registry_cache(tmp_path):
    async def go():
        async with running(tmp_path) as srv, srv.api as api:
            async with connected(srv) as cp:
                resp = await api.post(f"/commands/{CHARGER}/disconnect", json={"reason": "test"})
                assert resp.status_code == 200
                await cp._connection.wait_closed()
                assert cp._connection.close_code == 1008
            assert srv.events("conn.kicked")[0]["reason"] == "command"
    asyncio.run(go())


def test_command_without_token_is_401(tmp_path):
    async def go():
        async with running(tmp_path) as srv:
            base = f"http://127.0.0.1:{srv.settings.api_port}"
            async with httpx.AsyncClient(base_url=base) as anon:
                assert (await anon.get("/connections")).status_code == 401
                assert (await anon.post(f"/commands/{CHARGER}/reset", json={})).status_code == 401
                bad = {"Authorization": "Bearer wrong"}
                assert (await anon.get("/connections", headers=bad)).status_code == 401
                assert (await anon.get("/health")).json()["status"] == "ok"
                assert "evcharge_csms_connected_chargers" in (await anon.get("/metrics")).text
            assert len(srv.events("api.auth_failed")) == 3
    asyncio.run(go())
