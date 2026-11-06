import pytest
from evregistry.settings import Settings
from evcommon.config import ConfigError

QUAR = {"reason": "test", "source": "test"}


def test_hierarchy_creation(porto):
    cp = porto.get("/nodes/pt.porto.st-001.cp-1").json()
    assert cp["kind"] == "charger" and cp["parent"] == "pt.porto.st-001"
    assert (cp["city"], cp["station"], cp["charger"]) == ("porto", "st-001", "cp-1")
    assert cp["max_kw"] == 22 and cp["updated_by"] == "porto" and cp["state"] == "active"
    assert porto.get("/nodes/pt.porto").json()["updated_by"] == "admin"
    assert porto.get("/nodes?prefix=pt.porto").json()["count"] == 5
    assert porto.get("/nodes?kind=charger").json()["count"] == 2
    assert porto.get("/nodes/pt.nowhere").status_code == 404


def test_put_replaces_and_returns_200(porto):
    r = porto.put("/nodes/pt.porto.st-001.cp-1", "porto", json={"vendor": "EVLab"})
    assert r.status_code == 200
    assert r.json()["vendor"] == "EVLab" and r.json()["max_kw"] is None


@pytest.mark.parametrize("path", ["/nodes/pt.lisboa.st-001", "/nodes/pt.porto", "/nodes/pt.central.x"])
def test_city_token_denied_outside_scope(api, path):
    api.put("/nodes/pt.lisboa")
    r = api.put(path, "porto")
    assert r.status_code == 403
    assert any(x["event"] == "registry.write_denied" and x["hier_id"] == path.rsplit("/", 1)[1]
               and x["operator"] == "porto" and "srcip" in x for x in api.log_lines())


def test_city_token_allowed_in_scope(api):
    api.put("/nodes/pt.porto")
    assert api.put("/nodes/pt.porto.st-001", "porto").status_code == 201
    assert api.put("/nodes/pt.central.k3s-master", "admin").status_code == 201


def test_auth_required(api):
    assert api.put("/nodes/pt.porto", who=None).status_code == 401
    assert api.c.put("/nodes/pt.porto", headers={"Authorization": "Bearer wrong-wrong-wrong-x"}, json={}).status_code == 401
    assert api.call("POST", "/quarantine/pt.porto", json=QUAR).status_code == 401
    assert any(x["event"] == "api.auth_failed" for x in api.log_lines())


def test_whoami(api):
    assert api.get("/whoami", "porto").json() == {"operator": "porto", "scope": "pt.porto"}
    assert api.get("/whoami", "admin").json() == {"operator": "admin", "scope": "pt"}
    assert api.get("/whoami").status_code == 401


def test_missing_parent_and_bad_id(api):
    assert api.put("/nodes/pt.porto.st-001").status_code == 409
    api.put("/nodes/pt.porto")
    assert api.put("/nodes/pt.porto.st-001.cp-1").status_code == 409
    assert api.put("/nodes/pt.porto.bogus").status_code == 422


def test_patch_partial_update(porto):
    r = porto.call("PATCH", "/nodes/pt.porto.st-001", "porto", json={"ip": "10.31.0.99"})
    assert r.status_code == 200
    body = r.json()
    assert body["ip"] == "10.31.0.99" and body["name"] == "Aliados" and body["lat"] == 41.1496
    assert porto.call("PATCH", "/nodes/pt.porto.st-009", "porto", json={}).status_code == 404
    assert porto.call("PATCH", "/nodes/pt.lisboa.st-001", "porto", json={}).status_code == 403


def test_delete_with_children(porto):
    assert porto.call("DELETE", "/nodes/pt.porto.st-001", "porto").status_code == 409
    assert porto.call("DELETE", "/nodes/pt.porto.st-001", "porto", params={"recursive": "true"}).status_code == 204
    assert porto.get("/nodes/pt.porto.st-001.cp-1").status_code == 404
    assert porto.get("/nodes/pt.porto").status_code == 200
    assert porto.call("DELETE", "/nodes/pt.porto.st-001.cp-1", "porto").status_code == 404


def test_locate_exact(porto):
    r = porto.get("/locate/pt.porto.st-001.cp-1").json()
    assert r["count"] == 1 and not r["truncated"]
    m = r["matches"][0]
    assert m["path"] == ["pt.porto", "pt.porto.st-001", "pt.porto.st-001.cp-1"]
    assert (m["operator"], m["edge"], m["station"]) == ("porto", "pt.porto.edge", "pt.porto.st-001")
    assert (m["lat"], m["lon"], m["ip"], m["host"]) == (41.1496, -8.6109, "10.31.0.13", "porto-st-001")
    assert m["network"] == "city-porto" and m["dns"] == "st-001.porto.pt.lab"


def test_locate_prefix_and_root(porto):
    porto.put("/nodes/pt.portoalegre")
    ids = [m["hier_id"] for m in porto.get("/locate/pt.porto").json()["matches"]]
    assert ids == ["pt.porto", "pt.porto.edge", "pt.porto.st-001",
                   "pt.porto.st-001.cp-1", "pt.porto.st-001.cp-2"]
    assert porto.get("/locate/pt").json()["count"] == 6
    assert porto.get("/locate/pt.porto", params={"kind": "charger"}).json()["count"] == 2


