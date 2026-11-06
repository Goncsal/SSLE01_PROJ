import datetime as dt
import json
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from evcommon import jsonlog
from evcommon.roster import load_roster
from evfraud.app import create_app
from evfraud.settings import Settings
from fastapi.testclient import TestClient

ROSTER = Path(__file__).resolve().parents[2] / "dev" / "roster.yaml"
PORTO = "pt.porto.st-001.cp-1"        # 22 kW
LISBOA = "pt.lisboa.st-001.cp-1"
CARD = "04A1B2C3D4E5F6"
T0 = dt.datetime(2026, 11, 6, 10, 0, tzinfo=dt.UTC)
TOKEN_LEN = 24


def iso(t: dt.datetime) -> str:
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


class Stub:
    """A tiny HTTP server on a free port that records every request.

    GET /nodes/<id> answers from the roster (stations and cities), GET /connections from
    `connections`; everything else answers 200 with {}.
    """

    def __init__(self, nodes=None, connections=()):
        self.requests: list[dict] = []
        self.nodes = nodes or {}
        self.connections = list(connections)
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _serve(self):
                n = int(self.headers.get("content-length") or 0)
                raw = self.rfile.read(n) if n else b""
                body = json.loads(raw) if raw else None
                path = self.path.split("?")[0]
                stub.requests.append({"method": self.command, "path": path, "query": self.path,
                                      "auth": self.headers.get("authorization"), "body": body})
                status, out = 200, {}
                if self.command == "GET" and path.startswith("/nodes/"):
                    node = stub.nodes.get(path[len("/nodes/"):])
                    status, out = (200, node) if node else (404, {"detail": "not found"})
                elif self.command == "GET" and path == "/connections":
                    out = {"count": len(stub.connections), "items": stub.connections}
                data = json.dumps(out).encode()
                self.send_response(status)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            do_GET = do_POST = _serve

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def posts(self):
        return [r for r in self.requests if r["method"] == "POST"]


def roster_nodes() -> dict:
    nodes = {}
    for sid, city, st in load_roster(ROSTER).iter_stations():
        nodes[sid] = {"hier_id": sid, "lat": st.lat, "lon": st.lon}
        nodes[f"pt.{city.city}"] = {"hier_id": f"pt.{city.city}", "lat": city.edge_lat, "lon": city.edge_lon}
    return nodes


class Api:
    def __init__(self, client, token, log_dir, app):
        self.c, self.token, self.log_dir, self.app = client, token, log_dir, app
        self.scorer = app.state.scorer

    def call(self, method, path, auth=True, **kw):
        headers = {"Authorization": f"Bearer {self.token}"} if auth else {}
        return self.c.request(method, path, headers=headers, **kw)

    def get(self, path, **kw):
        return self.call("GET", path, **kw)

    def post(self, path, json=None, **kw):
        return self.call("POST", path, json=json if json is not None else {}, **kw)

    def log_lines(self, event=None):
        lines = []
        for f in self.log_dir.glob("fraud-*.json"):
            lines += [json.loads(x) for x in f.read_text().splitlines()]
        return [x for x in lines if event is None or x["event"] == event]

    def event(self, kind, hier_id=PORTO, ts=T0, **kw):
        city, station = hier_id.split(".")[1:3]
        body = {"event": kind, "ts": iso(ts), "hier_id": hier_id, "city": city, "station": station,
                "max_kw": kw.pop("max_kw", 22.0)}
        body.update(kw)
        return self.post("/events", body)

    def alerts(self, **params):
        return self.get("/alerts", params=params).json()["items"]


@pytest.fixture
def stubs():
    made = {"registry": Stub(nodes=roster_nodes()), "billing": Stub(), "csms": Stub(),
            "correlator": Stub()}
    yield made
    for s in made.values():
        s.close()


@pytest.fixture
def make_api(tmp_path, stubs):
    clients = []

    def make(dry_run=False, **overrides):
        token = secrets.token_urlsafe(TOKEN_LEN)
        jsonlog.setup_logging("fraud", instance="test", log_dir=tmp_path, stdout=False)
        fields = dict(
            api_token=token, database_url=f"sqlite:///{tmp_path}/fraud-{len(clients)}.db",
            dry_run=dry_run, registry_url=stubs["registry"].url,
            registry_responder_token="registry-responder-token",
            billing_url=stubs["billing"].url, billing_responder_token="billing-responder-token",
            csms_api_url=stubs["csms"].url, csms_responder_token="csms-responder-token",
            correlator_url=stubs["correlator"].url, correlator_api_token="correlator-api-token-x")
        fields.update(overrides)
        app = create_app(Settings(**fields))
        client = TestClient(app)
        client.__enter__()
        clients.append(client)
        return Api(client, token, tmp_path, app)

    yield make
    for c in clients:
        c.__exit__(None, None, None)


@pytest.fixture
def api(make_api):
    return make_api()
