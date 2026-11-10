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


def sequential(api, minutes, card=CARD):
    """Porto session that stopped, then a Lisboa start `minutes` after the Porto start."""
    api.event("session.start", PORTO, T0, card=card, transaction_id=1, meter_start_wh=0)
    api.event("session.stop", PORTO, T0 + dt.timedelta(minutes=5), card=card, transaction_id=1,
              energy_wh=1000, duration_s=300.0)
    return api.event("session.start", LISBOA, T0 + dt.timedelta(minutes=minutes), card=card,
                     transaction_id=2, meter_start_wh=0)


def blocks(stubs):
    return [p for p in stubs["billing"].posts() if p["path"].endswith("/block")]


def test_201_kmh_roaming_alerts_but_does_not_block(api, stubs):
    minutes = 274.5 / 201 * 60                      # Porto -> Lisboa at about 201 km/h
    assert sequential(api, minutes).status_code == 202
    (a,) = api.alerts()
    assert a["rule"] == "impossible_travel" and 200 < a["evidence"]["speed_kmh"] < 215
    assert a["evidence"]["high_confidence"] is False and a["evidence"]["concurrent"] is False
    assert blocks(stubs) == [] and stubs["billing"].posts() == []
    (line,) = api.log_lines("fraud.alert")
    assert line["auto_action"] == "none" and line["dry_run"] is False
    assert api.log_lines("fraud.action") == []


def test_concurrent_sessions_block_even_when_slow(api, stubs):
    # 3 h apart (91 km/h, no alert on speed alone), but the Porto session never stopped
    api2 = api
    api2.event("session.start", PORTO, T0, card=CARD, transaction_id=1, meter_start_wh=0)
    api2.event("session.start", LISBOA, T0 + dt.timedelta(minutes=50), card=CARD, transaction_id=2)
    (a,) = api.alerts()
    assert a["evidence"]["concurrent"] is True and a["evidence"]["speed_kmh"] > 200
    assert [p["path"] for p in blocks(stubs)] == [f"/cards/{CARD}/block"]
    assert api.log_lines("fraud.alert")[0]["auto_action"] == "block_card"


def test_speed_at_block_kmh_blocks_without_overlap(api, stubs):
    assert sequential(api, 274.5 / 450 * 60).status_code == 202       # about 450 km/h, stopped before
    (a,) = api.alerts()
    assert a["evidence"]["concurrent"] is False and a["evidence"]["high_confidence"] is True
    assert [p["path"] for p in blocks(stubs)] == [f"/cards/{CARD}/block"]


def test_alert_only_hit_does_not_mute_a_later_block(api, stubs):
    """A 201 km/h alert for a card must not suppress the block when the clone follows."""
    sequential(api, 274.5 / 201 * 60)
    assert blocks(stubs) == []
    api.event("session.start", "pt.aveiro.st-001.cp-1", T0 + dt.timedelta(minutes=95), card=CARD,
              transaction_id=3)                              # tx 2 (Lisboa) still open: overlap
    assert len(blocks(stubs)) == 1


def test_dry_run_alert_line_carries_the_flag(make_api, stubs):
    api = make_api(dry_run=True)
    sequential(api, 274.5 / 450 * 60)
    (line,) = api.log_lines("fraud.alert")
    assert line["dry_run"] is True and line["auto_action"] == "block_card"
    assert stubs["billing"].requests == []
    assert {x["result"] for x in api.log_lines("fraud.action")} == {"dry_run"}


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


STATION = "pt.porto.st-001"


def power_run(api, feeder, gap, n=24, start=0, station=STATION, city="porto"):
    """n polls, 30 s apart, of a station charging at `feeder` W with `gap` W unreported."""
    out = []
    for i in range(n):
        out += api.scorer.record_grid_sample(station, city, feeder, 30.0,
                                             T0 + dt.timedelta(seconds=30 * (start + i)), gap_w=gap)
    return out


def test_live_power_gap_quarantines_flags_sessions_and_isolates_chargers(make_api, stubs):
    stubs["csms"].connections = [{"hier_id": "pt.porto.st-001.cp-1"}, {"hier_id": "pt.porto.st-001.cp-2"}]
    api = make_api()
    api.event("session.start", PORTO, T0 - dt.timedelta(minutes=30), card=CARD, transaction_id=41)
    api.event("session.start", "pt.porto.st-001.cp-2", T0 - dt.timedelta(minutes=20),
              card="04B1B2C3D4E5F6", transaction_id=42)
    api.event("session.start", "pt.porto.st-002.cp-1", T0 - dt.timedelta(minutes=20),
              card="04C1B2C3D4E5F6", transaction_id=43)              # another station: untouched
    raised = power_run(api, 12000.0, 6000.0)                          # feeder 2x the reported power
    assert [r.alert.rule for r in raised] == ["power_gap"]
    api.app.state.act(raised)
    q = [p for p in stubs["registry"].posts() if p["path"].startswith("/quarantine/")]
    assert [x["path"] for x in q] == [f"/quarantine/{STATION}"]
    assert q[0]["auth"] == "Bearer registry-responder-token" and q[0]["body"]["source"] == "fraud"
    cmds = [(p["path"], p["auth"]) for p in stubs["csms"].posts()]
    assert cmds == [(f"/commands/pt.porto.st-001.cp-{n}/{c}", "Bearer csms-responder-token")
                    for n in (1, 2) for c in ("change-availability", "disconnect")]
    assert stubs["csms"].posts()[0]["body"]["type"] == "Inoperative"
    # the station's recent sessions are flagged for billing review, with the responder token
    flags = [p for p in stubs["billing"].posts() if "/flag" in p["path"]]
    assert sorted(p["path"] for p in flags) == ["/sessions/41/flag", "/sessions/42/flag"]
    assert all(p["auth"] == "Bearer billing-responder-token" and p["body"]["source"] == "fraud"
               for p in flags)
    assert any(p["path"] == "/signals/fraud" for p in stubs["correlator"].posts())
    assert api.scorer.charger_score(PORTO)["rules"] == ["power_gap"]     # via its station
    (line,) = api.log_lines("fraud.alert")
    assert line["rule"] == "power_gap" and line["auto_action"] == "quarantine" and line["dry_run"] is False


