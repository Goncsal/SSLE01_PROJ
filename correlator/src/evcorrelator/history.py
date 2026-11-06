"""Optional closed-incident history (DATABASE_URL). Losing it loses no live state."""

from __future__ import annotations

import datetime as dt

from .models import Incident


class History:
    """One table, `correlator_incidents`: the id, a few columns to filter on, and the JSON body."""

    def __init__(self, url: str):
        import sqlalchemy as sa       # optional dependency (extra `history` or `pg`)

        self._sa = sa
        self._engine = sa.create_engine(url, future=True)
        meta = sa.MetaData()
        self._table = sa.Table(
            "correlator_incidents", meta,
            sa.Column("incident_id", sa.String(40), primary_key=True),
            sa.Column("kind", sa.String(16), nullable=False),
            sa.Column("t_first_signal", sa.String(40), nullable=False, index=True),
            sa.Column("body", sa.Text, nullable=False))
        meta.create_all(self._engine)

    def save(self, incident: Incident) -> None:
        sa, t = self._sa, self._table
        row = dict(incident_id=incident.incident_id, kind=incident.kind,
                   t_first_signal=incident.t_first_signal.isoformat(), body=incident.model_dump_json())
        with self._engine.begin() as conn:
            conn.execute(sa.delete(t).where(t.c.incident_id == incident.incident_id))
            conn.execute(sa.insert(t).values(**row))

    def get(self, incident_id: str) -> Incident | None:
        sa, t = self._sa, self._table
        with self._engine.connect() as conn:
            body = conn.execute(sa.select(t.c.body).where(t.c.incident_id == incident_id)).scalar()
        return Incident.model_validate_json(body) if body else None

    def list(self, since: dt.datetime | None, limit: int) -> list[Incident]:
        sa, t = self._sa, self._table
        stmt = sa.select(t.c.body).order_by(t.c.t_first_signal.desc()).limit(limit)
        if since:
            stmt = stmt.where(t.c.t_first_signal >= since.isoformat())
        with self._engine.connect() as conn:
            return [Incident.model_validate_json(b) for (b,) in conn.execute(stmt)]
