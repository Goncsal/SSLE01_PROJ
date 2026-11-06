"""The service end to end: API, scoring, dry run, actions, replay."""

import datetime as dt
import json
from pathlib import Path

from conftest import CARD, LISBOA, PORTO, T0, iso
from evcommon.models import FraudAlert, FraudEvent

HERE = Path(__file__).parent


def cross_city(api, minutes=20, card=CARD):
    api.event("session.start", PORTO, T0, card=card, transaction_id=1, meter_start_wh=0)
    return api.event("session.start", LISBOA, T0 + dt.timedelta(minutes=minutes), card=card,
                     transaction_id=2, meter_start_wh=0)


def test_health_metrics_and_auth(api):
    r = api.get("/health", auth=False)
    assert r.status_code == 200 and r.json()["service"] == "fraud"
    text = api.get("/metrics", auth=False).text
    for name in ("events_total", "alerts_total", "actions_total", "high_risk_cards"):
        assert f"# TYPE evcharge_fraud_{name} " in text
    for method, path in (("GET", "/alerts"), ("GET", f"/scores/cards/{CARD}"),
                         ("GET", f"/scores/chargers/{PORTO}"), ("POST", "/events")):
        assert api.call(method, path, auth=False, **({"json": {}} if method == "POST" else {})).status_code == 401
    bad = api.c.get("/alerts", headers={"Authorization": "Bearer wrong-token-wrong-token"})
    assert bad.status_code == 401
    assert [x for x in api.log_lines("api.auth_failed")]


def test_check_order_401_before_422(api):
    junk = {"event": "nope"}
    assert api.call("POST", "/events", auth=False, json=junk).status_code == 401
    assert api.post("/events", junk).status_code == 422
    assert api.get("/scores/cards/not-a-card").status_code == 422
    assert api.get("/scores/chargers/not.valid").status_code == 422
    assert api.get("/alerts", params={"limit": 0}).status_code == 422


def test_event_accepted_and_metric_counted(api):
    r = api.event("session.start", PORTO, T0, card=CARD, transaction_id=1)
    assert r.status_code == 202 and r.json() == {"accepted": True}
    assert 'evcharge_fraud_events_total{event="session.start"}' in api.get("/metrics").text


def test_csms_event_shape_validates(api):
    """The events the CSMS sender builds (csms/src/evcsms/chargepoint.py) carry extra fields."""
    ev = FraudEvent(event="meter.anomaly", ts=T0, hier_id=PORTO, city="porto", station="st-001",
                    max_kw=22.0, srcip="10.0.0.9", card=CARD, transaction_id=5, reason="backwards",
                    meter_wh=900, prev_meter_wh=1200, power_w=-3.0)
    r = api.post("/events", ev.model_dump(mode="json"))
    assert r.status_code == 202
    stop = FraudEvent(event="session.stop", ts=T0, hier_id=PORTO, city="porto", station="st-001",
                      max_kw=22.0, card=CARD, transaction_id=5, meter_start_wh=0, meter_stop_wh=900,
                      energy_wh=900, duration_s=600.0, peak_power_w=5400.0)
    assert api.post("/events", stop.model_dump(mode="json")).status_code == 202


def test_single_cross_city_start_does_not_fire(api):
    """ConcurrentTx-style traffic: one start in another city, no earlier session, is normal."""
    assert api.event("session.start", LISBOA, T0, card=CARD, transaction_id=1).status_code == 202
    assert api.alerts() == []
    # a plausible trip (3 hours, 274 km) is not fraud either
    api.event("session.start", PORTO, T0 + dt.timedelta(hours=3), card=CARD, transaction_id=2)
    assert api.alerts() == []


def test_impossible_travel_alert_score_and_log(api, stubs):
    assert cross_city(api).status_code == 202
    (a,) = api.alerts()
    FraudAlert.model_validate(a)
    assert a["rule"] == "impossible_travel" and a["severity"] == "critical"
    assert a["card"] == CARD and a["hier_id"] == LISBOA and a["other_hier_id"] == PORTO
    assert a["evidence"]["distance_km"] > 270 and a["evidence"]["speed_kmh"] > 200
    s = api.get(f"/scores/cards/{CARD}").json()
    assert s["card"] == CARD and s["score"] >= 0.7 and s["rules"] == ["impossible_travel"]
    assert s["last_seen"].endswith("Z")
    assert api.get(f"/scores/chargers/{LISBOA}").json()["rules"] == ["impossible_travel"]
    assert api.get(f"/scores/chargers/{PORTO}").json()["rules"] == ["impossible_travel"]
    other = api.get("/scores/cards/04FFFFFFFFFFFF").json()
    assert other["score"] == 0 and other["rules"] == [] and other["last_seen"] is None
    line = api.log_lines("fraud.alert")[0]
    assert line["rule"] == "impossible_travel" and line["card"] == CARD
    assert line["distance_km"] > 270 and line["speed_kmh"] > 200
    assert 'evcharge_fraud_alerts_total{city="lisboa",rule="impossible_travel"}' in api.get("/metrics").text
    assert "evcharge_fraud_high_risk_cards 1.0" in api.get("/metrics").text


