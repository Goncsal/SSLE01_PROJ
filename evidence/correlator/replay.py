#!/usr/bin/env python3
"""Replay recorded signals into the correlator, preserving their original arrival timing.

Usage: replay.py {hijack|fraud|botnet} <correlator-url>      (bearer in $CORRELATOR_API_TOKEN)

The recorded alerts (evidence/hijack, evidence/fraud, evidence/botnet) are re-posted to the
same endpoints the live sources use. All timestamps are shifted by one constant so that the
first recorded arrival happens now; the spacing between the events, and the lag between an
event's own time and its arrival at the correlator, stay as recorded. Nothing is generated:
no attack, no load. Alerts below Wazuh level 7 are posted only if they are response signals,
exactly what the custom-correlator integration forwards.
"""
import datetime as dt
import json
import os
import sys
import time
import urllib.request

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
EV = os.path.join(ROOT, "evidence")


def ts(s):
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00").replace("+0000", "+00:00"))


def iso(t):
    return t.astimezone(dt.UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def wanted(a):
    r, d = a.get("rule", {}), a.get("data", {})
    if int(r.get("level", 0)) >= 7:
        return True
    return (d.get("event") == "ar.action" and d.get("result") == "ok") or \
        d.get("event") in ("registry.quarantine", "billing.card_blocked")


def jsonl(path):
    with open(os.path.join(EV, path)) as fh:
        return [json.loads(x) for x in fh if x.strip()]


def wazuh_events(alerts):
    """(arrival, endpoint, payload) in Wazuh terms: the arrival is the alert time."""
    return [(ts(a["timestamp"]), "wazuh", a) for a in alerts if "data" in a and wanted(a)]


def scenario(name):
    if name == "hijack":
        ev = wazuh_events(jsonl("hijack/scenario1-rogue-dupe/wazuh/alerts-110104-110131.jsonl"))
        line = jsonl("hijack/scenario1-rogue-dupe/ar/registry-quarantine-and-release.jsonl")[0]
        last = max(a for a, _, _ in ev)
        # the recorded run has the registry log line, not its 110405 alert: wrap it as Wazuh
        # would have forwarded it, arriving just after the confirming alert
        ev.append((last + dt.timedelta(milliseconds=300), "wazuh", {
            "timestamp": iso(last), "id": "recorded-registry-quarantine",
            "rule": {"id": "110405", "level": 4, "groups": ["evcharge", "infra"],
                     "description": "Response audit: registry registry.quarantine for %s (source wazuh)." % line["hier_id"]},
            "data": line}))
        return ev
    if name == "fraud":
        ev = wazuh_events(jsonl("fraud/enforcing-redemo/clone-card/wazuh-alerts.jsonl"))
        ev = [e for e in ev if e[2]["rule"]["id"] in ("110210", "110209", "110410")]
        fa = json.load(open(os.path.join(EV, "fraud/enforcing-redemo/alerts/impossible_travel-cloned-card.json")))
        ev.append((ts(fa["ts"]) + dt.timedelta(milliseconds=40), "fraud", fa))
        return ev
    if name == "botnet":
        al = [a for a in jsonl("botnet/alerts/wazuh-alerts.jsonl") if a["timestamp"] >= "2026-11-10T18:00"]
        return wazuh_events(al)
    raise SystemExit("unknown scenario")


def shift(payload, kind, delta):
    p = json.loads(json.dumps(payload))
    if kind == "wazuh":
        p["timestamp"] = iso(ts(p["timestamp"]) + delta)
        if isinstance(p.get("data"), dict) and p["data"].get("ts"):
            p["data"]["ts"] = iso(ts(p["data"]["ts"]) + delta)
    else:
        p["ts"] = iso(ts(p["ts"]) + delta)
    return p


def post(base, token, kind, payload):
    req = urllib.request.Request(
        "%s/signals/%s" % (base, kind), data=json.dumps(payload).encode(), method="POST",
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + token})
    with urllib.request.urlopen(req, timeout=5) as r:
        return r.status


def main():
    name, base = sys.argv[1], sys.argv[2].rstrip("/")
    token = os.environ["CORRELATOR_API_TOKEN"]
    ev = sorted(scenario(name), key=lambda e: e[0])
    t0 = dt.datetime.now(dt.UTC) + dt.timedelta(seconds=1)
    delta = t0 - ev[0][0]
    print("%s: %d signals, first arrival -> %s" % (name, len(ev), iso(t0)))
    for arrival, kind, payload in ev:
        wait = (arrival + delta - dt.datetime.now(dt.UTC)).total_seconds()
        if wait > 0:
            time.sleep(wait)
        body = [] if False else shift(payload, kind, delta)
        status = post(base, token, kind, body)
        rid = body.get("rule", {}).get("id") if kind == "wazuh" else body["rule"]
        print("  +%6.1fs %-6s %-12s -> %s" % ((arrival - ev[0][0]).total_seconds(), kind, rid, status))


if __name__ == "__main__":
    main()