def test_dry_run_power_gap_logs_the_intent_and_calls_nothing(make_api, stubs):
    api = make_api(dry_run=True)
    api.event("session.start", PORTO, T0 - dt.timedelta(minutes=30), card=CARD, transaction_id=41)
    raised = power_run(api, 12000.0, 6000.0)
    api.app.state.act(raised)
    assert stubs["registry"].posts() == [] and stubs["csms"].requests == []
    assert [p for p in stubs["billing"].posts()] == [] and stubs["correlator"].requests == []
    (line,) = api.log_lines("fraud.alert")
    assert line["dry_run"] is True and line["auto_action"] == "quarantine"
    assert {(x["action"], x["result"]) for x in api.log_lines("fraud.action")} == {("quarantine", "dry_run")}


def test_honest_variance_station_is_never_quarantined(api, stubs):
    """Cumulative ratio 0.6 (like a live honest station) but feeder == reported in real time."""
    api.event("session.stop", PORTO, T0, card=CARD, transaction_id=1, energy_wh=2000, duration_s=1800.0)
    api.event("session.stop", PORTO, T0 + dt.timedelta(minutes=1), card=CARD, transaction_id=2,
              energy_wh=1000, duration_s=1800.0)
    raised = power_run(api, 12000.0, 0.0, n=240)                      # two hours of charging
    api.app.state.act(raised)
    assert not [r for r in raised if r.finding.action]                # nothing asks for an action
    assert [p for p in stubs["registry"].posts() if p["path"].startswith("/quarantine")] == []
    assert stubs["csms"].posts() == []
    # the cumulative ratio may still ALERT (warning), but it never isolates anything
    assert {a["rule"] for a in api.alerts()} <= {"grid_gap"}
    assert all(a["severity"] == "warning" for a in api.alerts())


def test_power_gap_needs_the_gap_to_last(api, stubs):
    raised = power_run(api, 12000.0, 6000.0, n=6)                     # 3 minutes: not enough samples
    assert raised == []
    assert power_run(api, 12000.0, 0.0, n=40, start=6) == []          # then it closes: never fires
    assert api.scorer.record_grid_sample(STATION, "porto", 12000.0, 30.0,
                                         T0 + dt.timedelta(hours=1), gap_w=None) == []   # no gap series


def test_grid_gap_quiet_when_reported_matches(api, stubs):
    api.event("session.stop", PORTO, T0, card=CARD, transaction_id=1, energy_wh=9000, duration_s=1800.0)
    api.event("session.stop", PORTO, T0 + dt.timedelta(minutes=1), card=CARD, transaction_id=2,
              energy_wh=1000, duration_s=600.0)
    api.scorer.record_grid_sample("pt.porto.st-001", "porto", 10000.0, 3600.0, T0 + dt.timedelta(minutes=30))
    raised = api.scorer.record_grid_sample("pt.porto.st-001", "porto", 0.0, 30.0, T0 + dt.timedelta(hours=1))
    assert raised == []


def test_grid_gap_quiet_with_nothing_completed_and_during_a_session(api, stubs):
    st = "pt.porto.st-001"
    # aveiro st-004 case: the feeder moves, no session has stopped yet
    for i in range(6):
        assert api.scorer.record_grid_sample(st, "porto", 10000.0, 600.0, T0 + dt.timedelta(minutes=10 * i)) == []
    # porto st-004 case: one session stopped, another still charging; its energy is not booked yet
    api.event("session.stop", PORTO, T0 + dt.timedelta(minutes=61), card=CARD, transaction_id=1,
              energy_wh=1000, duration_s=600.0)
    api.event("session.stop", PORTO, T0 + dt.timedelta(minutes=62), card=CARD, transaction_id=2,
              energy_wh=900, duration_s=600.0)
    # 1.9 kWh against 10 kWh (19%) right now, but the settled view an interval ago was empty
    assert api.alerts() == []


def test_sustained_scale_tamper_alerts_grid_gap_without_isolating(api, stubs):
    st = "pt.porto.st-001"
    # a meter scaling readings by 0.5: each 10-minute session books half of the feeder energy
    for i in range(6):
        t = T0 + dt.timedelta(minutes=10 * i)
        api.scorer.record_grid_sample(st, "porto", 12000.0, 600.0, t)            # 2000 Wh on the feeder
        api.event("session.stop", PORTO, t + dt.timedelta(minutes=1), card=CARD, transaction_id=10 + i,
                  energy_wh=1000, duration_s=600.0)
    api.scorer.record_grid_sample(st, "porto", 0.0, 30.0, T0 + dt.timedelta(hours=2))
    a = [x for x in api.alerts() if x["rule"] == "grid_gap"][0]
    assert a["severity"] == "warning" and a["hier_id"] == st          # alert only: power_gap isolates
    assert 0.4 <= a["evidence"]["ratio"] <= 0.6 and a["evidence"]["sessions"] >= 2


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
