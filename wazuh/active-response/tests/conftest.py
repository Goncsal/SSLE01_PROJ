"""Shared fixtures: fake registry/CSMS/billing servers, a temp layout, a script runner."""
import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
SCRIPTS = HERE.parent
TOKENS = {"REGISTRY_RESPONDER_TOKEN": "reg-tok", "CSMS_RESPONDER_TOKEN": "csms-tok",
          "BILLING_RESPONDER_TOKEN": "bill-tok"}


class FakeServices:
    """One HTTP server standing in for the three services; records every request."""

    def __init__(self):
        self.calls = []
        self.responses = {}      # (method, path) -> (status, body); default 200 {}
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def _serve(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length)) if length else None
                outer.calls.append({"method": self.command, "path": self.path, "body": body,
                                    "auth": self.headers.get("Authorization")})
                status, payload = outer.responses.get((self.command, self.path), (200, {"ok": True}))
                raw = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            do_GET = do_POST = _serve

            def log_message(self, *a):
                pass

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = "http://127.0.0.1:%d" % self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def paths(self):
        return [(c["method"], c["path"]) for c in self.calls]

    def stop(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def services():
    fake = FakeServices()
    yield fake
    fake.stop()


@pytest.fixture
def layout(tmp_path, services):
    etc, logs, run = tmp_path / "etc", tmp_path / "logs", tmp_path / "run"
    for d in (etc, logs, run):
        d.mkdir()
    env_lines = ["REGISTRY_URL=%s" % services.url, "CSMS_API_URL=%s" % services.url,
                 "BILLING_URL=%s" % services.url] + ["%s=%s" % kv for kv in TOKENS.items()]
    (etc / "ar.env").write_text("\n".join(env_lines) + "\n")
    return type("Layout", (), {"etc": etc, "logs": logs, "run": run, "root": tmp_path})


def run_script(name, layout, message, replies=("continue",), args=(), extra_env=None, stdin_text=None):
    env = dict(os.environ, EVCHARGE_AR_ETC=str(layout.etc), EVCHARGE_AR_LOGS=str(layout.logs),
               EVCHARGE_AR_RUN=str(layout.run))
    env.update(extra_env or {})
    if stdin_text is None:
        stdin_text = json.dumps(message) + "\n" + "".join(json.dumps({"command": r}) + "\n" for r in replies)
    return subprocess.run([sys.executable, str(SCRIPTS / name), *args], input=stdin_text,
                          capture_output=True, text=True, env=env, timeout=60)


def ar_lines(layout, name="evcharge-ar.json"):
    path = layout.logs / name
    if not path.exists():
        return []
    return [json.loads(x) for x in path.read_text().splitlines()]


def wazuh_message(alert, command="add"):
    return {"version": 1, "origin": {"name": "node01", "module": "wazuh-execd"},
            "command": command, "parameters": {"extra_args": [], "alert": alert, "program": "x"}}
