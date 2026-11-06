import pytest

sqlalchemy = pytest.importorskip("sqlalchemy")
from sqlalchemy import Column, Integer, MetaData, Table, Text, create_engine, inspect, text  # noqa: E402

from evcommon.dbschema import SchemaError, add_missing_columns  # noqa: E402


def _v1(md):
    return Table("sessions", md, Column("transaction_id", Integer, primary_key=True),
                 Column("card", Text, nullable=False))


def test_adds_nullable_and_defaulted_columns(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path}/t.db")
    v1 = MetaData()
    _v1(v1)
    v1.create_all(engine)
    with engine.begin() as conn:
        conn.execute(text("INSERT INTO sessions (transaction_id, card) VALUES (1, 'AA')"))

    v2 = MetaData()
    Table("sessions", v2, Column("transaction_id", Integer, primary_key=True),
          Column("card", Text, nullable=False),
          Column("last_meter_at", Text),
          Column("state", Text, nullable=False, server_default=text("'open'")))
    Table("brand_new", v2, Column("id", Integer, primary_key=True))
    v2.create_all(engine)                       # creates brand_new, leaves sessions alone
    assert add_missing_columns(engine, v2) == ["sessions.last_meter_at", "sessions.state"]
    cols = {c["name"] for c in inspect(engine).get_columns("sessions")}
    assert {"last_meter_at", "state"} <= cols
    with engine.begin() as conn:
        assert conn.execute(text("SELECT state FROM sessions")).scalar() == "open"
    assert add_missing_columns(engine, v2) == []    # idempotent


def test_refuses_not_null_without_default(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path}/t.db")
    v1 = MetaData()
    _v1(v1)
    v1.create_all(engine)
    v2 = MetaData()
    Table("sessions", v2, Column("transaction_id", Integer, primary_key=True),
          Column("card", Text, nullable=False), Column("hier_id", Text, nullable=False))
    with pytest.raises(SchemaError):
        add_missing_columns(engine, v2)
