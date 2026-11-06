"""An old database without a column is upgraded at startup (section 8.1)."""

import asyncio

from helpers import running
from sqlalchemy import create_engine, inspect, text


def test_missing_column_is_added_at_start(tmp_path):
    url = f"sqlite:///{tmp_path}/csms.db"
    engine = create_engine(url)
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE sessions (transaction_id INTEGER PRIMARY KEY, "
                          "hier_id TEXT NOT NULL, connector_id INTEGER NOT NULL, card TEXT NOT NULL, "
                          "auth_status TEXT NOT NULL, meter_start_wh BIGINT NOT NULL, "
                          "last_meter_wh BIGINT, meter_stop_wh BIGINT, peak_power_w FLOAT, "
                          "started_at DATETIME NOT NULL, stopped_at DATETIME, stop_reason TEXT, "
                          "state TEXT NOT NULL)"))       # no last_meter_at

    async def go():
        async with running(tmp_path, DATABASE_URL=url) as srv:
            started = srv.events("service.start")[0]
            assert "sessions.last_meter_at" in started["msg"]
    asyncio.run(go())
    assert "last_meter_at" in {c["name"] for c in inspect(engine).get_columns("sessions")}
