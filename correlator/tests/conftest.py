"""Fixtures: the app wired to a fake registry (canned /locate answers) and a settable clock."""

import datetime as dt
import json
import secrets

import httpx
import pytest
from evcommon import hierid
from evcorrelator.app import create_app
from evcorrelator.locate import Locator
from evcorrelator.settings import Settings
from fastapi.testclient import TestClient
from pydantic import TypeAdapter

T0 = dt.datetime(2026, 11, 6, 10, 0, 0, tzinfo=dt.UTC)

# station -> address and host; the topology is porto (st-001..st-003) and lisboa (st-001)
STATION_IPS = {
    "pt.porto.st-001": "10.31.0.11",
    "pt.porto.st-002": "10.31.0.12",
    "pt.porto.st-003": "10.31.0.13",
    "pt.lisboa.st-001": "10.32.0.11",
}
EDGE_IPS = {"pt.porto.edge": "10.31.0.2"}


def iso(offset_s: float = 0.0, base: dt.datetime = T0) -> str:
    return (base + dt.timedelta(seconds=offset_s)).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def ts(offset_s: float = 0.0) -> str:
    """The time as the API serialises it (pydantic drops zero fractions)."""
    return json.loads(TypeAdapter(dt.datetime).dump_json(T0 + dt.timedelta(seconds=offset_s)))


def match(hier_id: str) -> dict:
    """What the registry returns for one element (section 4.1)."""
    parsed = hierid.parse(hier_id)
    station = str(parsed.station_id) if parsed.station_id else None
    host = ip = None
    if station:
        host = f"{parsed.city}-{parsed.station}"
        ip = STATION_IPS.get(station)
    elif parsed.kind == "edge":
        host, ip = f"{parsed.city}-edge", EDGE_IPS.get(hier_id)
    return {"hier_id": hier_id, "kind": parsed.kind, "state": "active", "effective_state": "active",
            "path": parsed.ancestry(), "operator": parsed.city or "noc", "edge": str(parsed.edge_id) if parsed.edge_id else None,
            "station": station, "host": host, "ip": ip, "network": f"city-{parsed.city}", "dns": None}


class FakeRegistry:
    """httpx transport answering /locate/{id} and /locate/ip/{ip}; counts the calls."""

    def __init__(self):
        self.calls: list[str] = []
        self.down = False

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.calls.append(path)
        if self.down:
            raise httpx.ConnectError("registry down")
        if path.startswith("/locate/ip/"):
            ip = path.removeprefix("/locate/ip/")
            owners = [s for s, a in {**STATION_IPS, **EDGE_IPS}.items() if a == ip]
            matches = [match(o) for o in owners]
            if owners and owners[0] in STATION_IPS:      # the chargers of the station follow it
                matches += [match(f"{owners[0]}.cp-{n}") for n in (1, 2)]
        else:
            hid = path.removeprefix("/locate/")
            matches = [match(hid)] if hierid.is_valid(hid) else []
        return httpx.Response(200, json={"query": path, "count": len(matches), "truncated": False,
                                         "matches": matches})


class Clock:
    def __init__(self):
        self.now = T0

    def __call__(self):
        return self.now

    def set(self, offset_s: float):
        self.now = T0 + dt.timedelta(seconds=offset_s)


class Api:
    def __init__(self, client, token, registry, clock, settings):
        self.c, self.token, self.registry, self.clock, self.settings = client, token, registry, clock, settings

    def post(self, path, body, token=True, **kw):
        headers = {"Authorization": f"Bearer {self.token}"} if token is True else (
            {"Authorization": f"Bearer {token}"} if token else {})
        return self.c.post(path, json=body, headers=headers, **kw)

    def get(self, path, token=True, **kw):
        headers = {"Authorization": f"Bearer {self.token}"} if token else {}
        return self.c.get(path, headers=headers, **kw)

    def incidents(self, **params):
        resp = self.get("/incidents", params=params)
        assert resp.status_code == 200, resp.text
        return resp.json()["items"]

    # ---- signal builders ----
    def prom(self, alertname, hier_id, threat, at=0.0, severity="warning", city=None):
        city = city or hier_id.split(".")[1]
        alert = {"status": "firing", "startsAt": iso(at),
                 "labels": {"alertname": alertname, "hier_id": hier_id, "city": city,
                            "threat": threat, "severity": severity},
                 "annotations": {"summary": alertname}}
        return self.post("/signals/alertmanager", {"version": "4", "status": "firing", "alerts": [alert]})

    def wazuh(self, event, rule_id, level, group, at=0.0, wid=None, **data):
        alert = {"id": wid or f"{rule_id}.{at}", "timestamp": iso(at).replace("Z", "+0000"),
                 "rule": {"id": rule_id, "level": level, "description": event, "groups": ["evcharge", group]},
                 "data": {"event": event, "ts": iso(at), **data}}
        return self.post("/signals/wazuh", alert)

    def fraud(self, rule, at=0.0, severity="critical", alert_id=None, **fields):
        return self.post("/signals/fraud", {"alert_id": alert_id or f"fa-{rule}-{at}", "ts": iso(at),
                                            "rule": rule, "severity": severity, "score": 0.9,
                                            "detail": rule, "evidence": {}, **fields})


def build(window="120", skew="3", close="600"):
    token = secrets.token_urlsafe(24)
    settings = Settings.from_env({"CORRELATOR_API_TOKEN": token, "REGISTRY_URL": "http://registry.test",
                                  "CORRELATOR_WINDOW_S": window, "CORRELATOR_SKEW_S": skew,
                                  "CORRELATOR_CLOSE_S": close})
    registry, clock = FakeRegistry(), Clock()
    locator = Locator(settings.registry_url,
                      client=httpx.Client(base_url=settings.registry_url, transport=httpx.MockTransport(registry)))
    app = create_app(settings, locator=locator, clock=clock)
    return Api(TestClient(app), token, registry, clock, settings)


@pytest.fixture(autouse=True)
def log_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("LOG_DIR", str(tmp_path / "logs"))


@pytest.fixture
def api():
    return build()


@pytest.fixture
def make_api():
    return build
