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
    for n in (1, 2):
        api.event("session.stop", PORTO, T0 + dt.timedelta(minutes=n), card="04A1B2C3D4E5F6",
                  transaction_id=n, energy_wh=500, duration_s=600.0)
    api.scorer.record_grid_sample("pt.porto.st-001", "porto", 10000.0, 3600.0, T0 + dt.timedelta(minutes=10))
    seen = []

    def handler(request):
        assert request.url.path == "/api/v1/query"
        return httpx.Response(200, json=prom_body(({"city": "porto", "hier_id": "pt.porto.st-001"}, 10000)))

    client = httpx.Client(base_url="http://prom.test", transport=httpx.MockTransport(handler))
    poller = GridPoller(api.scorer, api.app.state.settings, seen.append, client)
    assert poller.poll_once(seconds=30.0) == 1            # 10 kWh settled on the feeder, 1 kWh at the CSMS
    assert [r.alert.rule for batch in seen for r in batch] == ["grid_gap"]


def test_poller_feeds_power_gap_from_the_gap_series(api):
    st = "pt.porto.st-001"
    meta = {"city": "porto", "hier_id": st}
    seen = []

    def handler(request):
        q = request.url.params["query"]
        assert q in ("city:station_grid_power_watts:sum", "city:station_power_gap_watts")
        return httpx.Response(200, json=prom_body((meta, 12000 if "grid" in q else 6000)))

    client = httpx.Client(base_url="http://prom.test", transport=httpx.MockTransport(handler))
    poller = GridPoller(api.scorer, api.app.state.settings, seen.append, client)
    for _ in range(12):
        assert poller.poll_once(seconds=30.0) == 1
    assert [r.alert.rule for batch in seen for r in batch if r.finding.action] == ["power_gap"]


def test_poller_without_the_gap_series_never_quarantines(api):
    meta = {"city": "porto", "hier_id": "pt.porto.st-001"}

    def handler(request):
        if "gap" in request.url.params["query"]:
            return httpx.Response(500)
        return httpx.Response(200, json=prom_body((meta, 12000)))

    seen = []
    client = httpx.Client(base_url="http://prom.test", transport=httpx.MockTransport(handler))
    poller = GridPoller(api.scorer, api.app.state.settings, seen.append, client)
    for _ in range(30):
        assert poller.poll_once(seconds=30.0) == 1
    assert [r for batch in seen for r in batch if r.finding.action] == []


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
    assert (s.block_kmh, s.concurrent_max_age_s) == (400.0, 3600.0)
    assert (s.power_gap_w, s.power_gap_ratio, s.power_gap_window_s, s.power_gap_min_samples,
            s.power_gap_frac) == (1000.0, 0.3, 900.0, 10, 0.8)


def test_settings_from_env_overrides_and_checks():
    s = Settings.from_env({"FRAUD_API_TOKEN": TOKEN, "DATABASE_URL": "sqlite:///:memory:",
                           "FRAUD_DRY_RUN": "true", "FRAUD_MAX_KMH": "120", "FRAUD_MIN_SESSIONS": "5"})
    assert s.dry_run and s.max_kmh == 120.0 and s.min_sessions == 5
    k = Settings.from_env({"FRAUD_API_TOKEN": TOKEN, "DATABASE_URL": "sqlite:///:memory:",
                           "FRAUD_BLOCK_KMH": "500", "FRAUD_POWER_GAP_W": "2000",
                           "FRAUD_POWER_GAP_MIN_SAMPLES": "4"})
    assert (k.block_kmh, k.power_gap_w, k.power_gap_min_samples) == (500.0, 2000.0, 4)
    with pytest.raises(config.ConfigError):      # block speed under the alert speed
        Settings.from_env({"FRAUD_API_TOKEN": TOKEN, "DATABASE_URL": "sqlite:///:memory:",
                           "FRAUD_BLOCK_KMH": "100"})
    with pytest.raises(config.ConfigError):
        Settings.from_env({})
    with pytest.raises(config.ConfigError):
        Settings.from_env({"FRAUD_API_TOKEN": "short"})
    with pytest.raises(config.ConfigError):
        Settings.from_env({"FRAUD_API_TOKEN": TOKEN, "BILLING_RESPONDER_TOKEN": "short"})
