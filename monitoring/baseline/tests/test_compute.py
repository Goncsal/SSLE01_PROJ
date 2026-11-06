import math
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import compute  # noqa: E402

ROSTER = {"cities": [
    {"city": "porto", "stations": [{"chargers": [{"max_kw": 22.0}, {"max_kw": 50.0}]}]},
    {"city": "aveiro", "stations": [{"chargers": [{"max_kw": 100.0}]}]},
]}


def payload(vals):
    return {"status": "success", "data": {"resultType": "matrix", "result": [
        {"metric": {}, "values": [[1000 + 60 * i, str(v)] for i, v in enumerate(vals)]}]}}


def test_stats_mean_sigma_upper():
    s = compute.stats([2, 4, 4, 4, 5, 5, 7, 9])
    assert s["n"] == 8 and s["mean"] == 5 and s["sigma"] == 2 and s["upper"] == 11


def test_stats_empty():
    assert compute.stats([])["n"] == 0


def test_extract_skips_nan_and_checks_status():
    p = payload([1, "NaN", 3])
    assert compute.extract_values(p) == [1.0, 3.0]
    with pytest.raises(RuntimeError):
        compute.extract_values({"status": "error", "error": "boom"})


def test_capacities_watts():
    assert compute.capacities(ROSTER) == {"porto": 72000.0, "aveiro": 100000.0}


def test_parse_window():
    assert compute.parse_window("6h").total_seconds() == 6 * 3600
    assert compute.parse_window("90m").total_seconds() == 5400
    with pytest.raises(ValueError):
        compute.parse_window("soon")


def test_load_upper_capped_at_90_percent():
    caps = compute.capacities(ROSTER)
    samples = {"porto": {"load": [60000.0, 70000.0] * 20, "msgs": [1.0] * 40, "auth": [0.0] * 40}}
    r = compute.analyse(samples, caps)["porto"]["load"]
    assert r["capped"] and r["upper"] == pytest.approx(0.9 * 72000) and r["upper_raw"] > r["upper"]


def test_load_not_capped_when_below():
    caps = compute.capacities(ROSTER)
    samples = {"aveiro": {"load": [20000.0, 30000.0] * 20, "msgs": [1.0] * 40, "auth": [0.0] * 40}}
    r = compute.analyse(samples, caps)["aveiro"]["load"]
    assert not r["capped"] and r["upper"] == pytest.approx(25000 + 3 * 5000)


def test_render_is_valid_baseline_file():
    caps = compute.capacities(ROSTER)
    samples = {c: {"load": [1000.0, 2000.0] * 20, "msgs": [1.0, 2.0] * 20, "auth": [0.0, 2.0] * 20}
               for c in caps}
    end = datetime(2026, 11, 7, 12, 0, tzinfo=timezone.utc)
    text = compute.render(compute.analyse(samples, caps), end - compute.parse_window("24h"), end, "24h", caps)
    assert text.startswith("# Baseline thresholds")
    assert 'basis="24h-20261107"' in text
    doc = yaml.safe_load(text)
    rules = doc["groups"][0]["rules"]
    assert len(rules) == 10
    byname = {(r["record"], r["labels"]["city"]): r for r in rules}
    assert byname[("baseline:city_auth_failures_10m:upper", "porto")]["expr"] == "vector(4)"
    assert byname[("baseline:city_msgs_rate:upper", "aveiro")]["labels"]["basis"] == "24h-20261107"
    assert math.isclose(float(byname[("baseline:city_msgs_rate:mean", "aveiro")]["expr"][7:-1]), 1.5)


def test_main_with_mocked_api(monkeypatch, tmp_path, capsys):
    import httpx

    roster = tmp_path / "roster.yaml"
    roster.write_text(yaml.safe_dump(ROSTER))

    def handler(request):
        q = request.url.params["query"]
        assert request.url.params["step"] == "60"
        vals = [1000.0, 2000.0] * 20 if "grid_power" in q else [1.0, 2.0] * 20
        return httpx.Response(200, json=payload(vals))

    real = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    rc = compute.main(["--prometheus", "http://prom:9090", "--window", "6h",
                       "--end", "2026-11-07T12:00:00Z", "--roster", str(roster)])
    assert rc == 0
    out = capsys.readouterr().out
    assert 'basis: "6h-20261107"' in out
    assert yaml.safe_load(out)["groups"][0]["name"] == "baseline"


def test_main_refuses_too_few_samples(monkeypatch, tmp_path):
    import httpx

    roster = tmp_path / "r.yaml"
    roster.write_text(yaml.safe_dump(ROSTER))
    real = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda **kw: real(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json=payload([1.0]))), **kw))
    assert compute.main(["--prometheus", "http://p", "--roster", str(roster)]) == 2
