#!/usr/bin/env python3
"""Station outbound watcher (docs/interfaces.md 15.1).

Run by a systemd timer every 10 s as root. Lists the established outbound TCP
connections (`ss -tnpH state established`) and, for each destination that is not in the
allow file and has not been reported during the last hour, appends one `host.new_outbound`
line (base fields of section 6) to the netwatch log. Standard library only.

    netwatch.py --hier-id pt.porto.st-001
"""
from __future__ import annotations

import argparse
import datetime as dt
import fcntl
import ipaddress
import json
import os
import re
import subprocess
import sys

ALLOW_FILE = "/etc/evcharge/netwatch-allow.txt"
STATE_FILE = "/var/lib/evcharge-netwatch/seen.json"
LOG_FILE = "/var/log/evcharge/netwatch.json"
REPORT_EVERY_S = 3600

_PROC_RE = re.compile(r'\(\("([^"]+)"')


def split_addr(token: str) -> tuple[str, int] | None:
    """IPv4, v4-mapped IPv6 or IPv6 'address:port' token -> (ip, port), or None."""
    host, sep, port = token.rpartition(":")
    if not sep or not port.isdigit():
        return None
    host = host.strip("[]")
    try:
        ip = ipaddress.ip_address(host.split("%")[0])
    except ValueError:
        return None
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return str(ip), int(port)


def parse_ss(text: str) -> list[dict]:
    """Rows of `ss -tnpH`: Recv-Q Send-Q Local Peer [users:(("name",pid=1,fd=2))]."""
    rows = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 4:
            continue
        if parts[0] in ("Recv-Q", "State"):
            continue
        # tolerate output that still starts with the state column
        if parts[0].isalpha() and len(parts) >= 5:
            parts = parts[1:]
        local, peer = split_addr(parts[2]), split_addr(parts[3])
        if local is None or peer is None:
            continue
        m = _PROC_RE.search(line)
        rows.append({"local_ip": local[0], "local_port": local[1],
                     "dstip": peer[0], "dstport": peer[1],
                     "process": m.group(1) if m else "unknown"})
    return rows


def parse_listeners(text: str) -> set[int]:
    """Ports in `ss -tlnH` output (inbound connections to them are not outbound)."""
    ports = set()
    for line in text.splitlines():
        parts = line.split()
        if parts and parts[0].isalpha():      # LISTEN state column
            parts = parts[1:]
        if len(parts) >= 3:
            addr = split_addr(parts[2])
            if addr:
                ports.add(addr[1])
    return ports


def load_allow(text: str) -> set[str]:
    allow = set()
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            allow.add(str(ipaddress.ip_address(line)))
    return allow


def new_outbound(rows: list[dict], listeners: set[int], allow: set[str],
                 seen: dict[str, float], now: float) -> list[dict]:
    """Connections to report; `seen` ({"ip:port": last report time}) is updated in place."""
    for key in [k for k, t in seen.items() if now - t >= REPORT_EVERY_S]:
        del seen[key]
    out = []
    for r in rows:
        ip = ipaddress.ip_address(r["dstip"])
        if ip.is_loopback or r["dstip"] in allow or r["local_port"] in listeners:
            continue
        key = f"{r['dstip']}:{r['dstport']}"
        if key in seen:
            continue
        seen[key] = now
        out.append(r)
    return out


def socket_report(rows: list[dict], listeners: set[int], allow: set[str]) -> str:
    """Stable text for the agent's check_diff: only destinations outside the allow file, no
    ephemeral ports, queue sizes or pids, so a quiet station always prints the same line."""
    dests = set()
    for r in rows:
        if (ipaddress.ip_address(r["dstip"]).is_loopback or r["dstip"] in allow
                or r["local_port"] in listeners):
            continue
        dests.add(f"{r['dstip']}:{r['dstport']} {r['process']}")
    return "\n".join(sorted(dests)) if dests else "no destinations outside the allow list"


def build_line(hier_id: str, row: dict, now: float) -> dict:
    d = dt.datetime.fromtimestamp(now, tz=dt.timezone.utc)
    parts = hier_id.split(".")
    line = {
        "ts": d.strftime("%Y-%m-%dT%H:%M:%S.") + f"{d.microsecond // 1000:03d}Z",
        "level": "warning",
        "service": "netwatch",
        "instance": hier_id,
        "event": "host.new_outbound",
        "msg": f"new outbound connection to {row['dstip']}:{row['dstport']} by {row['process']}",
        "hier_id": hier_id,
        "dstip": row["dstip"],
        "dstport": row["dstport"],
        "process": row["process"],
    }
    if len(parts) == 3:
        line["city"], line["station"] = parts[1], parts[2]
    return line


def _run(cmd: list[str]) -> str:
    return subprocess.run(cmd, capture_output=True, text=True, check=True, timeout=5).stdout


def _read(path: str) -> str:
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return ""


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--hier-id", help="station id, e.g. pt.porto.st-001 (required unless --sockets)")
    ap.add_argument("--sockets", action="store_true",
                    help="print the current non-allowed destinations, sorted (sampled by the agent)")
    ap.add_argument("--allow-file", default=ALLOW_FILE)
    ap.add_argument("--state-file", default=STATE_FILE)
    ap.add_argument("--log-file", default=LOG_FILE)
    ap.add_argument("--ss-file", help="read established sockets from a file (tests, dry runs)")
    ap.add_argument("--now", type=float, help=argparse.SUPPRESS)
    args = ap.parse_args(argv)
    if not args.sockets and not args.hier_id:
        ap.error("--hier-id is required")

    import time
    now = args.now if args.now is not None else time.time()
    established = _read(args.ss_file) if args.ss_file else _run(["ss", "-tnpH", "state", "established"])
    listeners = parse_listeners(_run(["ss", "-tlnH"])) if not args.ss_file else set()
    try:
        allow = load_allow(_read(args.allow_file))
    except ValueError as exc:
        print(f"netwatch: bad allow file: {exc}", file=sys.stderr)
        return 2

    if args.sockets:
        print(socket_report(parse_ss(established), listeners, allow))
        return 0

    os.makedirs(os.path.dirname(args.state_file), exist_ok=True)
    with open(args.state_file + ".lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            seen = json.loads(_read(args.state_file) or "{}")
        except ValueError:
            seen = {}
        rows = new_outbound(parse_ss(established), listeners, allow, seen, now)
        if rows:
            with open(args.log_file, "a", encoding="utf-8") as f:
                for r in rows:
                    f.write(json.dumps(build_line(args.hier_id, r, now), separators=(",", ":")) + "\n")
        tmp = args.state_file + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(seen, f)
        os.replace(tmp, args.state_file)
    return 0


if __name__ == "__main__":
    sys.exit(main())
