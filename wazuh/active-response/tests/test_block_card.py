import json

from conftest import TOKENS, ar_lines, run_script, wazuh_message

CARD = "04A1B2C3D4E5F6"


def alert(card=CARD, tid=1042, aid="1730000000.222", rule="110203"):
    data = {"card": card, "hier_id": "pt.porto.st-003.cp-2"}
    if tid is not None:
        data["transaction_id"] = tid
    return {"id": aid, "rule": {"id": rule, "description": "card used twice"}, "data": data}


def run(layout, a=None, **kw):
    return run_script("block-card", layout, wazuh_message(a or alert()), **kw)


def test_happy_path_with_flag(layout, services):
    res = run(layout)
    assert res.returncode == 0
    assert json.loads(res.stdout.splitlines()[0])["parameters"]["keys"] == [CARD]
    assert services.paths() == [("GET", "/sessions?card=%s&limit=100" % CARD),
                                ("POST", "/cards/%s/block" % CARD), ("POST", "/sessions/1042/flag")]
    for c in services.calls:
        assert c["auth"] == "Bearer " + TOKENS["BILLING_RESPONDER_TOKEN"]
    assert services.calls[1]["body"]["incident_id"] == "1730000000.222"
    assert services.calls[1]["body"]["source"] == "wazuh"
    lines = ar_lines(layout)
    assert [x["event"] for x in lines] == ["ar.evidence", "ar.action"]
    assert lines[-1]["result"] == "ok" and lines[-1]["card"] == CARD and lines[-1]["ar_command"] == "block-card"
    assert lines[-1]["rule_id"] == "110203" and lines[-1]["alert_id"] == "1730000000.222"


def test_no_flag_without_transaction(layout, services):
    run(layout, alert(tid=None))
    assert services.paths()[-1] == ("POST", "/cards/%s/block" % CARD) and len(services.calls) == 2


def test_never_unblocks(layout, services):
    run(layout)
    assert all("unblock" not in p for _, p in services.paths())


def test_service_down(layout, services):
    services.stop()
    assert run(layout).returncode == 0
    last = ar_lines(layout)[-1]
    assert last["result"] == "error" and "unreachable" in last["reason"]


def test_http_error_reported(layout, services):
    services.responses[("POST", "/cards/%s/block" % CARD)] = (403, {"detail": "scope"})
    run(layout)
    last = ar_lines(layout)[-1]
    assert last["result"] == "error" and "block: http 403" in last["reason"]


def test_invalid_card_refused(layout, services):
    for bad in ["04a1b2c3d4", "XYZ", "0" * 21, "0" * 7, "", "04A1B2C3; DROP"]:
        run(layout, alert(card=bad))
    lines = ar_lines(layout)
    assert len(lines) == 6 and all(x["result"] == "refused" and x["reason"] == "invalid_card" for x in lines)
    assert services.calls == []


def test_allowlisted_card_refused(layout, services):
    (layout.etc / "card-allowlist").write_text("%s\n" % CARD)
    run(layout)
    assert ar_lines(layout)[-1]["reason"] == "allowlisted" and services.calls == []


def test_rate_guard_10_per_hour(layout, services):
    for n in range(10):
        run(layout, alert(card="AB%06X" % n, tid=None))
    assert all(x["result"] == "ok" for x in ar_lines(layout) if x["event"] == "ar.action")
    n = len(services.calls)
    run(layout, alert(card="CD000001", tid=None))
    last = ar_lines(layout)[-1]
    assert last["result"] == "refused" and last["reason"].startswith("rate_guard") and len(services.calls) == n


def test_check_keys_abort(layout, services):
    res = run(layout, replies=("abort",))
    assert res.returncode == 0 and services.calls == [] and ar_lines(layout) == []


def test_self_test(layout):
    res = run_script("block-card", layout, None, args=["--self-test"], stdin_text="")
    assert res.returncode == 0 and "self-test ok" in res.stdout
