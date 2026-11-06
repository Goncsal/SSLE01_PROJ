import json

from conftest import TOKENS, ar_lines, run_script, wazuh_message

HID = "pt.porto.st-005.cp-9"


def alert(hier_id=HID, aid="1730000000.111", rule="110101"):
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
    assert act["hier_id"] == HID and act["rule_id"] == "110101" and act["alert_id"] == "1730000000.111"
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
    for bad in ["pt.porto.edge", "pt.porto.st-005", "../etc/passwd", "pt.porto.st-005.cp-0", "", "pt.Porto.st-001.cp-1"]:
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
