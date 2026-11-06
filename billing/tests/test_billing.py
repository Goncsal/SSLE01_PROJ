import datetime as dt
import threading
import time
from contextlib import nullcontext

import httpx
import pytest
import uvicorn
import yaml
from conftest import CARD, CHARGER, make_settings
from evbilling.app import create_app
from evcommon.models import SessionStart, SessionStop

OTHER = "AABBCCDDEEFF00"
CLONE = "1122334455667788"


# ---- auth and check order ----------------------------------------------------

def test_auth_required(api):
    assert api.get("/sessions", auth=False).status_code == 401
    bad = api.c.get("/sessions", headers={"Authorization": "Bearer wrong"})
    assert bad.status_code == 401
    assert api.get("/health", auth=False).status_code == 200
    assert api.get("/metrics", auth=False).status_code == 200
    assert api.log_lines("api.auth_failed")[0]["path"] == "/sessions"


def test_check_order_401_before_422(api):
    assert api.call("POST", "/authorize", auth=False, json={"nope": 1}).status_code == 401
    assert api.post("/authorize", {"nope": 1}).status_code == 422
    assert api.call("GET", "/cards/xyz", auth=False).status_code == 401
    assert api.get("/cards/xyz").status_code == 422


# ---- authorize ---------------------------------------------------------------

def test_authorize_statuses(api):
    def status(card):
        r = api.post("/authorize", {"card": card, "hier_id": CHARGER})
        assert r.status_code == 200
        return r.json()["status"]

    assert status(CARD) == "Accepted"
    assert status(OTHER) == "Invalid"
    api.post(f"/cards/{CARD}/block", {"reason": "cloned"})
    assert status(CARD) == "Blocked"
    past = (dt.datetime.now(dt.UTC) - dt.timedelta(days=1)).isoformat()
    api.post("/cards", {"card": CLONE, "holder": "x", "home_city": "porto", "expiry_date": past})
    assert status(CLONE) == "Expired"


# ---- sessions ----------------------------------------------------------------

def test_open_stop_prices_session(api):
    r = api.start(7, meter=1199790)
    assert r.status_code == 201
    assert r.json()["status"] == "open" and r.json()["city"] == "porto"
    r = api.stop(7, meter=1203456, at="2026-11-06T09:10:00Z")
    body = r.json()
    assert body["energy_wh"] == 3666 and body["cost_eur"] == 1.28
    assert body["status"] == "closed" and body["stop_reason"] == "Local"
    line = api.log_lines("billing.session_closed")[0]
    assert line["transaction_id"] == 7 and line["cost_eur"] == 1.28


def test_stop_is_idempotent(api):
    api.start()
    first = api.stop().json()
    again = api.stop(meter=9999999).json()
    assert again == first


def test_start_idempotent_and_conflict(api):
    assert api.start(5).status_code == 201
    assert api.start(5).status_code == 200
    assert api.start(5, card=OTHER).status_code == 409
    assert api.start(5, hier_id="pt.porto.st-001.cp-2").status_code == 409
    assert api.get("/sessions").json()["count"] == 1


def test_unknown_card_uses_standard_tariff(api):
    assert api.start(3, card=OTHER).json()["tariff_id"] == "standard"


def test_stop_unknown_is_404(api):
    assert api.stop(99).status_code == 404


def test_negative_energy_is_flagged(api):
    api.start(2, meter=5000)
    body = api.stop(2, meter=4000).json()
    assert body["status"] == "flagged" and body["flag_reason"] == "meter_backwards"
    assert body["energy_wh"] == 0 and body["cost_eur"] == 0
    assert api.log_lines("billing.session_flagged")[0]["reason"] == "meter_backwards"


def test_flag_session(api):
    api.start(4)
    r = api.post("/sessions/4/flag", {"reason": "tamper", "source": "fraud", "incident_id": "INC-1"})
    assert r.json()["status"] == "flagged" and r.json()["flag_reason"] == "tamper"
    api.post("/sessions/4/flag", {"reason": "again"})          # idempotent
    lines = api.log_lines("billing.session_flagged")
    assert len(lines) == 1 and lines[0]["source"] == "fraud" and lines[0]["incident_id"] == "INC-1"
    assert api.post("/sessions/404/flag", {"reason": "x"}).status_code == 404
    assert api.stop(4).json()["status"] == "flagged"            # still priced, still flagged


def test_list_filters(api):
    api.start(1, at="2026-11-06T09:00:00Z")
    api.start(2, card=OTHER, hier_id="pt.lisboa.st-001.cp-1", at="2026-11-07T09:00:00Z")
    api.start(3, at="2026-11-08T09:00:00Z")
    api.stop(3)

    def ids(**params):
        return [s["transaction_id"] for s in api.get("/sessions", params=params).json()["items"]]

    assert ids() == [3, 2, 1]
    assert ids(card=CARD) == [3, 1]
    assert ids(hier_id="pt.lisboa") == [2]
    assert ids(hier_id=CHARGER) == [3, 1]
    assert ids(status="closed") == [3]
    assert ids(since="2026-11-07T00:00:00Z") == [3, 2]
    assert ids(limit=1) == [3]
    assert api.get("/sessions", params={"limit": 1001}).status_code == 422
    assert api.get("/sessions", params={"status": "bogus"}).status_code == 422


