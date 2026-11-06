"""The roster: the static description of cities, stations, chargers and cards.

One YAML file (dev/roster.yaml) feeds the simulator, the registry seeding and
the billing seeding. Format: docs/interfaces.md, section 9.1.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from . import hierid
from .models import CardId


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RosterCharger(_Strict):
    charger: str                                   # cp-1
    max_kw: float = Field(gt=0, le=400)
    connectors: int = Field(default=1, ge=1, le=4)
    vendor: str = "EVLab"
    model: str = "EVL-AC22"
    serial: str | None = None

    @field_validator("charger")
    @classmethod
    def _cp(cls, v: str) -> str:
        if not hierid.CHARGER_RE.match(v):
            raise ValueError("charger must look like cp-1")
        return v


class RosterStation(_Strict):
    station: str                                   # st-001
    name: str
    lat: float = Field(ge=-90, le=90)
    lon: float = Field(ge=-180, le=180)
    dev_metrics_port: int | None = Field(default=None, ge=1024, le=65535)
    chargers: list[RosterCharger] = Field(min_length=1)

    @field_validator("station")
    @classmethod
    def _st(cls, v: str) -> str:
        if not hierid.STATION_RE.match(v):
            raise ValueError("station must look like st-001")
        return v


class RosterCity(_Strict):
    city: str
    name: str | None = None
    plain_ws: bool = False                         # legacy site without TLS
    edge_lat: float | None = None
    edge_lon: float | None = None
    stations: list[RosterStation] = Field(min_length=1)

    @field_validator("city")
    @classmethod
    def _city(cls, v: str) -> str:
        hierid.city(v)
        return v


class RosterCard(_Strict):
    card: CardId
    holder: str
    home_city: str
    tariff: str = "standard"
    blocked: bool = False


@dataclass(frozen=True, slots=True)
class ChargerEntry:
    hier_id: str
    city: RosterCity
    station: RosterStation
    charger: RosterCharger


class Roster(_Strict):
    version: int = Field(default=1, ge=1, le=1)
    country: str = "pt"
    cities: list[RosterCity] = Field(min_length=1)
    cards: list[RosterCard] = Field(default_factory=list)

    @model_validator(mode="after")
    def _unique(self) -> Roster:
        seen: set[str] = set()
        ports: set[int] = set()
        for c in self.cities:
            for s in c.stations:
                sid = hierid.station(c.city, s.station)
                if sid in seen:
                    raise ValueError(f"duplicate station {sid}")
                seen.add(sid)
                if s.dev_metrics_port is not None:
                    if s.dev_metrics_port in ports:
                        raise ValueError(f"duplicate dev_metrics_port {s.dev_metrics_port}")
                    ports.add(s.dev_metrics_port)
                cps = [ch.charger for ch in s.chargers]
                if len(cps) != len(set(cps)):
                    raise ValueError(f"duplicate charger in {sid}")
        cards = [c.card for c in self.cards]
        if len(cards) != len(set(cards)):
            raise ValueError("duplicate card id")
        city_names = {c.city for c in self.cities}
        for card in self.cards:
            if card.home_city not in city_names:
                raise ValueError(f"card {card.card}: unknown home_city {card.home_city}")
        return self

    # ---- lookups ------------------------------------------------------------
    def iter_stations(self) -> Iterator[tuple[str, RosterCity, RosterStation]]:
        for c in self.cities:
            for s in c.stations:
                yield hierid.station(c.city, s.station), c, s

    def iter_chargers(self) -> Iterator[ChargerEntry]:
        for c in self.cities:
            for s in c.stations:
                for ch in s.chargers:
                    yield ChargerEntry(hierid.charger(c.city, s.station, ch.charger), c, s, ch)

    def station(self, station_hier_id: str) -> tuple[RosterCity, RosterStation]:
        hid = hierid.parse(station_hier_id)
        if hid.kind != "station":
            raise ValueError(f"not a station id: {station_hier_id}")
        for sid, c, s in self.iter_stations():
            if sid == station_hier_id:
                return c, s
        raise KeyError(station_hier_id)

    def city(self, name: str) -> RosterCity:
        for c in self.cities:
            if c.city == name:
                return c
        raise KeyError(name)

    def cards_for_city(self, city: str) -> list[RosterCard]:
        return [c for c in self.cards if c.home_city == city]


def load_roster(path: str | Path) -> Roster:
    with open(path, encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    return Roster.model_validate(data)


def dump_roster(roster: Roster, path: str | Path) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        yaml.safe_dump(roster.model_dump(mode="json", exclude_none=True), fh,
                       sort_keys=False, allow_unicode=True)
