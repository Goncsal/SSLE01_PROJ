"""Open transactions (in memory) and the meter checks of section 5.4.

The last reading of every open transaction is kept here, so a MeterValues message costs
no I/O; the CSMS copies the readings to the database every CSMS_METER_FLUSH_S seconds, and a
replica that gets a transaction it does not hold reloads it with `Transaction.from_row`. The checks are plain functions that tests can call.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass


@dataclass
class Transaction:
    transaction_id: int
    hier_id: str
    connector_id: int
    card: str
    meter_start_wh: int
    started_at: dt.datetime
    last_meter_wh: int = 0          # last energy register reading
    last_ts: dt.datetime | None = None
    last_power_w: float = 0.0
    peak_power_w: float = 0.0

    def __post_init__(self):
        self.last_meter_wh = self.meter_start_wh
        self.last_ts = self.started_at

    @classmethod
    def from_row(cls, row: dict) -> Transaction:
        """Rebuild an open transaction from its `sessions` row (baseline = last flushed reading)."""
        tx = cls(row["transaction_id"], row["hier_id"], row["connector_id"], row["card"],
                 row["meter_start_wh"], row["started_at"])
        if row["last_meter_wh"] is not None:
            tx.last_meter_wh = row["last_meter_wh"]
        if row["last_meter_at"] is not None:
            tx.last_ts = row["last_meter_at"]
        tx.peak_power_w = row["peak_power_w"] or 0.0
        return tx


@dataclass
class Anomaly:
    reason: str                      # backwards | over_power | over_energy | stop_mismatch
    meter_wh: int | None = None
    prev_meter_wh: int | None = None
    power_w: float | None = None


def check_reading(tx: Transaction, meter_wh: int | None, power_w: float | None,
                  ts: dt.datetime, max_kw: float | None, tolerance: float) -> list[Anomaly]:
    """Compare a reading with the previous one and with the charger's rating.

    `max_kw` is None when the registry gave no rating: the power checks are skipped.
    """
    found: list[Anomaly] = []
    if meter_wh is not None:
        if meter_wh < tx.last_meter_wh:
            found.append(Anomaly("backwards", meter_wh, tx.last_meter_wh, power_w))
        elif max_kw is not None:
            hours = max((ts - tx.last_ts).total_seconds(), 0.0) / 3600
            if meter_wh - tx.last_meter_wh > max_kw * hours * 1000 * tolerance + 10:
                found.append(Anomaly("over_energy", meter_wh, tx.last_meter_wh, power_w))
    if power_w is not None and max_kw is not None and power_w > max_kw * 1000 * tolerance:
        found.append(Anomaly("over_power", meter_wh, tx.last_meter_wh, power_w))
    return found


def record_reading(tx: Transaction, meter_wh: int | None, power_w: float | None,
                   ts: dt.datetime) -> int:
    """Store a reading as the new baseline. Returns the energy added since the last one (Wh)."""
    added = 0
    if meter_wh is not None:
        added = max(meter_wh - tx.last_meter_wh, 0)
        tx.last_meter_wh = meter_wh
    if power_w is not None:
        tx.last_power_w = power_w
        tx.peak_power_w = max(tx.peak_power_w, power_w)
    if ts > tx.last_ts:
        tx.last_ts = ts
    return added


class SessionTable:
    """The transactions that are open on this process."""

    def __init__(self):
        self._open: dict[int, Transaction] = {}

    def add(self, tx: Transaction) -> None:
        self._open[tx.transaction_id] = tx

    def get(self, transaction_id: int | None, hier_id: str) -> Transaction | None:
        """The open transaction with this id, only if it belongs to this charger."""
        tx = self._open.get(transaction_id)
        return tx if tx and tx.hier_id == hier_id else None

    def remove(self, transaction_id: int) -> None:
        self._open.pop(transaction_id, None)

    def forget(self, transaction_ids) -> None:
        for transaction_id in transaction_ids:
            self.remove(transaction_id)

    def all(self) -> list[Transaction]:
        return list(self._open.values())

    def of_charger(self, hier_id: str) -> list[Transaction]:
        return [tx for tx in self._open.values() if tx.hier_id == hier_id]

    def on_station(self, station_prefix: str) -> list[Transaction]:
        """Open transactions of the chargers whose id starts with `<station>.`."""
        return [tx for tx in self._open.values() if tx.hier_id.startswith(station_prefix + ".")]
