import json

from conftest import TOKENS, ar_lines, run_script, wazuh_message

HID = "pt.porto.st-005.cp-9"


def alert(hier_id=HID, aid="1730000000.111", rule="110303"):
    return {"id": aid, "rule": {"id": rule, "description": "duplicate charger id"},
            "data": {"hier_id": hier_id, "srcip": "10.31.0.66"}}


def run(layout, a=None, **kw):
    return run_script("quarantine-charger", layout, wazuh_message(a or alert()), **kw)


def test_happy_path(layout, services):
    res = run(layout)
    assert res.returncode == 0
    # handshake printed first, then nothing else on stdout
    hs = json.loads(res.stdout.splitlines()[0])
    assert hs["command"] == "check_keys" and hs["parameters"]["keys"] == [HID]
    assert services.paths() == [
        ("GET", "/connections/" + HID), ("GET", "/nodes/" + HID), ("POST", "/quarantine/" + HID),
        ("POST", "/commands/%s/remote-stop" % HID), ("POST", "/commands/%s/change-availability" % HID),
        ("POST", "/commands/%s/disconnect" % HID)]
    q = services.calls[2]
    assert q["auth"] == "Bearer " + TOKENS["REGISTRY_RESPONDER_TOKEN"]
    assert q["body"]["source"] == "wazuh" and q["body"]["incident_id"] == "1730000000.111"
    assert services.calls[0]["auth"] == "Bearer " + TOKENS["CSMS_RESPONDER_TOKEN"]
    ca = services.calls[4]["body"]
    assert ca["type"] == "Inoperative" and ca["connector_id"] == 0
    lines = ar_lines(layout)
    assert [x["event"] for x in lines] == ["ar.evidence", "ar.evidence", "ar.action"]
    act = lines[-1]
    assert act["result"] == "ok" and act["ar_command"] == "quarantine-charger"
    assert act["hier_id"] == HID and act["rule_id"] == "110303" and act["alert_id"] == "1730000000.111"
    assert act["srcip"] == "10.31.0.66"
    req = ar_lines(layout, "evcharge-revocation-requests.json")
    assert len(req) == 1 and req[0]["hier_id"] == HID and req[0]["alert_id"] == "1730000000.111"
    assert set(req[0]) == {"hier_id", "alert_id", "rule_id", "ts", "reason"}


def test_no_release_or_revocation_calls(layout, services):
    run(layout)
    for method, path in services.paths():
        assert "release" not in path and "revoke" not in path and method in ("GET", "POST")


def test_409_and_404_tolerated(layout, services):
    services.responses[("POST", "/commands/%s/remote-stop" % HID)] = (409, {"status": "Rejected"})
    services.responses[("POST", "/commands/%s/disconnect" % HID)] = (404, {"status": "NotConnected"})
    services.responses[("GET", "/connections/" + HID)] = (404, {"detail": "not found"})
    run(layout)
    assert ar_lines(layout)[-1]["result"] == "ok"
    assert len(ar_lines(layout, "evcharge-revocation-requests.json")) == 1


def test_csms_error_is_reported(layout, services):
    services.responses[("POST", "/commands/%s/disconnect" % HID)] = (502, {"status": "Error"})
    res = run(layout)
    assert res.returncode == 0
    last = ar_lines(layout)[-1]
    assert last["result"] == "error" and "disconnect" in last["reason"]
    # isolation still happened and the revocation request was still written
    assert ("POST", "/quarantine/" + HID) in services.paths()
    assert len(ar_lines(layout, "evcharge-revocation-requests.json")) == 1


def test_registry_refused_skips_revocation_request(layout, services):
    services.responses[("POST", "/quarantine/" + HID)] = (403, {"detail": "scope"})
    run(layout)
    assert ar_lines(layout)[-1]["result"] == "error"
    assert ("POST", "/commands/%s/disconnect" % HID) in services.paths()
    assert ar_lines(layout, "evcharge-revocation-requests.json") == []


def test_service_down_gives_error_line_and_exit_0(layout, services):
    services.stop()
    res = run(layout)
    assert res.returncode == 0
    last = ar_lines(layout)[-1]
    assert last["result"] == "error" and "unreachable" in last["reason"]
    assert ar_lines(layout, "evcharge-revocation-requests.json") == []