def test_repeat_is_not_re_alerted(api):
    cross_city(api)
    api.event("session.start", LISBOA, T0 + dt.timedelta(minutes=25), card=CARD, transaction_id=3)
    assert len(api.alerts()) == 1


def test_alert_filters(api):
    cross_city(api)
    cross_city(api, card="04B1B2C3D4E5F6")
    assert len(api.alerts()) == 2
    assert len(api.alerts(card=CARD)) == 1
    assert len(api.alerts(hier_id=LISBOA)) == 2
    assert api.alerts(hier_id=PORTO) == []
    assert len(api.alerts(limit=1)) == 1
    assert api.alerts(since="2030-01-01T00:00:00Z") == []
    assert len(api.alerts(since=iso(T0))) == 2


def test_dry_run_calls_nothing_and_logs_the_intent(make_api, stubs):
    api = make_api(dry_run=True)
    cross_city(api)
    api.event("session.stop", PORTO, T0 + dt.timedelta(minutes=30), card=CARD, transaction_id=1,
              energy_wh=50000, duration_s=600.0)
    assert {a["rule"] for a in api.alerts()} == {"impossible_travel", "over_energy"}
    assert stubs["billing"].requests == [] and stubs["csms"].requests == []
    assert stubs["correlator"].requests == []
    assert stubs["registry"].posts() == []                    # registry only served coordinates
    actions = api.log_lines("fraud.action")
    assert {(x["rule"], x["result"]) for x in actions} == {("impossible_travel", "dry_run"),
                                                          ("over_energy", "dry_run")}
    block = [x for x in actions if x["rule"] == "impossible_travel"][0]
    assert block["target"] == CARD and block["action"] == "block_card"
    alerts_logged = api.log_lines("fraud.alert")
    assert len(alerts_logged) == 2
    assert 'evcharge_fraud_actions_total{action="block_card"}' in api.get("/metrics").text


def test_live_block_card_uses_the_responder_token(api, stubs):
    cross_city(api)
    posts = stubs["billing"].posts()
    assert [p["path"] for p in posts] == [f"/cards/{CARD}/block", "/sessions/1/flag", "/sessions/2/flag"]
    for p in posts:
        assert p["auth"] == "Bearer billing-responder-token"
        assert p["body"]["source"] == "fraud" and "impossible_travel" in p["body"]["reason"]
    assert stubs["registry"].posts() == [] and stubs["csms"].posts() == []
    sig = stubs["correlator"].posts()
    assert [p["path"] for p in sig] == ["/signals/fraud"]
    assert sig[0]["auth"] == "Bearer correlator-api-token-x"
    FraudAlert.model_validate(sig[0]["body"])
    done = {(x["action"], x["result"]) for x in api.log_lines("fraud.action")}
    assert ("block_card", "ok") in done and ("flag_session", "ok") in done


def test_live_over_energy_and_backwards_flag_the_session(api, stubs):
    api.event("session.stop", PORTO, T0, card=CARD, transaction_id=9, energy_wh=90000, duration_s=600.0)
    api.event("meter.anomaly", PORTO, T0 + dt.timedelta(minutes=1), card=CARD, transaction_id=10,
              reason="backwards")
    posts = stubs["billing"].posts()
    assert [p["path"] for p in posts] == ["/sessions/9/flag", "/sessions/10/flag"]
    assert all(p["auth"] == "Bearer billing-responder-token" for p in posts)
    assert stubs["correlator"].requests == []                 # warnings are not sent on


def test_live_grid_gap_quarantines_station_and_isolates_chargers(make_api, stubs):
    stubs["csms"].connections = [{"hier_id": "pt.porto.st-001.cp-1"}, {"hier_id": "pt.porto.st-001.cp-2"}]
    api = make_api()
    station = "pt.porto.st-001"
    api.event("session.stop", PORTO, T0, card=CARD, transaction_id=1, energy_wh=2000, duration_s=1800.0)
    assert api.alerts() == []
    # the feeder meter measured 10 kW for an hour = 10 kWh, the CSMS saw 2 kWh
    raised = api.scorer.record_grid_sample(station, "porto", 10000.0, 3600.0, T0 + dt.timedelta(hours=1))
    assert [r.alert.rule for r in raised] == ["grid_gap"]
    api.app.state.act(raised)
    (q,) = stubs["registry"].posts()
    assert q["path"] == f"/quarantine/{station}" and q["auth"] == "Bearer registry-responder-token"
    assert q["body"]["source"] == "fraud"
    cmds = [(p["path"], p["auth"]) for p in stubs["csms"].posts()]
    assert cmds == [(f"/commands/pt.porto.st-001.cp-{n}/{c}", "Bearer csms-responder-token")
                    for n in (1, 2) for c in ("change-availability", "disconnect")]
    assert stubs["csms"].posts()[0]["body"]["type"] == "Inoperative"
    assert stubs["csms"].posts()[0]["body"]["connector_id"] == 0
    assert any(p["path"] == "/signals/fraud" for p in stubs["correlator"].posts())
    assert api.scorer.charger_score(PORTO)["rules"] == ["grid_gap"]    # via its station


