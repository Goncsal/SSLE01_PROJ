import json

import pytest

from evcommon import jsonlog


def _lines(path):
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def test_event_written_to_file(tmp_path):
    log = jsonlog.setup_logging("csms", instance="csms-0", log_dir=tmp_path, level="info", stdout=False)
    log.event("charger.duplicate_id", "id already connected", level="warning",
              hier_id="pt.porto.st-003.cp-2", srcip="10.31.0.66", other_srcip="10.31.0.13",
              tls=True, transaction_id=None)
    log.event("charger.status", "debug only", level="debug", hier_id="pt.porto.st-003.cp-2")
    path = tmp_path / "csms-csms-0.json"
    lines = _lines(path)
    assert len(lines) == 1
    line = lines[0]
    assert list(line)[:6] == ["ts", "level", "service", "instance", "event", "msg"]
    assert line["level"] == "warning"
    assert line["service"] == "csms"
    assert line["event"] == "charger.duplicate_id"
    assert line["city"] == "porto" and line["station"] == "st-003" and line["charger"] == "cp-2"
    assert line["srcip"] == "10.31.0.66"
    assert line["tls"] is True
    assert "transaction_id" not in line           # None values are dropped
    assert line["ts"].endswith("Z") and len(line["ts"]) == 24


def test_unknown_event_and_bad_fields_rejected(tmp_path):
    log = jsonlog.setup_logging("registry", instance="r", log_dir=tmp_path, stdout=False)
    with pytest.raises(ValueError):
        log.event("made.up", "x")
    with pytest.raises(ValueError):
        log.event("registry.write", "x", Event="bad")
    with pytest.raises(ValueError):
        log.event("registry.write", "x", service="override")
    jsonlog.register_events(["registry.custom_thing"])
    log.event("registry.custom_thing", "allowed after registration")


def test_library_records_become_service_log(tmp_path):
    import logging

    jsonlog.setup_logging("sim", instance="pt.porto.st-001", log_dir=tmp_path, stdout=False)
    logging.getLogger("websockets.client").warning("connection lost")
    logging.getLogger("websockets.client").info("ignored")
    lines = _lines(tmp_path / "sim-pt.porto.st-001.json")
    assert [x["event"] for x in lines] == ["service.log"]
    assert lines[0]["logger"] == "websockets.client"


def test_values_coerced(tmp_path):
    import datetime as dt
    from enum import StrEnum

    class S(StrEnum):
        a = "Accepted"

    line = jsonlog.build_line("x", "y", "auth.accept", "m", "info",
                              {"result": S.a, "at": dt.datetime(2026, 11, 5, tzinfo=dt.UTC),
                               "members": ("a", 1)})
    assert line["result"] == "Accepted"
    assert line["at"] == "2026-11-05T00:00:00.000Z"
    assert line["members"] == ["a", 1]


def test_vocabulary_names_are_well_formed():
    import re

    for name in jsonlog.EVENTS:
        assert re.match(r"^[a-z]+(\.[a-z_]+)+$", name), name