# ---- cards -------------------------------------------------------------------

def test_block_unblock_history(api):
    r = api.post(f"/cards/{CARD}/block", {"reason": "cloned", "source": "wazuh", "incident_id": "INC-9"})
    assert r.json()["status"] == "blocked" and r.json()["blocked_reason"] == "cloned"
    api.post(f"/cards/{CARD}/block", {"reason": "again"})         # idempotent
    assert len(api.log_lines("billing.card_blocked")) == 1
    assert api.log_lines("billing.card_blocked")[0]["incident_id"] == "INC-9"
    assert "evcharge_billing_blocked_cards 1.0" in api.get("/metrics").text

    r = api.post(f"/cards/{CARD}/unblock", {"reason": "false positive"})
    assert r.json()["status"] == "active" and r.json()["blocked_reason"] is None
    assert api.log_lines("billing.card_unblocked")[0]["reason"] == "false positive"
    assert "evcharge_billing_blocked_cards 0.0" in api.get("/metrics").text

    from evbilling.db import BlockedRow
    with api.c.app.state.sessions() as db:
        rows = db.query(BlockedRow).all()
    assert len(rows) == 1 and rows[0].unblocked_at is not None and rows[0].source == "wazuh"


def test_card_errors(api):
    assert api.get(f"/cards/{OTHER}").status_code == 404
    assert api.post(f"/cards/{OTHER}/block", {"reason": "x"}).status_code == 404
    assert api.post(f"/cards/{CARD}/block", {"reason": "x", "source": "bogus"}).status_code == 422
    assert api.post("/cards", {"card": CARD, "holder": "x", "home_city": "porto"}).status_code == 409
    assert api.post("/cards", {"card": OTHER, "holder": "x", "home_city": "porto",
                               "tariff_id": "nope"}).status_code == 422
    assert api.get(f"/cards/{CARD}").json()["holder"] == "driver-0001"


def test_tariffs(api):
    [t] = api.get("/tariffs").json()
    assert t == {"tariff_id": "standard", "price_per_kwh_eur": 0.35, "price_per_min_eur": 0.0,
                 "session_fee_eur": 0.0, "currency": "EUR"}


# ---- seeding and metrics -------------------------------------------------------

def test_seed_from_roster(make_api, tmp_path):
    roster = {"cities": [{"city": "porto", "stations": [{
        "station": "st-001", "name": "A", "lat": 41.0, "lon": -8.0,
        "chargers": [{"charger": "cp-1", "max_kw": 22}]}]}],
        "cards": [{"card": CARD, "holder": "d1", "home_city": "porto"},
                  {"card": OTHER, "holder": "d2", "home_city": "porto", "blocked": True}]}
    path = tmp_path / "roster.yaml"
    path.write_text(yaml.safe_dump(roster))
    api = make_api(str(path))
    assert api.get(f"/cards/{CARD}").json()["status"] == "active"
    blocked = api.get(f"/cards/{OTHER}").json()
    assert blocked["status"] == "blocked" and blocked["blocked_reason"] == "seeded as blocked"
    assert api.post("/authorize", {"card": OTHER, "hier_id": CHARGER}).json()["status"] == "Blocked"
    assert len(api.get("/tariffs").json()) == 1
    assert "evcharge_billing_blocked_cards 1.0" in api.get("/metrics").text


def test_metric_names(api):
    api.post("/authorize", {"card": CARD, "hier_id": CHARGER})
    text = api.get("/metrics").text
    for name in ("authorize_total", "sessions_total", "energy_wh_total", "revenue_eur_total",
                 "blocked_cards", "card_blocks_total"):
        assert f"# TYPE evcharge_billing_{name.removesuffix('_total')}" in text


def test_health(api):
    assert api.get("/health").json() == {"status": "ok", "service": "billing", "version": "0.1.0",
                                         "checks": {"db": "ok"}}


# ---- contract test with the real CSMS client -------------------------------------

class _Telemetry:
    def timed(self, _name):
        return nullcontext()

    def dependency_error(self, *_a):
        raise AssertionError("billing call failed")


def test_csms_http_client(tmp_path):
    import asyncio
    import socket

    from evcsms.billing import HttpBilling

    token = "t" * 24
    app = create_app(make_settings(tmp_path, token))
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_config=None))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    assert server.started

    async def drive():
        client = HttpBilling(f"http://127.0.0.1:{port}", token, _Telemetry())
        try:
            assert await client.authorize(CARD, CHARGER) == "Invalid"
            await client.session_start(SessionStart(
                transaction_id=11, card=CARD, hier_id=CHARGER, connector_id=1, meter_start_wh=0,
                started_at=dt.datetime(2026, 11, 6, 9, 0, tzinfo=dt.UTC)))
            await client.session_stop(11, SessionStop(
                meter_stop_wh=2000, stopped_at=dt.datetime(2026, 11, 6, 9, 5, tzinfo=dt.UTC)))
        finally:
            await client.aclose()

    try:
        asyncio.run(drive())
        rows = httpx.get(f"http://127.0.0.1:{port}/sessions",
                         headers={"Authorization": f"Bearer {token}"}).json()["items"]
        assert rows[0]["status"] == "closed" and rows[0]["cost_eur"] == 0.7
    finally:
        server.should_exit = True
        thread.join(5)
