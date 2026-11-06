import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import netwatch  # noqa: E402

SS = """\
0      0      10.31.0.16:37182 10.31.0.2:443   users:(("python",pid=796,fd=9))
0      0      10.31.0.16:37184 10.31.0.2:443   users:(("python",pid=796,fd=8))
0      0      10.31.0.16:51000 10.255.255.170:1514 users:(("wazuh-agentd",pid=40,fd=5))
0      0      10.31.0.16:44444 203.0.113.9:8443 users:(("curl",pid=900,fd=3))
0      0      [::ffff:10.31.0.16]:44446 [::ffff:203.0.113.9]:8443 users:(("curl",pid=901,fd=3))
0      0      10.31.0.16:9300 10.31.0.1:55555
0      0      127.0.0.1:5000 127.0.0.1:6000 users:(("x",pid=1,fd=1))
"""
LISTEN = "0 4096 0.0.0.0:9300 0.0.0.0:*\n0 128 0.0.0.0:22 0.0.0.0:*\n"
ALLOW = "# edge\n10.31.0.2\n10.255.255.170  # manager\n10.31.0.1\n"


def test_parse_ss_rows():
    rows = netwatch.parse_ss(SS)
    assert len(rows) == 7
    assert rows[0] == {"local_ip": "10.31.0.16", "local_port": 37182, "dstip": "10.31.0.2",
                       "dstport": 443, "process": "python"}
    assert rows[4]["dstip"] == "203.0.113.9"          # v4-mapped address unwrapped
    assert rows[5]["process"] == "unknown"             # no users: column


def test_parse_listeners_and_allow():
    assert netwatch.parse_listeners(LISTEN) == {9300, 22}
    assert netwatch.load_allow(ALLOW) == {"10.31.0.2", "10.255.255.170", "10.31.0.1"}


def test_only_unknown_destination_reported_once():
    seen = {}
    out = netwatch.new_outbound(netwatch.parse_ss(SS), netwatch.parse_listeners(LISTEN),
                                netwatch.load_allow(ALLOW), seen, now=1000.0)
    assert [(r["dstip"], r["dstport"], r["process"]) for r in out] == [("203.0.113.9", 8443, "curl")]
    # same destination again within the hour: silent
    again = netwatch.new_outbound(netwatch.parse_ss(SS), set(), netwatch.load_allow(ALLOW), seen, 2000.0)
    assert again == []
    # after the hour it is reported again
    later = netwatch.new_outbound(netwatch.parse_ss(SS), set(), netwatch.load_allow(ALLOW), seen, 1000.0 + 3601)
    assert len(later) == 1


def test_inbound_to_listening_port_ignored():
    rows = netwatch.parse_ss("0 0 10.31.0.16:9300 10.31.0.99:55555\n")
    assert netwatch.new_outbound(rows, {9300}, set(), {}, 0.0) == []


def test_line_format():
    row = {"dstip": "203.0.113.9", "dstport": 8443, "process": "curl"}
    line = netwatch.build_line("pt.porto.st-001", row, 1762400000.123)
    assert line["service"] == "netwatch" and line["event"] == "host.new_outbound"
    assert line["instance"] == line["hier_id"] == "pt.porto.st-001"
    assert line["level"] == "warning" and line["ts"].endswith("Z")
    assert (line["dstip"], line["dstport"], line["process"]) == ("203.0.113.9", 8443, "curl")
    assert line["city"] == "porto" and line["station"] == "st-001"
    for key in ("ts", "level", "service", "instance", "event", "msg"):
        assert key in line


def test_main_writes_log_and_dedupes(tmp_path):
    ss = tmp_path / "ss.txt"
    ss.write_text(SS)
    allow = tmp_path / "allow.txt"
    allow.write_text(ALLOW)
    log = tmp_path / "netwatch.json"
    args = ["--hier-id", "pt.porto.st-001", "--allow-file", str(allow), "--ss-file", str(ss),
            "--state-file", str(tmp_path / "state" / "seen.json"), "--log-file", str(log)]
    assert netwatch.main(args + ["--now", "100"]) == 0
    assert netwatch.main(args + ["--now", "110"]) == 0
    lines = [json.loads(x) for x in log.read_text().splitlines()]
    assert len(lines) == 1 and lines[0]["dstip"] == "203.0.113.9"


def test_socket_report_is_stable_and_names_only_new_destinations():
    rows = netwatch.parse_ss(SS)
    allow = netwatch.load_allow(ALLOW)
    listeners = netwatch.parse_listeners(LISTEN)
    assert netwatch.socket_report(rows, listeners, allow) == "203.0.113.9:8443 curl"
    quiet = [r for r in rows if r["dstip"] != "203.0.113.9"]
    assert netwatch.socket_report(quiet, listeners, allow) == "no destinations outside the allow list"
