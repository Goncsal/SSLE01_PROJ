"""The manager block, the rules and the logtest cases agree with each other."""
import json
import re
from pathlib import Path

WAZUH = Path(__file__).resolve().parents[2]
CONF = (WAZUH / "manager" / "ossec-evcharge.xml").read_text()
RULES = "".join(p.read_text() for p in (WAZUH / "rules").glob("evcharge_*.xml"))
RULE_IDS = set(re.findall(r'<rule id="(\d+)"', RULES))
CUT = {"110102", "110103", "110105", "110106", "110107", "110108", "110109", "110110", "110111", "110112",
       "110113", "110114", "110115", "110116", "110122", "110123", "110130", "110131", "110201", "110203",
       "110205", "110206", "110302", "110305", "110402", "110403", "110404"}


def triggers():
    out = {}
    for block in re.findall(r"<active-response>(.*?)</active-response>", CONF, re.S):
        cmd = re.search(r"<command>(.*?)</command>", block).group(1)
        out[cmd] = set(re.search(r"<rules_id>(.*?)</rules_id>", block).group(1).replace(" ", "").split(","))
    return out


def test_responses_point_at_existing_rules():
    t = triggers()
    assert t == {"firewall-drop": {"110121", "110307"}, "evcharge-edge-drop": {"110132"},
                 "quarantine-charger": {"110303", "110304"}, "block-card": {"110210"}}
    for ids in t.values():
        assert ids <= RULE_IDS


def test_cut_rules_are_gone_everywhere():
    assert not (CUT & RULE_IDS)
    for rid in CUT:
        assert rid not in CONF


def test_ruleset_size():
    assert len(RULE_IDS) == 36


def test_correlator_receives_what_it_needs():
    assert "<level>7</level>" in CONF
    ids = set(re.search(r"<rule_id>(.*?)</rule_id>", CONF).group(1).split(","))
    assert ids == {"110405", "110410", "110209"}
    levels = {m.group(1): int(m.group(2)) for m in re.finditer(r'<rule id="(\d+)" level="(\d+)"', RULES)}
    for rid in ("110100", "110101", "110104", "110132", "110210", "110300", "110303", "110304", "110411"):
        assert levels[rid] >= 7, rid


def test_110303_logtest_cases_carry_a_station_id():
    station = re.compile(r"^pt\.[a-z]+\.st-\d{3}$")
    lines = (WAZUH / "tests" / "botnet.jsonl").read_text().splitlines()
    expect = (WAZUH / "tests" / "botnet.expect").read_text().splitlines()
    hits = [json.loads(l) for l, e in zip(lines, expect) if e.split()[1] == "110303"]
    assert hits and all(station.match(h["hier_id"]) for h in hits)
