"""Pricing, conversions and seeding; nothing here depends on HTTP."""

from __future__ import annotations

import datetime as dt
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

from evcommon.models import Card, Session, Tariff
from evcommon.roster import load_roster
from sqlalchemy import func, select
from sqlalchemy.orm import Session as Db

from .db import BlockedRow, CardRow, TariffRow, TransactionRow

CENT = Decimal("0.01")
DEFAULT_TARIFF = "standard"


def now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def price(tariff: TariffRow, energy_wh: int, started_at: dt.datetime, stopped_at: dt.datetime) -> Decimal:
    """fee + kWh x price_per_kwh + minutes x price_per_min, rounded half-up to cents."""
    kwh = Decimal(energy_wh) / 1000
    minutes = Decimal(max((stopped_at - started_at).total_seconds(), 0)) / 60
    total = tariff.session_fee_eur + kwh * tariff.price_per_kwh_eur + minutes * tariff.price_per_min_eur
    return total.quantize(CENT, rounding=ROUND_HALF_UP)


def open_block(db: Db, card: str) -> BlockedRow | None:
    """The block that is currently in force for a card, if any."""
    return db.scalars(select(BlockedRow).where(BlockedRow.card == card, BlockedRow.unblocked_at.is_(None))
                      .order_by(BlockedRow.id.desc())).first()


def blocked_count(db: Db) -> int:
    return db.scalar(select(func.count()).select_from(CardRow).where(CardRow.status == "blocked")) or 0


def to_session(row: TransactionRow) -> Session:
    return Session(
        transaction_id=row.transaction_id, card=row.card, hier_id=row.hier_id, city=row.city,
        connector_id=row.connector_id, tariff_id=row.tariff_id, started_at=row.started_at,
        stopped_at=row.stopped_at, meter_start_wh=row.meter_start_wh, meter_stop_wh=row.meter_stop_wh,
        energy_wh=row.energy_wh, cost_eur=None if row.cost_eur is None else float(row.cost_eur),
        status=row.status, flag_reason=row.flag_reason, stop_reason=row.stop_reason)


def to_card(db: Db, row: CardRow) -> Card:
    block = open_block(db, row.card) if row.status == "blocked" else None
    return Card(
        card=row.card, holder=row.holder, home_city=row.home_city, tariff_id=row.tariff_id,
        expiry_date=row.expiry_date, status=row.status,
        blocked_reason=block.reason if block else None, blocked_at=block.blocked_at if block else None)


def to_tariff(row: TariffRow) -> Tariff:
    return Tariff(
        tariff_id=row.tariff_id, price_per_kwh_eur=float(row.price_per_kwh_eur),
        price_per_min_eur=float(row.price_per_min_eur), session_fee_eur=float(row.session_fee_eur),
        currency=row.currency)


def seed(db: Db, roster_path: str = "") -> None:
    """Insert the default tariff and, if a roster is given, its cards (only into empty tables)."""
    if db.scalar(select(func.count()).select_from(TariffRow)) == 0:
        db.add(TariffRow(tariff_id=DEFAULT_TARIFF, price_per_kwh_eur=Decimal("0.35"),
                         price_per_min_eur=Decimal(0), session_fee_eur=Decimal(0), currency="EUR"))
        db.flush()
    if roster_path and db.scalar(select(func.count()).select_from(CardRow)) == 0:
        stamp = now()
        for c in load_roster(Path(roster_path)).cards:
            db.add(CardRow(card=c.card, holder=c.holder, home_city=c.home_city, tariff_id=c.tariff,
                           status="blocked" if c.blocked else "active", created_at=stamp))
            if c.blocked:
                db.add(BlockedRow(card=c.card, reason="seeded as blocked", source="operator",
                                  blocked_at=stamp))
    db.commit()
