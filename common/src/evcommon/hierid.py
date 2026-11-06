"""Hierarchical identifiers (hier-ids) for every element of the network.

Grammar (docs/interfaces.md, section 1):

    city      pt.<city>                    pt.porto
    station   pt.<city>.<station>          pt.porto.st-003
    charger   pt.<city>.<station>.<cp>     pt.porto.st-003.cp-2
    edge      pt.<city>.edge               pt.porto.edge
    central   pt.central.<node>            pt.central.k3s-master

`pt` and `pt.central` are valid prefixes (for /locate and delegation checks)
but not element ids.

The same string is used as certificate CN, OCPP charge-point id, registry key,
Prometheus `hier_id` label, Wazuh agent name and log field.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

COUNTRY = "pt"
DNS_ZONE = "pt.lab"          # cities live in <city>.pt.lab
CENTRAL_DNS_ZONE = "incus"   # central instances resolve as <name>.incus
RESERVED_CITY_NAMES = frozenset({"central", "edge", "lab"})

Kind = Literal["city", "station", "charger", "edge", "central"]

_LABEL = r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*"
CITY_RE = re.compile(rf"^{_LABEL}$")
STATION_RE = re.compile(r"^st-(?!000)[0-9]{3}$")
CHARGER_RE = re.compile(r"^cp-[1-9][0-9]?$")
NODE_RE = re.compile(rf"^{_LABEL}$")
MAX_CITY_LEN = 24
MAX_NODE_LEN = 40

# One regex for the whole id, for use outside Python (Wazuh, docs, nginx maps).
HIER_ID_REGEX = (
    r"^pt\.(?:central\.[a-z][a-z0-9]*(?:-[a-z0-9]+)*"
    r"|[a-z][a-z0-9]*(?:-[a-z0-9]+)*(?:\.edge|\.st-[0-9]{3}(?:\.cp-[1-9][0-9]?)?)?)$"
)
HIER_ID_PATTERN = re.compile(HIER_ID_REGEX)


@dataclass(frozen=True, slots=True)
class HierId:
    kind: Kind
    city: str | None = None
    station: str | None = None
    charger: str | None = None
    node: str | None = None

    # ---- formatting -------------------------------------------------
    def __str__(self) -> str:
        match self.kind:
            case "city":
                return f"{COUNTRY}.{self.city}"
            case "station":
                return f"{COUNTRY}.{self.city}.{self.station}"
            case "charger":
                return f"{COUNTRY}.{self.city}.{self.station}.{self.charger}"
            case "edge":
                return f"{COUNTRY}.{self.city}.edge"
            case "central":
                return f"{COUNTRY}.central.{self.node}"
        raise AssertionError(self.kind)

    @property
    def cn(self) -> str:
        """Certificate common name: the hier-id itself."""
        return str(self)

    @property
    def wazuh_agent_name(self) -> str:
        """Wazuh agent name: the hier-id itself (allowed charset [a-z0-9.-])."""
        return str(self)

    @property
    def parent(self) -> HierId | None:
        match self.kind:
            case "charger":
                return HierId("station", city=self.city, station=self.station)
            case "station" | "edge":
                return HierId("city", city=self.city)
        return None

    @property
    def station_id(self) -> HierId | None:
        """The station that physically hosts this element (charger or station)."""
        if self.kind == "charger":
            return self.parent
        if self.kind == "station":
            return self
        return None

    @property
    def edge_id(self) -> HierId | None:
        """The edge gateway that serves this element's city."""
        if self.city is None:
            return None
        return HierId("edge", city=self.city)

    def ancestry(self) -> list[str]:
        """Ids from the city down to this element, e.g. [pt.porto, pt.porto.st-003, pt.porto.st-003.cp-2]."""
        chain: list[str] = []
        cur: HierId | None = self
        while cur is not None:
            chain.append(str(cur))
            cur = cur.parent
        return list(reversed(chain))

    def labels(self) -> dict[str, str]:
        """Prometheus / log labels: hier_id plus the components that apply."""
        out = {"hier_id": str(self)}
        if self.city:
            out["city"] = self.city
        if self.station:
            out["station"] = self.station
        if self.charger:
            out["charger"] = self.charger
        return out

    # ---- naming in other systems -------------------------------------
    def dns_name(self) -> str:
        """DNS name. Chargers have no address of their own: their name is used
        only as a certificate SAN and resolves nowhere."""
        match self.kind:
            case "city":
                return f"{self.city}.{DNS_ZONE}"
            case "station":
                return f"{self.station}.{self.city}.{DNS_ZONE}"
            case "charger":
                return f"{self.charger}.{self.station}.{self.city}.{DNS_ZONE}"
            case "edge":
                return f"edge.{self.city}.{DNS_ZONE}"
            case "central":
                return f"{self.node}.{CENTRAL_DNS_ZONE}"
        raise AssertionError(self.kind)

    def incus_instance(self) -> str | None:
        """Incus instance hosting the element (instance names are global, so
        city elements are prefixed with the city)."""
        match self.kind:
            case "station":
                return f"{self.city}-{self.station}"
            case "charger":
                return f"{self.city}-{self.station}"
            case "edge":
                return f"{self.city}-edge"
            case "central":
                return self.node
        return None

    def incus_network(self) -> str | None:
        return f"city-{self.city}" if self.city else None


# ---- parsing ---------------------------------------------------------------

