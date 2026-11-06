"""Prometheus metrics for the station simulator (docs/interfaces.md, section 7.2).

Metric objects are created once, at import time, from the shared specs in
evcommon.metrics so names and label sets cannot drift from the contract.
"""

from __future__ import annotations

from evcommon import metrics as _m

CHARGER_CONNECTED = _m.make(_m.SIM_CHARGER_CONNECTED)
CHARGER_STATUS = _m.make(_m.SIM_CHARGER_STATUS)
POWER_WATTS = _m.make(_m.SIM_POWER_WATTS)
ENERGY_REGISTER_WH = _m.make(_m.SIM_ENERGY_REGISTER_WH)
OCPP_MESSAGES_SENT_TOTAL = _m.make(_m.SIM_OCPP_MESSAGES_SENT_TOTAL)
RECONNECTS_TOTAL = _m.make(_m.SIM_RECONNECTS_TOTAL)
SESSIONS_TOTAL = _m.make(_m.SIM_SESSIONS_TOTAL)
OCPP_CALL_SECONDS = _m.make(_m.SIM_OCPP_CALL_SECONDS)
STATION_GRID_POWER_WATTS = _m.make(_m.SIM_STATION_GRID_POWER_WATTS)

CHARGER_STATUS_CODES = _m.CHARGER_STATUS_CODES


def init_exposition() -> None:
    _m.init_exposition()


class GridMeter:
    """The station's feeder meter: the true total power of every connector.

    Unaffected by --tamper (docs/interfaces.md, 7.2): detection rules compare
    this against the (possibly tampered) values the chargers report.
    """

    def __init__(self, station_labels: dict[str, str]):
        self._labels = station_labels
        self._power: dict[str, float] = {}

    def set(self, key: str, watts: float) -> None:
        self._power[key] = watts
        STATION_GRID_POWER_WATTS.labels(**self._labels).set(sum(self._power.values()))