def test_fallback_url_used_when_primary_down(layout, services):
    env = (layout.etc / "ar.env").read_text().replace("REGISTRY_URL=", "REGISTRY_URL=http://127.0.0.1:1\nX=")
    env += "REGISTRY_URL_FALLBACK=%s\n" % services.url
    (layout.etc / "ar.env").write_text(env)
    run(layout)
    assert ("POST", "/quarantine/" + HID) in services.paths()
    assert ar_lines(layout)[-1]["result"] == "ok"


def test_missing_env_is_error(layout):
    (layout.etc / "ar.env").unlink()
    assert run(layout).returncode == 0
    assert ar_lines(layout)[-1]["result"] == "error"


def test_invalid_id_refused(layout, services):
    for bad in ["pt.porto.edge", "pt.porto", "../etc/passwd", "pt.porto.st-005.cp-0", "", "pt.Porto.st-001.cp-1"]:
        res = run(layout, alert(hier_id=bad))
        assert res.returncode == 0
    lines = ar_lines(layout)
    assert len(lines) == 6 and all(x["result"] == "refused" and x["reason"] == "invalid_id" for x in lines)
    assert all("hier_id" not in x for x in lines)
    assert services.calls == []


def test_allowlisted_refused(layout, services):
    (layout.etc / "quarantine-allowlist").write_text("# test\n%s\n" % HID)
    run(layout)
    last = ar_lines(layout)[-1]
    assert last["result"] == "refused" and last["reason"] == "allowlisted"
    assert services.calls == []


def test_rate_guard(layout, services):
    for n in range(1, 6):
        run(layout, alert(hier_id="pt.porto.st-005.cp-%d" % n, aid="a%d" % n))
    assert all(x["result"] == "ok" for x in ar_lines(layout) if x["event"] == "ar.action")
    before = len(services.calls)
    run(layout, alert(hier_id="pt.porto.st-005.cp-6", aid="a6"))
    last = ar_lines(layout)[-1]
    assert last["result"] == "refused" and last["reason"].startswith("rate_guard")
    assert len(services.calls) == before
    # a repeat for an already-quarantined charger is still allowed (idempotent, no new budget)
    run(layout, alert(hier_id="pt.porto.st-005.cp-1", aid="a7"))
    assert ar_lines(layout)[-1]["result"] == "ok"


def test_check_keys_abort_does_nothing(layout, services):
    res = run(layout, replies=("abort",))
    assert res.returncode == 0 and json.loads(res.stdout)["command"] == "check_keys"
    assert services.calls == [] and ar_lines(layout) == []


def test_delete_command_is_a_noop(layout, services):
    res = run_script("quarantine-charger", layout, wazuh_message(alert(), "delete"))
    assert res.returncode == 0 and res.stdout == "" and services.calls == []


def test_garbage_input_exits_0(layout):
    res = run_script("quarantine-charger", layout, None, stdin_text="not json\n")
    assert res.returncode == 0


def test_self_test_and_dry_run(layout, services):
    res = run_script("quarantine-charger", layout, None, args=["--self-test"], stdin_text="")
    assert res.returncode == 0 and "self-test ok" in res.stdout
    f = layout.root / "a.json"
    f.write_text(json.dumps(wazuh_message(alert())))
    res = run_script("quarantine-charger", layout, None, args=["--dry-run", str(f)], stdin_text="")
    assert res.returncode == 0 and "/quarantine/" + HID in res.stdout
    assert "reg-tok" not in res.stdout
    assert services.calls == [] and ar_lines(layout) == []
    assert not (layout.run / "evcharge-quarantine-rate.json").exists()


# ---- station level (rule 110303) -------------------------------------------------------

STATION = "pt.lisboa.st-001"
CHARGERS = [STATION + ".cp-1", STATION + ".cp-2"]


def station_alert(aid="1730000000.777"):
    return {"id": aid, "rule": {"id": "110303", "description": "station opened a new outbound connection"},
            "data": {"hier_id": STATION, "dstip": "10.255.255.173", "dstport": "8099"}}


def locate_body(ids):
    return {"query": STATION, "count": len(ids), "truncated": False,
            "matches": [{"hier_id": h, "kind": "charger"} for h in ids]}


