"""Database layer: tables of docs/interfaces.md section 8.2 (SQLAlchemy 2, sync)."""

from __future__ import annotations

import datetime as dt

from evcommon.dbschema import add_missing_columns
from sqlalchemy import DateTime, Float, ForeignKey, Integer, String, Text, create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker
from sqlalchemy.types import TypeDecorator


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


class NodeRow(Base):
    __tablename__ = "nodes"

    hier_id: Mapped[str] = mapped_column(String, primary_key=True)
    kind: Mapped[str] = mapped_column(String, nullable=False)
    parent: Mapped[str | None] = mapped_column(String, ForeignKey("nodes.hier_id"), index=True)
    city: Mapped[str | None] = mapped_column(String, index=True)
    station: Mapped[str | None] = mapped_column(String)
    charger: Mapped[str | None] = mapped_column(String)
    name: Mapped[str | None] = mapped_column(String)
    lat: Mapped[float | None] = mapped_column(Float)
    lon: Mapped[float | None] = mapped_column(Float)
    max_kw: Mapped[float | None] = mapped_column(Float)
    connectors: Mapped[int | None] = mapped_column(Integer)
    vendor: Mapped[str | None] = mapped_column(String)
    model: Mapped[str | None] = mapped_column(String)
    serial: Mapped[str | None] = mapped_column(String)
    cert_serial: Mapped[str | None] = mapped_column(String)
    host: Mapped[str | None] = mapped_column(String)
    ip: Mapped[str | None] = mapped_column(String, index=True)
    network: Mapped[str | None] = mapped_column(String)
    dns: Mapped[str | None] = mapped_column(String)
    metrics_target: Mapped[str | None] = mapped_column(String)
    state: Mapped[str] = mapped_column(String, nullable=False, default="active", server_default="active")
    state_reason: Mapped[str | None] = mapped_column(String)
    state_source: Mapped[str | None] = mapped_column(String)
    state_changed_at: Mapped[dt.datetime | None] = mapped_column(UtcDateTime)
    created_at: Mapped[dt.datetime] = mapped_column(UtcDateTime, nullable=False)
    updated_at: Mapped[dt.datetime] = mapped_column(UtcDateTime, nullable=False)
    updated_by: Mapped[str] = mapped_column(String, nullable=False)


class QuarantineLogRow(Base):
    __tablename__ = "quarantine_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    hier_id: Mapped[str] = mapped_column(String, nullable=False)
    action: Mapped[str] = mapped_column(String, nullable=False)   # quarantine | release
    reason: Mapped[str | None] = mapped_column(Text)
    source: Mapped[str | None] = mapped_column(String)
    operator: Mapped[str | None] = mapped_column(String)
    incident_id: Mapped[str | None] = mapped_column(String)
    at: Mapped[dt.datetime] = mapped_column(UtcDateTime, nullable=False)


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
