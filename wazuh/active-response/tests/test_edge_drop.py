import json
import os
import stat
import subprocess
import sys

import pytest

from conftest import SCRIPTS, wazuh_message

NET = "10.31.0.0/24"
FAKE_IPT = """#!/bin/sh
# records every call; `-C` succeeds only if the rule was inserted before
echo "iptables $*" >> "$FAKE_DIR/calls"
rule=$(echo "$*" | sed 's/-w 5 -[CIDA] //')
case "$*" in
  *" -C "*) grep -qxF "$rule" "$FAKE_DIR/rules" 2>/dev/null; exit $? ;;
  *" -I "*) echo "$rule" >> "$FAKE_DIR/rules"; exit 0 ;;
  *" -D "*) grep -vxF "$rule" "$FAKE_DIR/rules" > "$FAKE_DIR/rules.new"; mv "$FAKE_DIR/rules.new" "$FAKE_DIR/rules"; exit 0 ;;
esac
"""
FAKE_IP = """#!/bin/sh
case "$*" in
  *route*) echo "default via 10.31.0.1 dev eth0" ;;
  *addr*) echo "2: eth0    inet 10.31.0.2/24 brd 10.31.0.255 scope global eth0" ;;
esac
"""


@pytest.fixture
def edge(tmp_path):
    etc, fake, log = tmp_path / "etc", tmp_path / "fake", tmp_path / "log" / "ar-edge.json"
    etc.mkdir(); fake.mkdir()
    for name, body in (("iptables", FAKE_IPT), ("ip", FAKE_IP)):
        p = fake / name
        p.write_text(body)
        p.chmod(p.stat().st_mode | stat.S_IXUSR)
    (etc / "role").write_text("edge\n")
    (etc / "city-net").write_text(NET + "\n")

    class E:
        pass
    e = E()
    e.etc, e.fake, e.log = etc, fake, log

    def run(srcip, command="add", reply="continue", args=(), rule="110110"):
        alert = {"id": "1730000000.333", "rule": {"id": rule}, "data": {"srcip": srcip}}
        env = dict(os.environ, EVCHARGE_EDGE_ETC=str(etc), EVCHARGE_EDGE_LOG=str(log), FAKE_DIR=str(fake),
                   PATH=str(fake) + os.pathsep + os.environ["PATH"])
        stdin = json.dumps(wazuh_message(alert, command)) + "\n" + json.dumps({"command": reply}) + "\n"
        return subprocess.run([sys.executable, str(SCRIPTS / "evcharge-edge-drop"), *args], input=stdin,
                              capture_output=True, text=True, env=env, timeout=30)
    e.run = run
    e.calls = lambda: (fake / "calls").read_text().splitlines() if (fake / "calls").exists() else []
    e.lines = lambda: [json.loads(x) for x in log.read_text().splitlines()] if log.exists() else []
    return e


def test_not_an_edge_is_silent_noop(edge):
    (edge.etc / "role").write_text("station\n")
    res = edge.run("10.31.0.66")
    assert res.returncode == 0 and res.stdout == "" and edge.calls() == [] and edge.lines() == []


def test_missing_role_is_noop(edge):
    (edge.etc / "role").unlink()
    assert edge.run("10.31.0.66").stdout == "" and edge.calls() == []


def test_srcip_outside_city_is_noop(edge):
    res = edge.run("10.32.0.66")
    assert res.returncode == 0 and res.stdout == "" and edge.calls() == [] and edge.lines() == []


def test_add_inserts_input_and_forward(edge):
    res = edge.run("10.31.0.66")
    assert res.returncode == 0
    assert json.loads(res.stdout)["parameters"]["keys"] == ["10.31.0.66"]
    ins = [c for c in edge.calls() if " -I " in c]
    assert ins == ["iptables -w 5 -I INPUT -s 10.31.0.66 -j DROP", "iptables -w 5 -I FORWARD -s 10.31.0.66 -j DROP"]
    line = edge.lines()[-1]
    assert line["event"] == "ar.action" and line["result"] == "ok" and line["reason"] == "drop_added"
    assert line["srcip"] == "10.31.0.66" and line["ar_command"] == "evcharge-edge-drop"
    assert line["rule_id"] == "110110" and line["alert_id"] == "1730000000.333"


def test_add_twice_does_not_stack(edge):
    edge.run("10.31.0.66")
    edge.run("10.31.0.66")
    assert len([c for c in edge.calls() if " -I " in c]) == 2
    assert edge.lines()[-1]["reason"] == "already_dropped"


def test_delete_removes_rules(edge):
    edge.run("10.31.0.66")
    res = edge.run("10.31.0.66", command="delete")
    assert res.returncode == 0 and res.stdout == ""
    dels = [c for c in edge.calls() if " -D " in c]
    assert dels == ["iptables -w 5 -D INPUT -s 10.31.0.66 -j DROP", "iptables -w 5 -D FORWARD -s 10.31.0.66 -j DROP"]
    assert edge.lines()[-1]["reason"] == "drop_removed"
    edge.run("10.31.0.66", command="delete")
    assert edge.lines()[-1]["reason"] == "drop_not_present"


def test_whitelisted_address_not_dropped(edge):
    (edge.etc / "ar-whitelist").write_text("# trusted\n10.31.0.40\n")
    edge.run("10.31.0.40")
    assert not [c for c in edge.calls() if " -I " in c]
    assert edge.lines()[-1]["result"] == "refused" and edge.lines()[-1]["reason"] == "whitelisted"


def test_own_and_gateway_addresses_not_dropped(edge):
    for ip in ("10.31.0.1", "10.31.0.2"):
        edge.run(ip)
    assert not [c for c in edge.calls() if " -I " in c]
    assert [x["reason"] for x in edge.lines()] == ["own_or_gateway_address"] * 2


def test_non_private_address_refused(edge):
    (edge.etc / "city-net").write_text("0.0.0.0/0\n")
    edge.run("8.8.8.8")
    assert not [c for c in edge.calls() if " -I " in c]
    assert edge.lines()[-1]["reason"] == "not_private"


def test_abort_handshake(edge):
    res = edge.run("10.31.0.66", reply="abort")
    assert res.returncode == 0 and not [c for c in edge.calls() if " -I " in c] and edge.lines() == []


def test_invalid_srcip_on_edge_refused(edge):
    edge.run("not-an-ip")
    assert edge.lines()[-1]["reason"] == "invalid_srcip"


def test_self_test_and_dry_run(edge, tmp_path):
    res = edge.run("10.31.0.66", args=["--self-test"])
    assert res.returncode == 0 and "self-test ok" in res.stdout
    f = tmp_path / "a.json"
    f.write_text(json.dumps(wazuh_message({"id": "1", "rule": {"id": "1"}, "data": {"srcip": "10.31.0.66"}})))
    res = edge.run("x", args=["--dry-run", str(f)])
    assert "-I INPUT -s 10.31.0.66" in res.stdout and edge.calls() == [] and edge.lines() == []
