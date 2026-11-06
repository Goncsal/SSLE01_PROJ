import json
from pathlib import Path

import pytest
from conftest import build, iso, ts
from evcorrelator import normalise
from evcorrelator.engine import Engine

CHARGER = "pt.porto.st-003.cp-2"
STATION = "pt.porto.st-003"
CARD = "04A1B2C3D4E5F6"
ROGUE_IP = "198.51.100.10"          # not in the registry: an attacker's address


# ---- hijack: Prometheus + Wazuh ------------------------------------------------------

def test_prometheus_and_wazuh_join_into_one_hijack_incident(api):
    assert api.prom("DuplicateChargerID", STATION, "hijack", at=0, severity="critical").status_code == 202
    api.clock.set(4)
    assert api.wazuh("ident.ip_mismatch", "110104", 7, "hijack", at=3, hier_id=STATION, srcip=ROGUE_IP).status_code == 202
    # the Wazuh signal is about the charger, the Prometheus alert about its station (same branch)
    api.wazuh("charger.duplicate_id", "110100", 10, "hijack", at=5, hier_id=CHARGER, srcip=ROGUE_IP)
    (inc,) = api.incidents()
    assert inc["kind"] == "hijack" and inc["status"] == "open" and inc["severity"] == "critical"
    assert {s["source"] for s in inc["signals"]} == {"prometheus", "wazuh"}
    assert inc["root_cause"]["hier_id"] == STATION          # lowest common element of station + charger
    assert inc["t_located"] is not None
    assert inc["keys"]["srcip"] == [ROGUE_IP]


def test_same_charger_gives_the_charger_as_root_cause(api):
    api.wazuh("charger.duplicate_id", "110100", 10, "hijack", at=0, hier_id=CHARGER, srcip=ROGUE_IP)
    api.clock.set(2)
    api.wazuh("ident.ip_mismatch", "110104", 7, "hijack", at=1, hier_id=CHARGER, srcip=ROGUE_IP)
    (inc,) = api.incidents()
    rc = inc["root_cause"]
    assert (rc["key_type"], rc["key"], rc["hier_id"]) == ("hier_id", CHARGER, CHARGER)
    assert rc["host"] == "porto-st-003" and rc["edge"] == "pt.porto.edge" and rc["city"] == "porto"
    assert ROGUE_IP in rc["explanation"] and "not in the registry" in rc["explanation"]
    assert inc["t_located"] == ts(0)           # clock still at T0 when the first signal was walked


# ---- clock skew ------------------------------------------------------------------------

@pytest.mark.parametrize("gap,joined", [(122, True), (123, True), (124, False)])
def test_skew_widens_the_window(gap, joined):
    api = build(window="120", skew="3")
    api.wazuh("charger.duplicate_id", "110100", 10, "hijack", at=0, hier_id=CHARGER)
    api.wazuh("ident.ip_mismatch", "110104", 7, "hijack", at=gap, hier_id=CHARGER)
    assert len(api.incidents()) == (1 if joined else 2)


def test_without_skew_the_edge_case_splits():
    api = build(window="120", skew="0")
    api.wazuh("charger.duplicate_id", "110100", 10, "hijack", at=0, hier_id=CHARGER)
    api.wazuh("ident.ip_mismatch", "110104", 7, "hijack", at=122, hier_id=CHARGER)
    assert len(api.incidents()) == 2


# ---- transitive keys ------------------------------------------------------------------

def test_srcip_only_signals_bind_through_an_earlier_pair(api):
    api.wazuh("charger.duplicate_id", "110100", 10, "hijack", at=0, hier_id=CHARGER, srcip=ROGUE_IP)
    # srcip only: no hier_id at all
    api.wazuh("netwatch.scan", "110305", 7, "hijack", at=60, srcip=ROGUE_IP)
    api.wazuh("netwatch.scan", "110306", 7, "hijack", at=100, srcip=ROGUE_IP)   # 100 s from the first only
    (inc,) = api.incidents()
    assert len(inc["signals"]) == 3
    assert inc["keys"] == {"hier_id": [CHARGER], "srcip": [ROGUE_IP]}


def test_incidents_that_a_later_signal_connects_are_merged(api):
    api.wazuh("a", "110101", 10, "hijack", at=0, hier_id=CHARGER)
    api.wazuh("b", "110102", 8, "hijack", at=1, srcip=ROGUE_IP)
    assert len(api.incidents()) == 2
    api.wazuh("c", "110103", 10, "hijack", at=2, hier_id=CHARGER, srcip=ROGUE_IP)
    (inc,) = api.incidents()
    assert len(inc["signals"]) == 3


# ---- fraud ------------------------------------------------------------------------------