def _check_city(city: str) -> None:
    if not CITY_RE.match(city) or len(city) > MAX_CITY_LEN or city in RESERVED_CITY_NAMES:
        raise ValueError(f"invalid city name: {city!r}")


def parse(value: str) -> HierId:
    """Parse an element hier-id. Raises ValueError if it is not one."""
    if not isinstance(value, str):
        raise ValueError("hier-id must be a string")
    parts = value.split(".")
    if len(parts) < 2 or parts[0] != COUNTRY:
        raise ValueError(f"invalid hier-id: {value!r}")
    if parts[1] == "central":
        if len(parts) != 3 or not NODE_RE.match(parts[2]) or len(parts[2]) > MAX_NODE_LEN:
            raise ValueError(f"invalid central hier-id: {value!r}")
        return HierId("central", node=parts[2])
    city = parts[1]
    _check_city(city)
    if len(parts) == 2:
        return HierId("city", city=city)
    if len(parts) == 3:
        if parts[2] == "edge":
            return HierId("edge", city=city)
        if STATION_RE.match(parts[2]):
            return HierId("station", city=city, station=parts[2])
        raise ValueError(f"invalid station in hier-id: {value!r}")
    if len(parts) == 4 and STATION_RE.match(parts[2]) and CHARGER_RE.match(parts[3]):
        return HierId("charger", city=city, station=parts[2], charger=parts[3])
    raise ValueError(f"invalid hier-id: {value!r}")


def is_valid(value: str, kind: Kind | None = None) -> bool:
    try:
        hid = parse(value)
    except ValueError:
        return False
    return kind is None or hid.kind == kind


def is_prefix(value: str) -> bool:
    """True for any element id plus the pure prefixes `pt` and `pt.central`."""
    return value in (COUNTRY, f"{COUNTRY}.central") or is_valid(value)


def is_under(hier_id: str, prefix: str) -> bool:
    """True if hier_id equals prefix or is a descendant of it (dot boundary)."""
    return hier_id == prefix or hier_id.startswith(prefix + ".")


def charger(city: str, station: str, cp: str) -> str:
    return str(parse(f"{COUNTRY}.{city}.{station}.{cp}"))


def station(city: str, st: str) -> str:
    return str(parse(f"{COUNTRY}.{city}.{st}"))


def edge(city: str) -> str:
    return str(parse(f"{COUNTRY}.{city}.edge"))


def city(name: str) -> str:
    return str(parse(f"{COUNTRY}.{name}"))


def central(node: str) -> str:
    return str(parse(f"{COUNTRY}.central.{node}"))


def from_dns(name: str) -> HierId:
    """Inverse of HierId.dns_name()."""
    name = name.rstrip(".").lower()
    if name.endswith("." + CENTRAL_DNS_ZONE):
        return parse(f"{COUNTRY}.central.{name[: -len(CENTRAL_DNS_ZONE) - 1]}")
    if not name.endswith("." + DNS_ZONE):
        raise ValueError(f"not a lab DNS name: {name!r}")
    labels = name[: -len(DNS_ZONE) - 1].split(".")
    if len(labels) == 1:
        return parse(f"{COUNTRY}.{labels[0]}")
    if len(labels) == 2 and labels[0] == "edge":
        return parse(f"{COUNTRY}.{labels[1]}.edge")
    if len(labels) == 2:
        return parse(f"{COUNTRY}.{labels[1]}.{labels[0]}")
    if len(labels) == 3:
        return parse(f"{COUNTRY}.{labels[2]}.{labels[1]}.{labels[0]}")
    raise ValueError(f"not a lab DNS name: {name!r}")


def path_id(path: str, prefix: str = "/ocpp/") -> HierId:
    """Charge-point id from an OCPP WebSocket path such as /ocpp/pt.porto.st-003.cp-2."""
    if not path.startswith(prefix):
        raise ValueError(f"OCPP path must start with {prefix}: {path!r}")
    rest = path[len(prefix):].split("?", 1)[0].rstrip("/")
    hid = parse(rest)
    if hid.kind != "charger":
        raise ValueError(f"OCPP id must be a charger hier-id: {rest!r}")
    return hid


def _split_dn(dn: str) -> list[str]:
    """Split an RFC 2253 DN on unescaped commas."""
    out, cur, esc = [], [], False
    for ch in dn:
        if esc:
            cur.append(ch)
            esc = False
        elif ch == "\\":
            cur.append(ch)
            esc = True
        elif ch == ",":
            out.append("".join(cur).strip())
            cur = []
        else:
            cur.append(ch)
    out.append("".join(cur).strip())
    return [p for p in out if p]


def cn_from_dn(dn: str | None) -> str | None:
    """Extract the CN from a subject DN as forwarded by the edge.

    Accepts the RFC 2253 form nginx uses for $ssl_client_s_dn
    ("CN=pt.porto.st-003.cp-2,OU=porto,O=EVCharge Lab") and the legacy
    slash form ("/O=EVCharge Lab/OU=porto/CN=pt.porto.st-003.cp-2").
    """
    if not dn:
        return None
    dn = dn.strip()
    parts = [p for p in dn.split("/") if p] if dn.startswith("/") else _split_dn(dn)
    for part in parts:
        key, sep, val = part.partition("=")
        if sep and key.strip().upper() == "CN":
            return val.strip().replace("\\", "")
    return None