def test_locate_central_operator_is_noc(api):
    api.put("/nodes/pt.central.wazuh")
    m = api.get("/locate/pt.central.wazuh").json()["matches"][0]
    assert m["operator"] == "noc" and m["edge"] is None


def test_locate_no_match_and_invalid(porto):
    r = porto.get("/locate/pt.faro")
    assert r.status_code == 200 and r.json()["count"] == 0
    assert porto.get("/locate/not-an-id").status_code == 422
    assert porto.get("/locate/pt.porto.st-001.cp-1.x").status_code == 422


def test_locate_by_ip(porto):
    r = porto.get("/locate/ip/10.31.0.13").json()
    assert [m["hier_id"] for m in r["matches"]] == [
        "pt.porto.st-001", "pt.porto.st-001.cp-1", "pt.porto.st-001.cp-2"]
    assert porto.get("/locate/ip/10.9.9.9").json()["count"] == 0
    assert porto.get("/locate/ip/nope").status_code == 422


def test_quarantine_station_blocks_chargers(porto):
    r = porto.call("POST", "/quarantine/pt.porto.st-001", "porto",
                   json={**QUAR, "incident_id": "inc-1"})
    assert r.status_code == 200 and r.json()["state"] == "quarantined"
    cp = porto.get("/nodes/pt.porto.st-001.cp-1").json()
    assert cp["state"] == "active" and cp["effective_state"] == "quarantined"
    assert porto.get("/nodes/pt.porto").json()["effective_state"] == "active"
    assert porto.get("/locate/pt.porto.st-001.cp-2").json()["matches"][0]["effective_state"] == "quarantined"
    assert [n["hier_id"] for n in porto.get("/quarantine").json()["items"]] == ["pt.porto.st-001"]
    line = next(x for x in porto.log_lines() if x["event"] == "registry.quarantine")
    assert line["incident_id"] == "inc-1" and line["operator"] == "porto" and line["source"] == "test"


def test_quarantine_idempotent_and_release(porto):
    for _ in range(2):
        assert porto.call("POST", "/quarantine/pt.porto.st-001", "admin", json=QUAR).status_code == 200
    assert sum(x["event"] == "registry.quarantine" for x in porto.log_lines()) == 1
    r = porto.call("POST", "/quarantine/pt.porto.st-001/release", "porto", json=QUAR)
    assert r.json()["state"] == "active"
    assert porto.get("/nodes/pt.porto.st-001.cp-1").json()["effective_state"] == "active"
    assert porto.call("POST", "/quarantine/pt.porto.st-001/release", "porto", json=QUAR).status_code == 200
    assert any(x["event"] == "registry.release" for x in porto.log_lines())
    assert porto.get("/quarantine").json()["count"] == 0


def test_quarantine_scope_and_missing(porto):
    porto.put("/nodes/pt.lisboa")
    assert porto.call("POST", "/quarantine/pt.lisboa", "porto", json=QUAR).status_code == 403
    assert porto.call("POST", "/quarantine/pt.porto.st-002", "porto", json=QUAR).status_code == 404
    assert porto.call("POST", "/quarantine/pt.porto", "porto", json={"source": "test"}).status_code == 422


def test_sd_prometheus(porto):
    porto.put("/nodes/pt.porto.edge", "porto", json={"metrics_target": "edge.porto.pt.lab:9100"})
    sd = porto.get("/sd/prometheus", params={"city": "porto", "kind": "station"}).json()
    assert sd == [{"targets": ["st-001.porto.pt.lab:9300"],
                   "labels": {"hier_id": "pt.porto.st-001", "city": "porto", "station": "st-001"}}]
    everything = porto.get("/sd/prometheus").json()
    edge = next(t for t in everything if t["labels"]["hier_id"] == "pt.porto.edge")
    assert edge["labels"] == {"hier_id": "pt.porto.edge", "city": "porto"}
    assert porto.get("/sd/prometheus", params={"city": "lisboa"}).json() == []


def test_health_and_metrics(porto):
    assert porto.get("/health").json() == {"status": "ok", "service": "registry",
                                           "version": "0.1.0", "checks": {"db": "ok"}}
    porto.call("POST", "/quarantine/pt.porto.st-001", "porto", json=QUAR)
    text = porto.get("/metrics").text
    for name in ("evcharge_registry_nodes", "evcharge_registry_station_info",
                 "evcharge_registry_writes_total", "evcharge_registry_quarantines_total"):
        assert f"\n{name}" in "\n" + text, name
    assert 'lat="41.1496"' in text


def test_settings_validation():
    ok = "a" * 16
    env = {"REGISTRY_ADMIN_TOKEN": ok, "REGISTRY_TOKEN_PORTO_ALEGRE": "b" * 16}
    assert Settings.from_env(env).city_tokens == {"porto-alegre": "b" * 16}
    for bad in ({}, {"REGISTRY_ADMIN_TOKEN": "short"},
                {**env, "REGISTRY_TOKEN_PORTO": ok},               # same as admin
                {**env, "REGISTRY_TOKEN_CENTRAL": "c" * 16},       # reserved city name
                {**env, "REGISTRY_TOKEN_PORTO": "short"}):
        with pytest.raises((ConfigError, ValueError)):
            Settings.from_env(bad)