def test_impossible_travel_names_the_card_not_a_charger(api):
    other = "pt.lisboa.st-001.cp-1"
    assert api.fraud("impossible_travel", at=0, card=CARD, hier_id=CHARGER, other_hier_id=other,
                     city="lisboa").status_code == 202
    api.clock.set(1)
    api.wazuh("fraud.alert", "110207", 10, "fraud", at=1, card=CARD, hier_id=other)
    (inc,) = api.incidents()
    rc = inc["root_cause"]
    assert inc["kind"] == "fraud"
    assert (rc["key_type"], rc["key"]) == ("card", CARD)
    assert rc["hier_id"] is None and "2 chargers" in rc["explanation"]
    assert inc["t_located"] is not None


def test_fraud_on_one_charger_stays_on_the_charger(api):
    api.fraud("grid_gap", at=0, hier_id=STATION, severity="critical")
    (inc,) = api.incidents()
    assert inc["root_cause"]["hier_id"] == STATION


# ---- botnet ------------------------------------------------------------------------------

def test_signals_of_several_stations_point_at_the_city(api):
    for n, st in enumerate(("pt.porto.st-001.cp-1", "pt.porto.st-002.cp-1", "pt.porto.st-003.cp-1")):
        api.clock.set(n * 5)
        api.wazuh("ocpp.rate_limited", "110302", 12, "botnet", at=n * 5, hier_id=st, city="porto", wid=f"w{n}")
    (inc,) = api.incidents()
    rc = inc["root_cause"]
    assert inc["kind"] == "botnet" and rc["hier_id"] == "pt.porto" and rc["city"] == "porto"
    assert "lowest common element is pt.porto (city)" in rc["explanation"]


def test_city_alert_joins_its_stations_of_the_same_kind_only(api):
    api.prom("CityLoadSpike", "pt.porto", "botnet", at=0)
    api.wazuh("session.start", "110308", 10, "botnet", at=3, hier_id="pt.porto.st-002.cp-1", city="porto")
    api.wazuh("ident.ip_mismatch", "110104", 7, "hijack", at=4, hier_id="pt.porto.st-002.cp-2")
    kinds = sorted(i["kind"] for i in api.incidents())
    assert kinds == ["botnet", "hijack"]


def test_botnets_in_two_cities_stay_apart(api):
    api.wazuh("ocpp.rate_limited", "110302", 12, "botnet", at=0, hier_id="pt.porto.st-001.cp-1", city="porto")
    api.wazuh("ocpp.rate_limited", "110302", 12, "botnet", at=1, hier_id="pt.lisboa.st-001.cp-1", city="lisboa")
    assert len(api.incidents()) == 2


# ---- idempotency ---------------------------------------------------------------------------

def test_repeated_alert_updates_the_signal_instead_of_opening_an_incident(api):
    for at in (0, 30, 60):
        api.prom("DuplicateChargerID", STATION, "hijack", at=at)
    api.wazuh("charger.duplicate_id", "110100", 10, "hijack", at=1, hier_id=STATION, wid="a")
    api.wazuh("charger.duplicate_id", "110100", 10, "hijack", at=2, hier_id=STATION, wid="b")
    (inc,) = api.incidents()
    by_source = {s["source"]: s for s in inc["signals"]}
    assert len(inc["signals"]) == 2
    assert by_source["prometheus"]["count"] == 3 and by_source["prometheus"]["last_ts"] == ts(60)
    assert by_source["wazuh"]["count"] == 2


# ---- mitigation ---------------------------------------------------------------------------------

def test_response_signal_sets_t_mitigated(api):
    api.wazuh("charger.duplicate_id", "110100", 10, "hijack", at=0, hier_id=CHARGER)
    api.wazuh("ar.action", "110410", 5, "infra", at=9, hier_id=CHARGER, result="ok", ar_command="quarantine-charger")
    (inc,) = api.incidents()
    assert inc["status"] == "mitigated" and inc["t_mitigated"] == ts(9)
    assert inc["kind"] == "hijack"          # the response does not change the kind


def test_registry_quarantine_and_card_block_are_responses(api):
    api.fraud("impossible_travel", at=0, card=CARD, hier_id=CHARGER, other_hier_id="pt.lisboa.st-001.cp-1")
    api.wazuh("billing.card_blocked", "110209", 5, "fraud", at=4, card=CARD)
    (inc,) = api.incidents()
    assert inc["t_mitigated"] == ts(4)
    api.wazuh("registry.quarantine", "110405", 4, "infra", at=1000, hier_id=CHARGER)   # far too late: no incident
    assert len(api.incidents()) == 1


