#!/var/ossec/framework/python/bin/python3
"""Replay <block>.jsonl through the manager's wazuh-logtest service and compare the rule ids.

Runs on the manager (pushed there by run_logtest.sh), standard library only.
usage: logtest_driver.py DIR BLOCK [BLOCK ...]

<block>.jsonl  one JSON log line per line; a line {"_session": "reset"} starts a fresh
               logtest session (frequency and composite rules count events of one session)
<block>.expect "<line number> <rule id | none | reset>  # comment", one entry per line
               of the .jsonl; "none" means no evcharge rule (110000-110499) matched
"""
import json
import os
import socket
import struct
import sys

SOCK = "/var/ossec/queue/sockets/logtest"
LOCATION = {"edge": "/var/log/evcharge/edge-test.json"}


def call(payload):
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(20)
    sock.connect(SOCK)
    raw = json.dumps(payload).encode()
    sock.sendall(struct.pack("<I", len(raw)) + raw)
    head = b""
    while len(head) < 4:
        head += sock.recv(4 - len(head))
    size = struct.unpack("<I", head)[0]
    body = b""
    while len(body) < size:
        chunk = sock.recv(size - len(body))
        if not chunk:
            break
        body += chunk
    sock.close()
    return json.loads(body)


def replay(event, token):
    params = {"event": event, "log_format": "syslog", "location": "/var/log/evcharge/test.json"}
    if token:
        params["token"] = token
    reply = call({"version": 1, "origin": {"name": "evcharge-tests", "module": "logtest_driver"},
                  "command": "log_processing", "parameters": params})
    if reply.get("error"):
        raise SystemExit("logtest error: %s" % reply.get("message", reply))
    data = reply["data"]
    rule = (data.get("output") or {}).get("rule") or {}
    rid = str(rule.get("id", ""))
    if not (rid.isdigit() and 110000 <= int(rid) <= 110499):
        rid = "none"
    return data.get("token"), rid, bool(data.get("alert"))


def main():
    base, blocks = sys.argv[1], sys.argv[2:]
    bad = total = 0
    print("%-8s %4s  %-8s %-8s %-5s %-6s %s" % ("block", "line", "expect", "got", "alert", "result", "case"))
    for block in blocks:
        with open(os.path.join(base, block + ".jsonl")) as fh:
            lines = fh.read().splitlines()
        expect, notes = {}, {}
        with open(os.path.join(base, block + ".expect")) as fh:
            for raw in fh:
                body, _, note = raw.partition("#")
                if body.strip():
                    num, want = body.split()[:2]
                    expect[int(num)] = want
                    notes[int(num)] = note.strip()
        token = None
        for num, line in enumerate(lines, 1):
            want = expect.get(num)
            if want is None:
                raise SystemExit("%s: line %d has no expectation" % (block, num))
            if want == "reset":
                token = None
                continue
            token, got, alert = replay(line, token)
            total += 1
            ok = got == want
            bad += 0 if ok else 1
            print("%-8s %4d  %-8s %-8s %-5s %-6s %s" % (
                block, num, want, got, "yes" if alert else "no", "PASS" if ok else "FAIL", notes.get(num, "")))
    print("\n%d cases, %d passed, %d failed" % (total, total - bad, bad))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
