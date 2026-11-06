import pytest
from pydantic import ValidationError

from evcommon import roster

SAMPLE = """
version: 1
country: pt
cities:
  - city: porto
    name: Porto
    stations:
      - station: st-001
        name: Porto - Aliados
        lat: 41.1496
        lon: -8.6109
        dev_metrics_port: 9301
        chargers:
          - {charger: cp-1, max_kw: 22}
          - {charger: cp-2, max_kw: 50, model: EVL-DC50, serial: EVL50-0001}
  - city: aveiro
    plain_ws: true
    stations:
      - station: st-001
        name: Aveiro - Forum
        lat: 40.6405
        lon: -8.6538
        dev_metrics_port: 9302
        chargers:
          - {charger: cp-1, max_kw: 11}
cards:
  - {card: 04A1B2C3D4E5F6, holder: driver-0001, home_city: porto}
  - {card: 04A1B2C3D4E5F7, holder: driver-0002, home_city: aveiro, blocked: true}
"""


def _load(tmp_path, text=SAMPLE):
    p = tmp_path / "roster.yaml"
    p.write_text(text)
    return roster.load_roster(p)


def test_load_and_iterate(tmp_path):
    r = _load(tmp_path)
    assert [e.hier_id for e in r.iter_chargers()] == [
        "pt.porto.st-001.cp-1", "pt.porto.st-001.cp-2", "pt.aveiro.st-001.cp-1"]
    city, st = r.station("pt.aveiro.st-001")
    assert city.plain_ws and st.chargers[0].max_kw == 11
    assert [c.card for c in r.cards_for_city("porto")] == ["04A1B2C3D4E5F6"]
    with pytest.raises(KeyError):
        r.station("pt.lisboa.st-001")


def test_round_trip(tmp_path):
    r = _load(tmp_path)
    out = tmp_path / "out.yaml"
    roster.dump_roster(r, out)
    assert roster.load_roster(out) == r


def test_rejects_bad_rosters(tmp_path):
    with pytest.raises(ValidationError):
        _load(tmp_path, SAMPLE.replace("cp-2", "cp-1"))
    with pytest.raises(ValidationError):
        _load(tmp_path, SAMPLE.replace("home_city: aveiro", "home_city: braga"))
    with pytest.raises(ValidationError):
        _load(tmp_path, SAMPLE.replace("9302", "9301"))
    with pytest.raises(ValidationError):
        _load(tmp_path, SAMPLE.replace("st-001\n        name: Aveiro", "st-1\n        name: Aveiro"))
