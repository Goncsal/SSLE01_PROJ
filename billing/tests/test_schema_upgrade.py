from evbilling.db import init_db
from sqlalchemy import create_engine, inspect, text


def test_missing_column_is_added(tmp_path):
    url = f"sqlite:///{tmp_path}/b.db"
    with create_engine(url).begin() as conn:
        conn.execute(text(
            "CREATE TABLE transactions (transaction_id INTEGER PRIMARY KEY, card TEXT NOT NULL, "
            "hier_id TEXT NOT NULL, city TEXT NOT NULL, connector_id INTEGER NOT NULL, "
            "tariff_id TEXT NOT NULL, started_at DATETIME NOT NULL, stopped_at DATETIME, "
            "meter_start_wh BIGINT NOT NULL, meter_stop_wh BIGINT, energy_wh BIGINT, "
            "cost_eur NUMERIC(10,2), status TEXT NOT NULL)"))       # no flag_reason, stop_reason
    init_db(url)
    assert {"transactions.flag_reason", "transactions.stop_reason"} <= set(init_db.added)
    cols = {c["name"] for c in inspect(create_engine(url)).get_columns("transactions")}
    assert {"flag_reason", "stop_reason"} <= cols