def test_grid_gap_quiet_when_reported_matches(api, stubs):
    api.event("session.stop", PORTO, T0, card=CARD, transaction_id=1, energy_wh=9000, duration_s=1800.0)
    raised = api.scorer.record_grid_sample("pt.porto.st-001", "porto", 10000.0, 3600.0,
                                           T0 + dt.timedelta(hours=1))
    assert raised == []


def test_peer_deviation_end_to_end(api):
    for i in range(20):                                   # peers: other stations in Porto
        api.event("session.stop", f"pt.porto.st-00{2 + i % 2}.cp-1", T0 + dt.timedelta(minutes=i),
                  card=CARD, transaction_id=100 + i, energy_wh=10000, duration_s=3600.0)
    for i in range(3):
        api.event("session.stop", PORTO, T0 + dt.timedelta(minutes=30 + i), card=CARD,
                  transaction_id=200 + i, energy_wh=3000, duration_s=3600.0)
    (a,) = api.alerts()
    assert a["rule"] == "peer_deviation" and a["hier_id"] == "pt.porto.st-001"
    assert a["evidence"]["peer_sessions"] == 20


def test_peer_deviation_needs_min_sessions_end_to_end(api):
    for i in range(19):
        api.event("session.stop", f"pt.porto.st-002.cp-1", T0 + dt.timedelta(minutes=i), card=CARD,
                  transaction_id=100 + i, energy_wh=10000, duration_s=3600.0)
    for i in range(4):
        api.event("session.stop", PORTO, T0 + dt.timedelta(minutes=30 + i), card=CARD,
                  transaction_id=200 + i, energy_wh=1000, duration_s=3600.0)
    assert api.alerts() == []


def test_card_velocity_end_to_end(api):
    for i in range(9):
        api.event("session.start", PORTO, T0 + dt.timedelta(minutes=i), card=CARD, transaction_id=i + 1)
    (a,) = api.alerts()
    assert a["rule"] == "card_velocity" and a["card"] == CARD and a["evidence"]["sessions_last_hour"] == 9


def test_unreachable_billing_is_logged_not_fatal(make_api, stubs):
    api = make_api(billing_url="http://127.0.0.1:9")      # nothing listens on the discard port
    assert cross_city(api).status_code == 202
    assert len(api.alerts()) == 1
    assert any(x["result"] == "error" for x in api.log_lines("fraud.action"))
    assert api.log_lines("dependency.error")


def test_missing_peer_config_is_reported(make_api):
    api = make_api(billing_url="", registry_url="", correlator_url="")
    cross_city(api)                                       # no registry: no coordinates, no alert
    assert api.alerts() == []


def test_action_rate_limit(make_api, stubs):
    api = make_api(max_actions_h=1)
    cross_city(api, card="04A1B2C3D4E5F6")
    cross_city(api, card="04B1B2C3D4E5F6")
    assert len(api.alerts()) == 2
    blocks = [p for p in stubs["billing"].posts() if p["path"].endswith("/block")]
    assert len(blocks) == 1
    assert any(x["result"] == "rate_limited" for x in api.log_lines("fraud.action"))


def test_replay_of_recorded_events(api, stubs):
    for line in (HERE / "events.jsonl").read_text().splitlines():
        assert api.post("/events", json.loads(line)).status_code == 202
    got = sorted((a["rule"], a["card"], a["hier_id"]) for a in api.alerts())
    assert got == sorted([
        ("impossible_travel", "04A1B2C3D4E5F6", "pt.lisboa.st-001.cp-1"),
        ("over_energy", "04A1B2C3D4E5F6", "pt.porto.st-001.cp-1"),
        ("meter_backwards", "E3EE5BE2ED4120", "pt.aveiro.st-001.cp-1"),
    ])
    flagged = sorted(p["path"] for p in stubs["billing"].posts() if "/flag" in p["path"])
    assert "/sessions/103/flag" in flagged and "/sessions/105/flag" in flagged
    assert f"/cards/04A1B2C3D4E5F6/block" in [p["path"] for p in stubs["billing"].posts()]