def test_a_response_alone_opens_nothing_and_a_failed_one_is_not_a_mitigation(api):
    api.wazuh("ar.action", "110410", 5, "infra", at=0, hier_id=CHARGER, result="ok")
    assert api.incidents() == []
    api.wazuh("charger.duplicate_id", "110100", 10, "hijack", at=1, hier_id=CHARGER)
    api.wazuh("ar.action", "110411", 10, "infra", at=2, hier_id=CHARGER, result="error")
    (inc,) = api.incidents()
    assert inc["t_mitigated"] is None


def test_mitigated_endpoint_and_close_endpoint(api):
    api.wazuh("charger.duplicate_id", "110100", 10, "hijack", at=0, hier_id=CHARGER)
    iid = api.incidents()[0]["incident_id"]
    r = api.post(f"/incidents/{iid}/mitigated", {"action": "manual quarantine", "by": "noc", "ts": iso(30)})
    assert r.status_code == 200 and r.json()["t_mitigated"] == ts(30) and r.json()["status"] == "mitigated"
    again = api.post(f"/incidents/{iid}/mitigated", {"action": "other", "by": "noc", "ts": iso(50)})
    assert again.json()["t_mitigated"] == ts(30)                        # set only if unset
    closed = api.post(f"/incidents/{iid}/close", {"by": "noc", "note": "done"})
    assert closed.json()["status"] == "closed" and closed.json()["t_closed"] is not None
    assert api.get(f"/incidents/{iid}").json()["status"] == "closed"
    assert api.post(f"/incidents/{iid}/mitigated", {"action": "x", "by": "noc"}).status_code == 409
    assert api.post("/incidents/not-a-uuid/close", {"by": "x"}).status_code == 422
    assert api.post("/incidents/00000000-0000-4000-8000-000000000000/close", {"by": "x"}).status_code == 404


# ---- auto-close --------------------------------------------------------------------------------------

def test_incident_closes_after_the_quiet_time(make_api):
    api = make_api(close="600")
    api.wazuh("charger.duplicate_id", "110100", 10, "hijack", at=0, hier_id=CHARGER)
    api.clock.set(100)
    api.wazuh("ident.ip_mismatch", "110104", 7, "hijack", at=100, hier_id=CHARGER)     # restarts the quiet time
    api.clock.set(699)
    assert api.incidents(status="open")[0]["status"] == "open"
    api.clock.set(701)
    assert api.incidents(status="open") == []
    (inc,) = api.incidents(status="closed")
    assert inc["t_closed"] == ts(701)
    # a new signal for the same charger now opens a new incident
    api.wazuh("charger.duplicate_id", "110100", 10, "hijack", at=701, hier_id=CHARGER, wid="new")
    assert len(api.incidents()) == 2


# ---- auth and input handling ------------------------------------------------------------------------------

@pytest.mark.parametrize("method,path", [("get", "/incidents"), ("post", "/signals/wazuh"),
                                         ("post", "/signals/fraud"), ("post", "/signals/alertmanager"),
                                         ("get", "/incidents/x"), ("post", "/incidents/x/close")])
def test_401_without_or_with_a_wrong_token(api, method, path):
    for token in (False, "wrong-token-wrong-token"):
        if method == "get":
            r = api.c.get(path, headers={"Authorization": f"Bearer {token}"} if token else {})
        else:
            r = api.c.post(path, json={}, headers={"Authorization": f"Bearer {token}"} if token else {})
        assert r.status_code == 401


def test_health_and_metrics_need_no_token(api):
    assert api.c.get("/health").json()["service"] == "correlator"
    assert "evcharge_correlator_signals_total" in api.c.get("/metrics").text


def test_garbage_is_tolerated(api):
    assert api.post("/signals/alertmanager", {"alerts": "nope"}).json() == {"accepted": 0}
    assert api.post("/signals/alertmanager", [1, 2]).json() == {"accepted": 0}
    assert api.post("/signals/wazuh", {"rule": 5, "data": {"hier_id": "pt.x; DROP", "srcip": "999.1.1.1"}}).json() == {"accepted": 0}
    assert api.post("/signals/fraud", {"alert_id": "x"}).status_code == 422
    assert api.c.post("/signals/wazuh", content=b"{not json", headers={"Authorization": f"Bearer {api.token}"}).status_code == 422
    # unknown fields are ignored, resolved alerts are not evidence
    ok = api.wazuh("ident.ip_mismatch", "110104", 7, "hijack", hier_id=CHARGER, surprise={"a": [1] * 500})
    assert ok.json() == {"accepted": 1}
    resolved = {"alerts": [{"status": "resolved", "labels": {"alertname": "X", "hier_id": CHARGER}}]}
    assert api.post("/signals/alertmanager", resolved).json() == {"accepted": 0}


