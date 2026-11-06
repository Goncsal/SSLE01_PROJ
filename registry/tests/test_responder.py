"""The isolate-only responder token (section 4.1)."""

import pytest
from evcommon import config
from evregistry.settings import Settings

STATION = "pt.porto.st-001"
CHARGER = "pt.porto.st-001.cp-1"
BODY = {"reason": "test", "source": "wazuh"}


def denied(api):
    return [x for x in api.log_lines() if x["event"] == "registry.write_denied"]


def test_responder_whoami_and_reads(porto):
    assert porto.get("/whoami", "responder").json()["operator"] == "responder"
    assert porto.get("/nodes/" + CHARGER, "responder").status_code == 200
    assert porto.get("/quarantine", "responder").status_code == 200


@pytest.mark.parametrize("target", ["pt.porto", STATION, CHARGER])
def test_responder_may_quarantine_any_element(porto, target):
    r = porto.call("POST", f"/quarantine/{target}", "responder", json=BODY)
    assert r.status_code == 200 and r.json()["state"] == "quarantined"
    again = porto.call("POST", f"/quarantine/{target}", "responder", json=BODY)
    assert again.status_code == 200
    q = [x for x in porto.log_lines() if x["event"] == "registry.quarantine"]
    assert q and q[0]["operator"] == "responder"
    assert not denied(porto)


def test_responder_cannot_release(porto):
    porto.call("POST", f"/quarantine/{CHARGER}", "responder", json=BODY)
    r = porto.call("POST", f"/quarantine/{CHARGER}/release", "responder", json=BODY)
    assert r.status_code == 403
    assert porto.get("/nodes/" + CHARGER).json()["state"] == "quarantined"
    d = denied(porto)
    assert d and d[-1]["operator"] == "responder" and d[-1]["hier_id"] == CHARGER
    # the admin can release
    assert porto.call("POST", f"/quarantine/{CHARGER}/release", "admin", json=BODY).status_code == 200


def test_responder_cannot_write(porto):
    assert porto.put(f"/nodes/{CHARGER}", "responder", json={"max_kw": 7}).status_code == 403
    assert porto.put("/nodes/pt.porto.st-009", "responder").status_code == 403
    assert porto.call("PATCH", f"/nodes/{CHARGER}", "responder", json={"max_kw": 7}).status_code == 403
    assert porto.call("DELETE", f"/nodes/{CHARGER}", "responder").status_code == 403
    assert porto.call("DELETE", f"/nodes/{STATION}?recursive=true", "responder").status_code == 403
    assert porto.get("/nodes/" + CHARGER).json()["max_kw"] == 22
    assert len(denied(porto)) == 5


def test_responder_check_order(porto):
    assert porto.call("POST", "/quarantine/nonsense/release", "responder", json=BODY).status_code == 422
    assert porto.call("POST", "/quarantine/pt.porto.st-009.cp-1", "responder", json=BODY).status_code == 404
    assert porto.call("POST", f"/quarantine/{CHARGER}", None, json=BODY).status_code == 401


def test_other_principals_unchanged(porto):
    assert porto.call("POST", f"/quarantine/{CHARGER}", "porto", json=BODY).status_code == 200
    assert porto.call("POST", f"/quarantine/{CHARGER}/release", "porto", json=BODY).status_code == 200
    assert porto.call("POST", "/quarantine/pt.lisboa", "porto", json=BODY).status_code == 403


def test_responder_token_rules():
    ok = {"REGISTRY_ADMIN_TOKEN": "a" * 20}
    assert Settings.from_env(ok).responder_token == ""        # absent = off
    assert Settings.from_env({**ok, "REGISTRY_RESPONDER_TOKEN": "r" * 20}).responder_token == "r" * 20
    for bad in ({"REGISTRY_RESPONDER_TOKEN": "short"}, {"REGISTRY_RESPONDER_TOKEN": "a" * 20},
                {"REGISTRY_RESPONDER_TOKEN": "c" * 20, "REGISTRY_TOKEN_PORTO": "c" * 20}):
        with pytest.raises(config.ConfigError):
            Settings.from_env({**ok, **bad})


def test_feature_off_without_token(tmp_path):
    from evregistry.auth import identify
    s = Settings(admin_token="a" * 20, database_url=f"sqlite:///{tmp_path}/x.db")
    assert identify(s, "Bearer ") is None and identify(s, "Bearer x") is None