def test_station_quarantines_the_station_and_isolates_each_charger(layout, services):
    services.responses[("GET", "/locate/%s?kind=charger" % STATION)] = (200, locate_body(CHARGERS))
    res = run(layout, station_alert())
    assert res.returncode == 0
    assert json.loads(res.stdout.splitlines()[0])["parameters"]["keys"] == [STATION]
    paths = services.paths()
    assert paths[:3] == [("GET", "/connections?prefix=" + STATION), ("GET", "/nodes/" + STATION),
                         ("POST", "/quarantine/" + STATION)]
    assert paths[3] == ("GET", "/locate/%s?kind=charger" % STATION)
    for hid in CHARGERS:
        for cmd in ("remote-stop", "change-availability", "disconnect"):
            assert ("POST", "/commands/%s/%s" % (hid, cmd)) in paths
    # one registry quarantine, for the station only (the registry propagates it to the chargers)
    assert [p for m, p in paths if p.startswith("/quarantine/")] == ["/quarantine/" + STATION]
    act = ar_lines(layout)[-1]
    assert act["result"] == "ok" and act["hier_id"] == STATION and act["rule_id"] == "110303"
    assert "2 chargers" in act["reason"]
    req = ar_lines(layout, "evcharge-revocation-requests.json")
    assert [r["hier_id"] for r in req] == CHARGERS


def test_station_falls_back_to_csms_connections_when_locate_fails(layout, services):
    services.responses[("GET", "/locate/%s?kind=charger" % STATION)] = (500, {"detail": "boom"})
    services.responses[("GET", "/connections?prefix=" + STATION)] = (200, {"items": [{"hier_id": CHARGERS[0]}]})
    run(layout, station_alert())
    assert ("POST", "/commands/%s/disconnect" % CHARGERS[0]) in services.paths()
    last = ar_lines(layout)[-1]
    assert last["result"] == "error" and "locate" in last["reason"]


def test_station_with_no_chargers_still_quarantines(layout, services):
    services.responses[("GET", "/locate/%s?kind=charger" % STATION)] = (200, locate_body([]))
    run(layout, station_alert())
    assert ("POST", "/quarantine/" + STATION) in services.paths()
    assert not [p for m, p in services.paths() if p.startswith("/commands/")]
    assert ar_lines(layout)[-1]["result"] == "ok"


def test_station_ignores_foreign_chargers_in_the_answer(layout, services):
    services.responses[("GET", "/locate/%s?kind=charger" % STATION)] = (
        200, locate_body([CHARGERS[0], "pt.porto.st-001.cp-1", "../x"]))
    run(layout, station_alert())
    cmds = [p for m, p in services.paths() if p.startswith("/commands/")]
    assert cmds and all(p.startswith("/commands/%s/" % CHARGERS[0]) for p in cmds)


def test_station_allowlisted_and_rate_guard(layout, services):
    (layout.etc / "quarantine-allowlist").write_text(STATION + "\n")
    run(layout, station_alert())
    assert ar_lines(layout)[-1]["reason"] == "allowlisted" and services.calls == []
    (layout.etc / "quarantine-allowlist").write_text("")
    for n in range(1, 6):
        run(layout, {"id": "s%d" % n, "rule": {"id": "110303"}, "data": {"hier_id": "pt.lisboa.st-00%d" % n}})
    before = len(services.calls)
    run(layout, {"id": "s6", "rule": {"id": "110303"}, "data": {"hier_id": "pt.lisboa.st-006"}})
    assert ar_lines(layout)[-1]["reason"].startswith("rate_guard") and len(services.calls) == before


def test_station_dry_run_prints_plan(layout, services):
    f = layout.root / "s.json"
    f.write_text(json.dumps(wazuh_message(station_alert())))
    res = run_script("quarantine-charger", layout, None, args=["--dry-run", str(f)], stdin_text="")
    assert res.returncode == 0 and "station " + STATION in res.stdout and "/quarantine/" + STATION in res.stdout
    assert services.calls == [] and ar_lines(layout) == []


def test_station_id_shapes(layout, services):
    for bad in ["pt.lisboa.st-000", "pt.lisboa", "pt.lisboa.st-1", "pt.lisboa.st-001.x"]:
        run(layout, {"id": "b", "rule": {"id": "110303"}, "data": {"hier_id": bad}})
    assert all(x["reason"] == "invalid_id" for x in ar_lines(layout)) and services.calls == []
