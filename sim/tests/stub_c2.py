"""A tiny C2 server for the botnet attack mode (docs/interfaces.md, 9.4).

Standard library only. Answers every POST /beacon with the configured task
and prints the bots (station hier-ids) it has seen, one line per beacon.

    python sim/tests/stub_c2.py --port 8099 --task flood --rate-hz 20 --duration-s 30
"""

from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

_seen_bots: set[str] = set()


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="tiny C2 server for the botnet PoC")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8099)
    p.add_argument("--task", default="idle", choices=["idle", "flood", "surge", "stop"])
    p.add_argument("--rate-hz", type=float, default=0.0)
    p.add_argument("--duration-s", type=float, default=0.0)
    return p


def make_handler(task: str, rate_hz: float, duration_s: float):
    class Handler(BaseHTTPRequestHandler):
        # HTTP/1.1 so the bot's persistent client (httpx.AsyncClient, one connection for
        # the whole beacon loop) keeps a single TCP connection open instead of
        # reconnecting on every beacon (BaseHTTPRequestHandler defaults to HTTP/1.0,
        # which closes the connection after each response) -- netwatch's periodic `ss`
        # snapshot can only see the beacon as an established outbound connection while
        # it stays open. Every response below sets Content-Length so keep-alive framing
        # stays correct.
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *a):  # quieter stdout
            pass

        def do_POST(self) -> None:
            if self.path != "/beacon":
                self.send_response(404)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length) if length else b"{}"
            try:
                data = json.loads(body)
            except ValueError:
                data = {}
            bot = data.get("bot", "?")
            if bot not in _seen_bots:
                _seen_bots.add(bot)
                print(f"bot seen: {bot} (total {len(_seen_bots)})", flush=True)
            answer: dict = {"task": task}
            if task == "flood":
                answer["rate_hz"] = rate_hz
                answer["duration_s"] = duration_s
            elif task == "surge":
                answer["duration_s"] = duration_s
            payload = json.dumps(answer).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    return Handler


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    handler = make_handler(args.task, args.rate_hz, args.duration_s)
    server = ThreadingHTTPServer((args.host, args.port), handler)
    print(f"stub_c2 listening on http://{args.host}:{args.port} task={args.task}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
