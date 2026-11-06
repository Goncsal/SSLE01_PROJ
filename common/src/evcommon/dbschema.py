"""Additive schema upgrades at startup (docs/interfaces.md, section 8.1).

The services create their tables with `metadata.create_all()`, which never changes a
table that already exists. `add_missing_columns()` closes the common gap: a column added
to the code is added to the database too, as long as it is nullable (or has a server
default). Anything else (renames, type changes, new constraints) is not automatic and
needs a manual ALTER or a fresh database.

    metadata.create_all(engine)
    added = add_missing_columns(engine, metadata)   # e.g. ["sessions.last_meter_at"]
"""

from __future__ import annotations


class SchemaError(RuntimeError):
    """A missing column cannot be added safely (NOT NULL without a server default)."""


def add_missing_columns(engine, metadata) -> list[str]:
    """Add the nullable columns that exist in `metadata` but not in the database.

    Returns the "table.column" names that were added. Tables missing entirely are left to
    `create_all()`. Raises SchemaError for a missing NOT NULL column without a server
    default, so the service stops with a clear message instead of failing later.
    """
    from sqlalchemy import inspect, text

    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())
    preparer = engine.dialect.identifier_preparer
    added: list[str] = []
    with engine.begin() as conn:
        for table in metadata.sorted_tables:
            if table.name not in existing_tables:
                continue
            present = {c["name"] for c in inspector.get_columns(table.name)}
            for column in table.columns:
                if column.name in present:
                    continue
                if not column.nullable and column.server_default is None:
                    raise SchemaError(
                        f"column {table.name}.{column.name} is NOT NULL without a server "
                        "default: add it by hand or start from a fresh database")
                col_type = column.type.compile(dialect=engine.dialect)
                ddl = (f"ALTER TABLE {preparer.quote(table.name)} "
                       f"ADD COLUMN {preparer.quote(column.name)} {col_type}")
                if column.server_default is not None:
                    default = column.server_default.arg
                    default_sql = default.text if hasattr(default, "text") else str(default)
                    ddl += f" DEFAULT {default_sql}"
                    if not column.nullable:
                        ddl += " NOT NULL"
                conn.execute(text(ddl))
                added.append(f"{table.name}.{column.name}")
    return added
