"""The Wazuh integration script runs on the manager's interpreter; here it is imported as a module."""

import http.server
import importlib.machinery
import importlib.util
import json
import threading
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "wazuh" / "integrations" / "custom-correlator"


@pytest.fixture
def integ(tmp_path, monkeypatch):
    monkeypatch.setenv("EVCHARGE_AR_ETC", str(tmp_path))
    monkeypatch.setenv("EVCHARGE_OUTBOX", str(tmp_path / "outbox.json"))
    loader = importlib.machinery.SourceFileLoader("custom_correlator", str(SCRIPT))
    spec = importlib.util.spec_from_loader("custom_correlator", loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


class Sink(http.server.BaseHTTPRequestHandler):
    received: list = []

    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        Sink.received.append((self.path, self.headers.get("Authorization"), json.loads(body)))
        self.send_response(202)
        self.end_headers()
        self.wfile.write(b"{}")

    def log_message(self, *args):
        pass


def serve():
    Sink.received = []
    server = http.server.HTTPServer(("127.0.0.1", 0), Sink)      # an ephemeral port, never a fixed one
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


ALERT = {"id": "1.1", "rule": {"id": "110104", "level": 7}, "data": {"hier_id": "pt.porto.st-003.cp-2"}}


def test_build_request_keeps_the_alert_and_adds_the_bearer(integ):
    req = integ.build_request("http://correlator.test/signals/wazuh", ALERT, "tok")
    assert json.loads(req.data) == ALERT
    assert req.get_header("Authorization") == "Bearer tok" and req.get_method() == "POST"
    assert integ.build_request("http://x", ALERT, "").get_header("Authorization") is None


def test_target_and_token_come_from_ar_env(integ, tmp_path):
    (tmp_path / "ar.env").write_text("# c\nCORRELATOR_API_TOKEN='abc'\nCORRELATOR_URL=http://c.test/signals/wazuh\n")
    env = integ.read_env()
    assert env["CORRELATOR_API_TOKEN"] == "abc"
    assert integ.target_url("http://hook", env) == "http://c.test/signals/wazuh"
    assert integ.target_url("http://hook", {}) == "http://hook"
    assert integ.target_url(None, {}) == integ.DEFAULT_URL


def test_failure_goes_to_the_outbox_and_replay_drains_it(integ, tmp_path):
    assert integ.forward([ALERT, {**ALERT, "id": "1.2"}], "http://127.0.0.1:9/signals/wazuh", "tok") == 0
    assert len((tmp_path / "outbox.json").read_text().splitlines()) == 2
    server = serve()
    try:
        url = "http://127.0.0.1:%d/signals/wazuh" % server.server_address[1]
        assert integ.replay(url, "tok") == 2
        assert [r[2]["id"] for r in Sink.received] == ["1.1", "1.2"]
        assert Sink.received[0][1] == "Bearer tok"
        assert (tmp_path / "outbox.json").read_text() == ""
        assert integ.forward([ALERT], url, "tok") == 1
    finally:
        server.shutdown()
        server.server_close()


def test_main_reads_the_alert_file(integ, tmp_path):
    alert_file = tmp_path / "alert.json"
    alert_file.write_text(json.dumps(ALERT) + "\n")
    assert integ.main(["custom-correlator", str(alert_file), "key", "http://127.0.0.1:9/x"]) == 0
    assert (tmp_path / "outbox.json").exists()
    assert integ.main(["custom-correlator"]) == 1
    assert integ.main(["custom-correlator", str(tmp_path / "missing")]) == 1


def test_outbox_is_capped(integ, tmp_path, monkeypatch):
    monkeypatch.setattr(integ, "OUTBOX_MAX", 10)
    integ.to_outbox(ALERT)
    size = (tmp_path / "outbox.json").stat().st_size
    integ.to_outbox(ALERT)
    assert (tmp_path / "outbox.json").stat().st_size == size