def test_registry_down_gives_a_derived_root_cause_without_t_located(api, monkeypatch):
    monkeypatch.setattr('evcorrelator.locate.ERROR_TTL_S', 0.0)       # do not cache the failure
    api.registry.down = True
    api.wazuh("charger.duplicate_id", "110100", 10, "hijack", at=0, hier_id=CHARGER)
    (inc,) = api.incidents()
    assert inc["root_cause"]["hier_id"] == CHARGER and "derived from the ids" in inc["root_cause"]["explanation"]
    assert inc["t_located"] is None
    api.registry.down = False
    api.clock.set(10)
    api.wazuh("ident.ip_mismatch", "110104", 7, "hijack", at=10, hier_id=CHARGER)
    api.wazuh("ident.cert_mismatch", "110101", 10, "hijack", at=11, hier_id=CHARGER)
    assert api.incidents()[0]["t_located"] is not None


def test_normalise_prefers_the_log_time_and_validates_fields():
    sig = normalise.from_wazuh({"timestamp": "2026-11-06T10:00:09.000+0000", "id": "1",
                                "rule": {"id": "110104", "level": 7, "groups": ["evcharge", "hijack"]},
                                "data": {"event": "ident.ip_mismatch", "ts": "2026-11-06T10:00:00.250Z",
                                         "hier_id": CHARGER, "srcip": "10.0.0.5", "card": "zz"}})
    assert sig.ts.isoformat() == "2026-11-06T10:00:00.250000+00:00"
    assert (sig.key_type, sig.card, sig.kind, sig.severity) == ("hier_id", None, "hijack", "warning")


def test_history_keeps_closed_incidents(tmp_path, monkeypatch):
    pytest.importorskip("sqlalchemy")
    import httpx
    from conftest import Api, Clock, FakeRegistry
    from evcorrelator.app import create_app
    from evcorrelator.locate import Locator
    from evcorrelator.settings import Settings
    from fastapi.testclient import TestClient
    token = "t" * 24
    settings = Settings.from_env({"CORRELATOR_API_TOKEN": token, "REGISTRY_URL": "http://r.test",
                                  "DATABASE_URL": f"sqlite:///{tmp_path}/c.db"})
    reg, clock = FakeRegistry(), Clock()
    loc = Locator("http://r.test", client=httpx.Client(base_url="http://r.test", transport=httpx.MockTransport(reg)))
    api = Api(TestClient(create_app(settings, locator=loc, clock=clock)), token, reg, clock, settings)
    api.wazuh("charger.duplicate_id", "110100", 10, "hijack", at=0, hier_id=CHARGER)
    iid = api.incidents()[0]["incident_id"]
    api.post(f"/incidents/{iid}/close", {"by": "noc"})
    api.c.app.state.engine._closed.clear()               # forget the in-memory copy
    assert api.get(f"/incidents/{iid}").json()["status"] == "closed"
    assert [i["incident_id"] for i in api.incidents(status="closed")] == [iid]


# ---- replay of a mixed stream -----------------------------------------------------------------------------------

def test_replay_of_a_botnet_stream_yields_one_incident(api):
    """tests/signals.jsonl: Prometheus, Wazuh and a fraud alert of one botnet, then the response.
    The clock follows the stream (each line has `at`, seconds from T0, and `via`)."""
    lines = [json.loads(x) for x in (Path(__file__).parent / "signals.jsonl").read_text().splitlines() if x.strip()]
    for line in lines:
        api.clock.set(line["at"])
        r = api.post(f"/signals/{line['via']}", line["body"])
        assert r.status_code == 202, line
    (inc,) = api.incidents()
    assert inc["kind"] == "botnet" and inc["severity"] == "critical"
    assert inc["root_cause"]["hier_id"] == "pt.porto" and inc["root_cause"]["key_type"] == "hier_id"
    locate_s = (api.clock.now.__class__.fromisoformat(inc["t_located"].replace("Z", "+00:00"))
                - api.clock.now.__class__.fromisoformat(inc["t_first_signal"].replace("Z", "+00:00"))).total_seconds()
    assert 0 <= locate_s <= 10, locate_s
    assert inc["t_mitigated"] is not None and inc["status"] == "mitigated"
    assert {s["source"] for s in inc["signals"]} == {"prometheus", "wazuh"}
    metrics = api.c.get("/metrics").text
    assert "evcharge_correlator_time_to_locate_seconds_count" in metrics
    print(f"replay: located {locate_s:.1f} s after the first signal")


def test_engine_rejects_nothing_silently_when_over_capacity():
    # a smoke test of the engine alone, without HTTP
    eng = Engine(123, 600)
    assert eng.list() == []
