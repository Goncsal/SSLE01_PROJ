"""Queries and conversions that do not depend on HTTP."""

from __future__ import annotations

import datetime as dt

from evcommon import hierid
from evcommon.models import LocateMatch, Node
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from .db import NodeRow

LOCATE_LIMIT = 500


def now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def quarantined_ids(db: Session) -> set[str]:
    return set(db.scalars(select(NodeRow.hier_id).where(NodeRow.state == "quarantined")))


def effective_state(row: NodeRow, quarantined: set[str]) -> str:
    """Own state if not active; else quarantined when any ancestor is quarantined."""
    if row.state != "active":
        return row.state
    ancestors = hierid.parse(row.hier_id).ancestry()[:-1]
    return "quarantined" if any(a in quarantined for a in ancestors) else "active"


def to_node(row: NodeRow, quarantined: set[str]) -> Node:
    return Node.model_validate(row).model_copy(
        update={"effective_state": effective_state(row, quarantined)})


def under(prefix: str):
    """SQL condition: hier_id equals prefix or is below it (dot boundary)."""
    return or_(NodeRow.hier_id == prefix, NodeRow.hier_id.startswith(prefix + "."))


def subtree(db: Session, prefix: str, kind: str | None = None, limit: int = LOCATE_LIMIT + 1) -> list[NodeRow]:
    stmt = select(NodeRow).where(under(prefix)).order_by(NodeRow.hier_id).limit(limit)
    if kind:
        stmt = stmt.where(NodeRow.kind == kind)
    return list(db.scalars(stmt))


def locate_matches(db: Session, rows: list[NodeRow]) -> list[LocateMatch]:
    """Build LocateMatch objects; chargers take their location from the station."""
    quarantined = quarantined_ids(db)
    stations = {r.hier_id: r for r in rows if r.kind == "station"}
    missing = {r.parent for r in rows if r.kind == "charger" and r.parent not in stations}
    if missing:
        stations.update({s.hier_id: s for s in db.scalars(select(NodeRow).where(NodeRow.hier_id.in_(missing)))})

    out = []
    for row in rows:
        hid = hierid.parse(row.hier_id)
        site = stations.get(row.hier_id if row.kind == "station" else row.parent) \
            if row.kind in ("station", "charger") else row
        edge = hid.edge_id
        out.append(LocateMatch(
            hier_id=row.hier_id, kind=row.kind, state=row.state,
            effective_state=effective_state(row, quarantined),
            path=hid.ancestry(),
            operator=hid.city or "noc",
            edge=str(edge) if edge else None,
            station=str(hid.station_id) if hid.station_id else None,
            lat=site.lat if site else None, lon=site.lon if site else None,
            host=site.host if site else None, ip=site.ip if site else None,
            network=site.network if site else None, dns=site.dns if site else None,
        ))
    return out
