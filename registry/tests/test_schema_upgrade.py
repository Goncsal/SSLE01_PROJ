from evregistry.db import init_db
from sqlalchemy import create_engine, inspect, text


def test_missing_column_is_added(tmp_path):
    url = f"sqlite:///{tmp_path}/r.db"
    with create_engine(url).begin() as conn:
        conn.execute(text("CREATE TABLE nodes (hier_id TEXT PRIMARY KEY, kind TEXT NOT NULL, "
                          "state TEXT NOT NULL DEFAULT 'active', created_at DATETIME NOT NULL, "
                          "updated_at DATETIME NOT NULL, updated_by TEXT NOT NULL)"))
    init_db(url)
    assert "nodes.vendor" in init_db.added
    cols = {c["name"] for c in inspect(create_engine(url)).get_columns("nodes")}
    assert {"vendor", "cert_serial", "state_reason"} <= cols
    init_db(url)
    assert init_db.added == []
