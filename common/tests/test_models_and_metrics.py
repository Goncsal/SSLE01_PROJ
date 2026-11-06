import datetime as dt

import pytest
from pydantic import ValidationError

from evcommon import metrics, models


def test_registry_models():
    body = models.NodeIn(name="Porto - Aliados", lat=41.1496, lon=-8.6109)
    assert body.model_dump(exclude_none=True) == {"name": "Porto - Aliados", "lat": 41.1496, "lon": -8.6109}
    with pytest.raises(ValidationError):
        models.NodeIn(lat=91)
    with pytest.raises(ValidationError):
        models.NodeIn(unknown_field=1)
    q = models.QuarantineRequest(reason="duplicate id", source="wazuh", incident_id="inc-1")
    assert q.source == "wazuh"
    with pytest.raises(ValidationError):
        models.QuarantineRequest(reason="")


def test_billing_models():
    now = dt.datetime.now(dt.UTC)
    models.AuthorizeRequest(card="04A1B2C3D4E5F6", hier_id="pt.porto.st-003.cp-2")
    with pytest.raises(ValidationError):
        models.AuthorizeRequest(card="04a1b2c3", hier_id="pt.porto.st-003.cp-2")
    with pytest.raises(ValidationError):
        models.AuthorizeRequest(card="04A1B2C3", hier_id="pt.porto.st-003")
    s = models.SessionStart(transaction_id=1, card="04A1B2C3D4E5F6", hier_id="pt.porto.st-003.cp-2",
                            connector_id=1, meter_start_wh=1000, started_at=now)
    assert s.model_dump(mode="json")["started_at"].startswith(str(now.year))


def test_csms_models():
    r = models.ChangeAvailabilityRequest(type="Inoperative", reason="quarantine", source="wazuh")
    assert r.connector_id == 0
    with pytest.raises(ValidationError):
        models.ResetRequest(type="Warm")
    res = models.CommandResult(cmd_id="c1", hier_id="pt.porto.st-003.cp-2",
                               command="change-availability", status="Accepted", pod="csms-0")
    assert res.status == "Accepted"


def test_fraud_alert_model():
    a = models.FraudAlert(alert_id="a1", ts=dt.datetime.now(dt.UTC), rule="impossible_travel",
                          severity="critical", card="04A1B2C3D4E5F6", hier_id="pt.porto.st-001.cp-1",
                          other_hier_id="pt.lisboa.st-001.cp-1", city="porto", score=0.95,
                          detail="x", evidence={"distance_km": 274.1})
    assert models.FraudAlert.model_validate(a.model_dump(mode="json")) == a
    with pytest.raises(ValidationError):
        models.FraudAlert(alert_id="a1", ts=dt.datetime.now(dt.UTC), rule="r", severity="fatal",
                          score=0.5, detail="x")
    with pytest.raises(ValidationError):
        models.FraudAlert(alert_id="a1", ts=dt.datetime.now(dt.UTC), rule="r", severity="info",
                          score=1.5, detail="x")


def test_metric_specs_are_consistent():
    names = [s.name for s in metrics.ALL]
    assert len(names) == len(set(names))
    for s in metrics.ALL:
        assert s.name.startswith("evcharge_")
        if s.type == "counter":
            assert s.name.endswith("_total")
        if s.scope == "per_charger":
            assert "charger" in s.labels and s.name.startswith("evcharge_sim_")
        if s.name.startswith("evcharge_csms_"):
            assert "charger" not in s.labels  # CSMS is scraped centrally
    for r in metrics.CITY_RECORDING_RULES:
        assert r.startswith("city:")
    assert len(set(metrics.ALERT_NAMES)) == len(metrics.ALERT_NAMES)


def test_make_and_render():
    prometheus_client = pytest.importorskip("prometheus_client")
    reg = prometheus_client.CollectorRegistry()
    metrics.init_exposition()
    c = metrics.make(metrics.CSMS_CONNECTIONS_TOTAL, registry=reg)
    c.labels(city="porto", result="accepted").inc()
    g = metrics.make(metrics.SIM_CHARGER_CONNECTED, registry=reg)
    g.labels(hier_id="pt.porto.st-003.cp-2", city="porto", station="st-003", charger="cp-2").set(1)
    h = metrics.make(metrics.CSMS_OCPP_HANDLE_SECONDS, registry=reg)
    h.labels(action="Heartbeat").observe(0.002)
    body, ctype = metrics.render(reg)
    text = body.decode()
    assert 'evcharge_csms_connections_total{city="porto",result="accepted"} 1.0' in text
    assert "_created" not in text
    assert ctype.startswith("text/plain")
