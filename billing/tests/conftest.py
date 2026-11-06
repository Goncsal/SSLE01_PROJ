import json
import secrets

import pytest
from evbilling.app import create_app
from evbilling.settings import Settings
from evcommon import jsonlog
from fastapi.testclient import TestClient

CHARGER = "pt.porto.st-001.cp-1"
CARD = "04A1B2C3D4E5F6"


class Api:
    """TestClient with the token of this test run."""

    def __init__(self, client, token, log_dir):
        self.c, self.token, self.log_dir = client, token, log_dir

    def call(self, method, path, auth=True, **kw):
        headers = {"Authorization": f"Bearer {self.token}"} if auth else {}
        return self.c.request(method, path, headers=headers, **kw)

    def get(self, path, **kw):
        return self.call("GET", path, **kw)

    def post(self, path, json=None, **kw):
        return self.call("POST", path, json=json if json is not None else {}, **kw)

    def log_lines(self, event=None):
        lines = []
        for f in self.log_dir.glob("billing-*.json"):
            lines += [json.loads(x) for x in f.read_text().splitlines()]
        return [x for x in lines if event is None or x["event"] == event]

    def start(self, tid=1, card=CARD, hier_id=CHARGER, meter=1000, at="2026-11-06T09:00:00Z"):
        return self.post("/sessions", {"transaction_id": tid, "card": card, "hier_id": hier_id,
                                       "connector_id": 1, "meter_start_wh": meter, "started_at": at})

    def stop(self, tid=1, meter=4666, at="2026-11-06T09:10:00Z"):
        return self.post(f"/sessions/{tid}/stop", {"meter_stop_wh": meter, "stopped_at": at})


def make_settings(tmp_path, token, roster=""):
    return Settings(api_token=token, database_url=f"sqlite:///{tmp_path}/billing.db", seed_roster=roster)


@pytest.fixture
def make_api(tmp_path):
    clients = []

    def make(roster=""):
        token = secrets.token_urlsafe(24)
        jsonlog.setup_logging("billing", instance="test", log_dir=tmp_path, stdout=False)
        client = TestClient(create_app(make_settings(tmp_path, token, roster)))
        client.__enter__()
        clients.append(client)
        return Api(client, token, tmp_path)

    yield make
    for c in clients:
        c.__exit__(None, None, None)


@pytest.fixture
def api(make_api):
    """Empty billing (default tariff only) with one active card."""
    a = make_api()
    assert a.post("/cards", {"card": CARD, "holder": "driver-0001", "home_city": "porto"}).status_code == 201
    return a
