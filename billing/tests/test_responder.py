"""The isolate-only responder token (section 4.3)."""

import secrets

import pytest
from conftest import CARD, CHARGER, Api, make_settings
from evbilling.app import create_app
from evbilling.settings import Settings
from evcommon import config, jsonlog
from fastapi.testclient import TestClient


@pytest.fixture
def both(tmp_path):
    """(admin Api, responder Api) on the same app."""
    api_tok, resp_tok = secrets.token_urlsafe(24), secrets.token_urlsafe(24)
    jsonlog.setup_logging("billing", instance="test", log_dir=tmp_path, stdout=False)
    settings = Settings(api_token=api_tok, responder_token=resp_tok,
                        database_url=f"sqlite:///{tmp_path}/billing.db")
    with TestClient(create_app(settings)) as client:
        admin, resp = Api(client, api_tok, tmp_path), Api(client, resp_tok, tmp_path)
        assert admin.post("/cards", {"card": CARD, "holder": "d", "home_city": "porto"}).status_code == 201
        assert admin.start(1).status_code == 201
        yield admin, resp


BLOCK = {"reason": "fraud", "source": "wazuh"}


def scope_lines(api):
    return [x for x in api.log_lines("api.auth_failed") if x.get("reason") == "scope"]


def test_responder_allowed_calls(both):
    admin, resp = both
    assert resp.get(f"/cards/{CARD}").status_code == 200
    assert resp.get("/sessions").status_code == 200
    assert resp.post(f"/cards/{CARD}/block", BLOCK).json()["status"] == "blocked"
    assert resp.post(f"/cards/{CARD}/block", BLOCK).status_code == 200      # idempotent
    flagged = resp.post("/sessions/1/flag", {"reason": "clone", "source": "wazuh"})
    assert flagged.status_code == 200 and flagged.json()["status"] == "flagged"
    assert not scope_lines(admin)


def test_responder_refused_calls(both):
    admin, resp = both
    admin.post(f"/cards/{CARD}/block", BLOCK)
    refused = [
        resp.post(f"/cards/{CARD}/unblock", BLOCK),
        resp.start(2),
        resp.stop(1),
        resp.post("/authorize", {"card": CARD, "hier_id": CHARGER}),
        resp.post("/cards", {"card": "AABBCCDDEEFF00", "holder": "x", "home_city": "porto"}),
        resp.get("/tariffs"),
    ]
    assert [r.status_code for r in refused] == [403] * 6
    assert admin.get(f"/cards/{CARD}").json()["status"] == "blocked"      # still blocked
    assert admin.get("/sessions").json()["items"][0]["status"] == "open"
    lines = scope_lines(admin)
    assert len(lines) == 6 and lines[0]["operator"] == "responder"
    assert lines[0]["path"].endswith("/unblock") and lines[0]["method"] == "POST"


def test_responder_check_order(both):
    admin, resp = both
    # 401 first, then 422, then 403
    assert resp.post(f"/cards/{CARD}/unblock", BLOCK, auth=False).status_code == 401
    assert resp.post(f"/cards/{CARD}/unblock", {"nope": 1}).status_code == 422
    assert resp.post(f"/cards/{CARD}/unblock", BLOCK).status_code == 403
    assert resp.post("/sessions/99/flag", {"reason": "x", "source": "wazuh"}).status_code == 404


def test_admin_unchanged(both):
    admin, _ = both
    assert admin.post(f"/cards/{CARD}/block", BLOCK).status_code == 200
    assert admin.post(f"/cards/{CARD}/unblock", BLOCK).json()["status"] == "active"
    assert admin.get("/tariffs").status_code == 200


def test_responder_token_rules(tmp_path):
    ok = {"BILLING_API_TOKEN": "a" * 20}
    assert Settings.from_env(ok).responder_token == ""
    for bad in ("short", "a" * 20):
        with pytest.raises(config.ConfigError):
            Settings.from_env({**ok, "BILLING_RESPONDER_TOKEN": bad})


def test_feature_off_without_token(make_api):
    a = make_api()
    assert a.c.get("/sessions", headers={"Authorization": "Bearer "}).status_code == 401
    assert a.c.get("/sessions", headers={"Authorization": "Bearer x"}).status_code == 401
