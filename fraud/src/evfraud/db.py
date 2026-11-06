"""Database layer: card_events, station_stats and fraud_alerts (section 4.4; SQLAlchemy 2, sync)."""

from __future__ import annotations

import datetime as dt

from evcommon.dbschema import add_missing_columns
from sqlalchemy import BigInteger, DateTime, Float, Integer, String, Text, create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker
from sqlalchemy.types import JSON, TypeDecorator


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


class CardEventRow(Base):
    """One session start or stop of a card, with the station's position."""

    __tablename__ = "card_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[dt.datetime] = mapped_column(UtcDateTime, index=True, nullable=False)
    kind: Mapped[str] = mapped_column(String, nullable=False)          # start|stop
    card: Mapped[str] = mapped_column(String, index=True, nullable=False)
    city: Mapped[str] = mapped_column(String, nullable=False)
    hier_id: Mapped[str] = mapped_column(String, nullable=False)
    lat: Mapped[float | None] = mapped_column(Float)
    lon: Mapped[float | None] = mapped_column(Float)
    energy_wh: Mapped[int | None] = mapped_column(BigInteger)
    transaction_id: Mapped[int | None] = mapped_column(BigInteger)


class StationStatRow(Base):
    """Rolling per-station samples: energy the CSMS saw (a stopped session) or energy the
    feeder meter measured over one polling interval."""

    __tablename__ = "station_stats"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[dt.datetime] = mapped_column(UtcDateTime, index=True, nullable=False)
    city: Mapped[str] = mapped_column(String, index=True, nullable=False)
    station_id: Mapped[str] = mapped_column(String, index=True, nullable=False)   # pt.<city>.<st>
    reported_wh: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    grid_wh: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    sessions: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    grid_power_w: Mapped[float | None] = mapped_column(Float)


class AlertRow(Base):
    __tablename__ = "fraud_alerts"

    alert_id: Mapped[str] = mapped_column(String, primary_key=True)
    ts: Mapped[dt.datetime] = mapped_column(UtcDateTime, index=True, nullable=False)
    rule: Mapped[str] = mapped_column(String, nullable=False)
    severity: Mapped[str] = mapped_column(String, nullable=False)
    card: Mapped[str | None] = mapped_column(String, index=True)
    hier_id: Mapped[str | None] = mapped_column(String, index=True)
    other_hier_id: Mapped[str | None] = mapped_column(String)
    city: Mapped[str | None] = mapped_column(String)
    score: Mapped[float] = mapped_column(Float, nullable=False)
    detail: Mapped[str] = mapped_column(Text, nullable=False)
    evidence: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    dedup_key: Mapped[str] = mapped_column(String, index=True, nullable=False)


def make_engine(url: str) -> Engine:
    kwargs = {"pool_pre_ping": True}
    if url.startswith("postgresql"):
        kwargs.update(pool_size=5, max_overflow=5)
    engine = create_engine(url, **kwargs)
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
