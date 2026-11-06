import datetime as dt

import httpx
import pytest
from conftest import PORTO, T0
from evcommon import config
from evfraud.grid import GridPoller, parse_samples
from evfraud.settings import Settings

TOKEN = "t" * 24


def prom_body(*series):
    return {"status": "success", "data": {"resultType": "vector", "result": [
        {"metric": m, "value": [1700000000, str(v)]} for m, v in series]}}


def test_parse_samples_skips_bad_series():
    body = prom_body(({"city": "porto", "hier_id": "pt.porto.st-001"}, 1500.5),
                     ({"city": "porto"}, 3), ({"hier_id": "pt.porto.st-002"}, 3),
                     ({"city": "porto", "hier_id": "pt.porto.st-003"}, "NaNx"))
    assert parse_samples(body) == [("pt.porto.st-001", "porto", 1500.5)]


def test_poller_feeds_grid_gap(api):
    api.event("session.stop", PORTO, T0, card="04A1B2C3D4E5F6", transaction_id=1,
              energy_wh=1000, duration_s=600.0)
    seen = []

    def handler(request):
        assert request.url.path == "/api/v1/query"
        return httpx.Response(200, json=prom_body(({"city": "porto", "hier_id": "pt.porto.st-001"}, 10000)))

    client = httpx.Client(base_url="http://prom.test", transport=httpx.MockTransport(handler))
    poller = GridPoller(api.scorer, api.app.state.settings, seen.append, client)
    assert poller.poll_once(seconds=3600.0) == 1          # 10 kWh against the 1 kWh the CSMS saw
    assert [r.alert.rule for batch in seen for r in batch] == ["grid_gap"]


def test_poller_survives_a_dead_prometheus(api):
    def handler(request):
        raise httpx.ConnectError("down")

    client = httpx.Client(base_url="http://prom.test", transport=httpx.MockTransport(handler))
    assert GridPoller(api.scorer, api.app.state.settings, lambda r: None, client).poll_once(30.0) == 0
    assert api.log_lines("dependency.error")


def test_settings_from_env_defaults():
    s = Settings.from_env({"FRAUD_API_TOKEN": TOKEN, "DATABASE_URL": "sqlite:///:memory:"})
    assert (s.port, s.alert_score, s.max_kmh, s.energy_tol, s.peer_factor, s.grid_factor) == (
        8003, 0.7, 200.0, 1.15, 0.5, 0.8)
    assert (s.window_h, s.min_sessions, s.max_sessions_h, s.dry_run) == (6.0, 20, 8, False)


def test_settings_from_env_overrides_and_checks():
    s = Settings.from_env({"FRAUD_API_TOKEN": TOKEN, "DATABASE_URL": "sqlite:///:memory:",
                           "FRAUD_DRY_RUN": "true", "FRAUD_MAX_KMH": "120", "FRAUD_MIN_SESSIONS": "5"})
    assert s.dry_run and s.max_kmh == 120.0 and s.min_sessions == 5
    with pytest.raises(config.ConfigError):
        Settings.from_env({})
    with pytest.raises(config.ConfigError):
        Settings.from_env({"FRAUD_API_TOKEN": "short"})
    with pytest.raises(config.ConfigError):
        Settings.from_env({"FRAUD_API_TOKEN": TOKEN, "BILLING_RESPONDER_TOKEN": "short"})
