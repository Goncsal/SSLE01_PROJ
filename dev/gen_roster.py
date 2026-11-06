"""Write the roster (dev/roster.yaml): cities, stations, chargers and cards.

Deterministic: the same flags always give the same bytes (fixed seed, no
wall-clock input). The output is validated by evcommon.roster before it is
written. Format: docs/interfaces.md, section 9.1.

    python dev/gen_roster.py                       # the lab network, 3 cities
    python dev/gen_roster.py --stations-per-city 50 --chargers-per-station 10 \\
        --out /tmp/roster-big.yaml                 # scalability runs
"""

from __future__ import annotations

import argparse
import random
import sys
from pathlib import Path

from evcommon import hierid
from evcommon.roster import (
    Roster,
    RosterCard,
    RosterCharger,
    RosterCity,
    RosterStation,
    dump_roster,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUT = REPO_ROOT / "dev" / "roster.yaml"
METRICS_PORT_BASE = 9301          # local dev: one metrics port per station, in roster order

# City, display name, edge coordinates (= the first station), and the named stations.
CITIES: list[dict] = [
    {
        "city": "porto", "name": "Porto", "plain_ws": False,
        "stations": [
            ("Avenida dos Aliados", 41.1496, -8.6109),
            ("Boavista", 41.1579, -8.6291),
            ("Campanhã", 41.1486, -8.5856),
            ("Foz do Douro", 41.1520, -8.6763),
            ("Asprela", 41.1780, -8.6020),
        ],
    },
    {
        "city": "lisboa", "name": "Lisboa", "plain_ws": False,
        "stations": [
            ("Baixa", 38.7110, -9.1366),
            ("Parque das Nações", 38.7680, -9.0940),
            ("Alvalade", 38.7530, -9.1430),
            ("Belém", 38.6970, -9.2060),
            ("Benfica", 38.7510, -9.2010),
        ],
    },
    {
        "city": "aveiro", "name": "Aveiro", "plain_ws": True,
        "stations": [
            ("Fórum Aveiro", 40.6405, -8.6538),
            ("Universidade", 40.6303, -8.6574),
            ("Glicínias", 40.6280, -8.6440),
            ("Esgueira", 40.6500, -8.6290),
            ("Praia da Barra", 40.6430, -8.7470),
        ],
    },
]

NAMED_STATIONS = 5
OFFSET_DEG = 0.02                 # extra stations: up to ~2 km around the city's first point
CARD_HEX_LEN = 14
CARD_HOLDER_FMT = "driver-{:04d}"


def _charger_model(cp_index: int, station_index: int, stations_per_city: int) -> tuple[str, float]:
    """cp-1 is AC 22 kW; even chargers are DC 50 kW. One station per city (the
    last one) has a 150 kW DC charger as its cp-2."""
    if cp_index % 2 == 1:
        return "EVL-AC22", 22.0
    if station_index == stations_per_city - 1 and cp_index == 2:
        return "EVL-DC150", 150.0
    return "EVL-DC50", 50.0


def _serial_prefix(max_kw: float) -> str:
    return f"EVL{int(max_kw)}"


def build_roster(stations_per_city: int, chargers_per_station: int, cards: int, seed: int) -> Roster:
    serial_no = 0
    port = METRICS_PORT_BASE
    city_models: list[RosterCity] = []
    for spec in CITIES:
        rng = random.Random(f"{seed}:{spec['city']}")
        first_lat, first_lon = spec["stations"][0][1], spec["stations"][0][2]
        stations: list[RosterStation] = []
        for si in range(stations_per_city):
            if si < NAMED_STATIONS and si < len(spec["stations"]):
                label, lat, lon = spec["stations"][si]
                name = f"{spec['name']} - {label}"
            else:
                number = si + 1
                lat = round(first_lat + rng.uniform(-OFFSET_DEG, OFFSET_DEG), 6)
                lon = round(first_lon + rng.uniform(-OFFSET_DEG, OFFSET_DEG), 6)
                name = f"{spec['name']} - Station {number}"
            chargers: list[RosterCharger] = []
            for cpi in range(1, chargers_per_station + 1):
                model, max_kw = _charger_model(cpi, si, stations_per_city)
                serial_no += 1
                chargers.append(RosterCharger(
                    charger=f"cp-{cpi}",
                    max_kw=max_kw,
                    connectors=1,
                    vendor="EVLab",
                    model=model,
                    serial=f"{_serial_prefix(max_kw)}-{serial_no:04d}",
                ))
            stations.append(RosterStation(
                station=f"st-{si + 1:03d}",
                name=name,
                lat=lat,
                lon=lon,
                dev_metrics_port=port,
                chargers=chargers,
            ))
            port += 1
        city_models.append(RosterCity(
            city=spec["city"],
            name=spec["name"],
            plain_ws=spec["plain_ws"],
            edge_lat=first_lat,
            edge_lon=first_lon,
            stations=stations,
        ))

    cities = [c.city for c in city_models]
    rng = random.Random(f"{seed}:cards")
    card_ids: list[str] = []
    while len(card_ids) < cards:
        cid = f"{rng.getrandbits(4 * CARD_HEX_LEN):0{CARD_HEX_LEN}X}"
        if cid not in card_ids:
            card_ids.append(cid)
    blocked_idx = set(rng.sample(range(cards), 2))

    card_models: list[RosterCard] = []
    for i in range(cards):
        card_models.append(RosterCard(
            card=card_ids[i],
            holder=CARD_HOLDER_FMT.format(i + 1),
            home_city=cities[i % len(cities)],
            tariff="standard",
            blocked=i in blocked_idx,
        ))
    return Roster(version=1, country=hierid.COUNTRY, cities=city_models, cards=card_models)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--stations-per-city", type=int, default=5)
    parser.add_argument("--chargers-per-station", type=int, default=2)
    parser.add_argument("--cards", type=int, default=60)
    parser.add_argument("--seed", type=int, default=2026)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = parser.parse_args(argv)

    if not 1 <= args.stations_per_city <= 999:
        parser.error("--stations-per-city must be 1..999")
    if not 1 <= args.chargers_per_station <= 99:
        parser.error("--chargers-per-station must be 1..99")
    if args.cards < 2:
        parser.error("--cards must be at least 2 (two cards are blocked)")

    roster = build_roster(args.stations_per_city, args.chargers_per_station, args.cards, args.seed)
    out = args.out if args.out.is_absolute() else Path.cwd() / args.out
    out.parent.mkdir(parents=True, exist_ok=True)
    dump_roster(roster, out)
    print(f"wrote {out}: {len(roster.cities)} cities, "
          f"{sum(len(c.stations) for c in roster.cities)} stations, "
          f"{sum(len(s.chargers) for c in roster.cities for s in c.stations)} chargers, "
          f"{len(roster.cards)} cards")
    return 0


if __name__ == "__main__":
    sys.exit(main())
