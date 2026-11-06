import json
import secrets

import pytest
from evcommon import jsonlog
from evregistry.app import create_app
from evregistry.settings import Settings
from fastapi.testclient import TestClient


class Api:
    """TestClient plus the tokens of this test run."""

    def __init__(self, client, tokens, log_dir):
        self.c, self.tokens, self.log_dir = client, tokens, log_dir

    def call(self, method, path, who=None, **kw):
        headers = {"Authorization": f"Bearer {self.tokens[who]}"} if who else {}
        return self.c.request(method, path, headers=headers, **kw)

    def get(self, path, who=None, **kw):
        return self.call("GET", path, who, **kw)

    def put(self, path, who="admin", json=None, **kw):
        return self.call("PUT", path, who, json=json if json is not None else {}, **kw)

    def log_lines(self):
        lines = []
        for f in self.log_dir.glob("registry-*.json"):
            lines += [json.loads(x) for x in f.read_text().splitlines()]
        return lines


@pytest.fixture
def api(tmp_path):
    tokens = {"admin": secrets.token_urlsafe(24), "porto": secrets.token_urlsafe(24),
              "lisboa": secrets.token_urlsafe(24),
              "responder": secrets.token_urlsafe(24)}
    settings = Settings(admin_token=tokens["admin"], responder_token=tokens["responder"],
                        city_tokens={"porto": tokens["porto"], "lisboa": tokens["lisboa"]},
                        database_url=f"sqlite:///{tmp_path}/registry.db")
    jsonlog.setup_logging("registry", instance="test", log_dir=tmp_path, stdout=False)
    with TestClient(create_app(settings)) as client:
        yield Api(client, tokens, tmp_path)


@pytest.fixture
def porto(api):
    """pt.porto with station st-001 (located) and chargers cp-1, cp-2."""
    assert api.put("/nodes/pt.porto", json={"name": "Porto"}).status_code == 201
    assert api.put("/nodes/pt.porto.edge", "porto").status_code == 201
    assert api.put("/nodes/pt.porto.st-001", "porto", json={
        "name": "Aliados", "lat": 41.1496, "lon": -8.6109, "host": "porto-st-001",
        "ip": "10.31.0.13", "network": "city-porto", "dns": "st-001.porto.pt.lab",
        "metrics_target": "st-001.porto.pt.lab:9300"}).status_code == 201
    for cp in ("cp-1", "cp-2"):
        assert api.put(f"/nodes/pt.porto.st-001.{cp}", "porto", json={"max_kw": 22}).status_code == 201
    return api
