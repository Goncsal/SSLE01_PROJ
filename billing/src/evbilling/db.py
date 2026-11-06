"""Database layer: tables of docs/interfaces.md section 8.4 (SQLAlchemy 2, sync)."""

from __future__ import annotations

import datetime as dt
import warnings
from decimal import Decimal

from evcommon.dbschema import add_missing_columns
from sqlalchemy import (
    BigInteger, DateTime, ForeignKey, Integer, Numeric, String, Text, create_engine, event,
)
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker
from sqlalchemy.types import TypeDecorator

# SQLite stores NUMERIC as a float and warns about it; values are rounded to cents anyway.
warnings.filterwarnings("ignore", message="Dialect sqlite.*does \\*not\\* support Decimal")


class UtcDateTime(TypeDecorator):
    """Timestamp stored in UTC and always returned timezone-aware (SQLite drops the zone)."""

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            value = value.replace(tzinfo=dt.UTC)
        return value.astimezone(dt.UTC)

    def process_result_value(self, value, dialect):
        if value is not None and value.tzinfo is None:
            value = value.replace(tzinfo=dt.UTC)
        return value


class Base(DeclarativeBase):
    pass


class TariffRow(Base):
    __tablename__ = "tariffs"

    tariff_id: Mapped[str] = mapped_column(String, primary_key=True)
    price_per_kwh_eur: Mapped[Decimal] = mapped_column(Numeric(8, 4), nullable=False)
    price_per_min_eur: Mapped[Decimal] = mapped_column(Numeric(8, 4), nullable=False, default=0)
    session_fee_eur: Mapped[Decimal] = mapped_column(Numeric(8, 2), nullable=False, default=0)
    currency: Mapped[str] = mapped_column(String, nullable=False, default="EUR")


class CardRow(Base):
    __tablename__ = "cards"

    card: Mapped[str] = mapped_column(String, primary_key=True)
    holder: Mapped[str] = mapped_column(String, nullable=False)
    home_city: Mapped[str] = mapped_column(String, nullable=False)
    tariff_id: Mapped[str] = mapped_column(String, ForeignKey("tariffs.tariff_id"), nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False, default="active")  # active|blocked|expired
    expiry_date: Mapped[dt.datetime | None] = mapped_column(UtcDateTime)
    created_at: Mapped[dt.datetime] = mapped_column(UtcDateTime, nullable=False)


class BlockedRow(Base):
    """History of blocks; the current state is cards.status."""

    __tablename__ = "blocked"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    card: Mapped[str] = mapped_column(String, index=True, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[str] = mapped_column(String, nullable=False)
    incident_id: Mapped[str | None] = mapped_column(String)
    blocked_at: Mapped[dt.datetime] = mapped_column(UtcDateTime, nullable=False)
    unblocked_at: Mapped[dt.datetime | None] = mapped_column(UtcDateTime)
    unblocked_by: Mapped[str | None] = mapped_column(String)


class TransactionRow(Base):
    __tablename__ = "transactions"

    # the id comes from the CSMS, so it is not generated here
    transaction_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    card: Mapped[str] = mapped_column(String, index=True, nullable=False)
    hier_id: Mapped[str] = mapped_column(String, index=True, nullable=False)
    city: Mapped[str] = mapped_column(String, nullable=False)
    connector_id: Mapped[int] = mapped_column(Integer, nullable=False)
    tariff_id: Mapped[str] = mapped_column(String, nullable=False)
    started_at: Mapped[dt.datetime] = mapped_column(UtcDateTime, nullable=False)
    stopped_at: Mapped[dt.datetime | None] = mapped_column(UtcDateTime)
    meter_start_wh: Mapped[int] = mapped_column(BigInteger, nullable=False)
    meter_stop_wh: Mapped[int | None] = mapped_column(BigInteger)
    energy_wh: Mapped[int | None] = mapped_column(BigInteger)
    cost_eur: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    status: Mapped[str] = mapped_column(String, nullable=False, default="open")  # open|closed|flagged
    flag_reason: Mapped[str | None] = mapped_column(Text)
    stop_reason: Mapped[str | None] = mapped_column(String)


def make_engine(url: str) -> Engine:
    engine = create_engine(url, pool_pre_ping=True)
    if engine.dialect.name == "sqlite":
        @event.listens_for(engine, "connect")
        def _busy_timeout(dbapi_conn, _record):
            dbapi_conn.execute("PRAGMA busy_timeout = 5000")
    return engine


def init_db(url: str) -> sessionmaker:
    """Create the engine and the tables, add missing columns; return a session factory.

    The added "table.column" names are kept in `init_db.added` for the `service.start` line.
    """
    engine = make_engine(url)
    Base.metadata.create_all(engine)
    init_db.added = add_missing_columns(engine, Base.metadata)
    return sessionmaker(engine, expire_on_commit=False)


init_db.added = []
