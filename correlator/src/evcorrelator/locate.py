"""Root cause: walk the registry topology from the signals' elements.

Algorithm (docs/interfaces.md, section 4.5):

1. Every `hier_id` of the incident's signals is looked up with `GET /locate/{hier_id}`.
   The answer carries `path`, the ids from the city down to the element, e.g.
   `[pt.porto, pt.porto.st-003, pt.porto.st-003.cp-2]`.
2. Every `srcip` is looked up with `GET /locate/ip/{ip}`. Only a station, edge or central
   element owning that address counts (the chargers of a matching station are the station's
   residents, not the address's owner). An address nobody owns, such as an attacker's, adds no
   element: it is reported in the explanation instead.
3. The root cause is the lowest common element of all the paths, i.e. their longest common
   prefix. One charger -> that charger; two chargers of a station -> the station; stations of
   a city -> the city; elements of different cities -> `pt`.
4. A fraud incident that carries a card and does not end at one single charger names the
   card: the actor is the card, the chargers are where it was seen.

The registry is untrusted input: ids and paths are validated against the hier-id grammar,
at most MAX_MATCHES matches are read, unknown fields are ignored. If the registry cannot be
reached the root cause is derived from the ids themselves (`verified=False`): the incident
gets a root cause but `t_located` stays unset.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

import httpx
from evcommon import hierid

from .models import RootCause, Signal

MAX_MATCHES = 500
MAX_BODY = 1_000_000
MAX_ELEMENTS = 50          # per walk
CACHE_TTL_S = 30.0
ERROR_TTL_S = 3.0


class RegistryError(Exception):
    """The registry did not answer usefully."""


@dataclass(frozen=True)
class Element:
    hier_id: str
    kind: str
    path: tuple[str, ...]
    city: str | None = None
    edge: str | None = None
    station: str | None = None
    host: str | None = None
    ip: str | None = None
    verified: bool = True      # False: derived from the id, the registry was not consulted


def _valid_id(value) -> bool:
    return isinstance(value, str) and len(value) <= 100 and hierid.is_prefix(value)


def _opt_str(value) -> str | None:
    return value[:100] if isinstance(value, str) else None


def _element_from_match(match) -> Element | None:
    if not isinstance(match, dict) or not _valid_id(match.get("hier_id")):
        return None
    path = match.get("path")
    if not isinstance(path, list) or not path or len(path) > 6 or not all(_valid_id(p) for p in path):
        path = [match["hier_id"]]
    parts = match["hier_id"].split(".")
    return Element(
        hier_id=match["hier_id"], kind=str(match.get("kind", ""))[:20], path=tuple(path),
        city=parts[1] if len(parts) > 1 else None,
        edge=match["edge"] if _valid_id(match.get("edge")) else None,
        station=match["station"] if _valid_id(match.get("station")) else None,
        host=_opt_str(match.get("host")), ip=_opt_str(match.get("ip")))


def element_from_id(hier_id: str) -> Element | None:
    """Degraded lookup: the path is implied by the id (city > station > charger)."""
    try:
        parsed = hierid.parse(hier_id)
    except ValueError:
        return None
    return Element(hier_id=hier_id, kind=parsed.kind, path=tuple(parsed.ancestry()), city=parsed.city,
                   edge=str(parsed.edge_id) if parsed.edge_id and parsed.kind != "edge" else None,
                   station=str(parsed.station_id) if parsed.station_id else None, verified=False)


class Locator:
    """Registry client with a small TTL cache (a burst of signals re-walks the same ids)."""

    def __init__(self, base_url: str, client: httpx.Client | None = None, timeout: float = 2.0):
        self._client = client or httpx.Client(base_url=base_url, timeout=timeout)
        self._cache: dict[str, tuple[float, Element | None | RegistryError]] = {}
        self._lock = threading.Lock()

    def close(self) -> None:
        self._client.close()

    def _get(self, path: str) -> list:
        try:
            resp = self._client.get(path)
            if resp.status_code != 200 or len(resp.content) > MAX_BODY:
                raise RegistryError(f"registry answered {resp.status_code} for {path}")
            matches = resp.json().get("matches", [])
        except (httpx.HTTPError, ValueError, AttributeError) as exc:
            raise RegistryError(f"registry unreachable or malformed answer: {type(exc).__name__}") from exc
        return matches[:MAX_MATCHES] if isinstance(matches, list) else []

    def _cached(self, key: str, fetch):
        now = time.monotonic()
        with self._lock:
            hit = self._cache.get(key)
        if hit and hit[0] > now:
            if isinstance(hit[1], RegistryError):
                raise hit[1]
            return hit[1]
        try:
            value = fetch()
        except RegistryError as exc:
            with self._lock:
                self._cache[key] = (now + ERROR_TTL_S, exc)
            raise
        with self._lock:
            if len(self._cache) > 2000:
                self._cache.clear()
            self._cache[key] = (now + CACHE_TTL_S, value)
        return value

    def element(self, hier_id: str) -> Element | None:
        """The element itself, None if the registry does not know it."""
        def fetch():
            for match in self._get(f"/locate/{hier_id}"):
                if isinstance(match, dict) and match.get("hier_id") == hier_id:
                    return _element_from_match(match)
            return None
        return self._cached("id:" + hier_id, fetch)

    def by_ip(self, ip: str) -> Element | None:
        """The station, edge or central element that owns the address, if any."""
        def fetch():
            for match in self._get(f"/locate/ip/{ip}"):
                if isinstance(match, dict) and match.get("kind") in ("station", "edge", "central"):
                    return _element_from_match(match)
            return None
        return self._cached("ip:" + ip, fetch)


# ---- the walk -----------------------------------------------------------------------

@dataclass
class Walk:
    elements: list[Element] = field(default_factory=list)
    from_ip: set[str] = field(default_factory=set)       # ids that were reached only through an address
    unknown_ips: list[str] = field(default_factory=list)
    errors: int = 0


def _unique(items):
    return list(dict.fromkeys(items))


def walk(signals: list[Signal], locator: Locator) -> Walk:
    result = Walk()
    seen: set[str] = set()
    ids = _unique(h for s in signals if not s.response for h in s.hier_ids)[:MAX_ELEMENTS]
    ips = _unique(s.srcip for s in signals if s.srcip and not s.response)[:MAX_ELEMENTS]
    if not ids and not ips:                       # only response signals: nothing new to say
        ids = _unique(h for s in signals for h in s.hier_ids)[:MAX_ELEMENTS]
    for hier_id in ids:
        try:
            element = locator.element(hier_id)
        except RegistryError:
            result.errors += 1
            element = element_from_id(hier_id)
        if element and element.hier_id not in seen:
            seen.add(element.hier_id)
            result.elements.append(element)
    for ip in ips:
        try:
            element = locator.by_ip(ip)
        except RegistryError:
            result.errors += 1
            continue
        if element is None:
            result.unknown_ips.append(ip)
        elif element.hier_id not in seen:
            seen.add(element.hier_id)
            result.elements.append(element)
            result.from_ip.add(element.hier_id)
    return result


def common_prefix(paths: list[tuple[str, ...]]) -> tuple[str, ...]:
    if not paths:
        return ()
    prefix = paths[0]
    for path in paths[1:]:
        n = 0
        while n < min(len(prefix), len(path)) and prefix[n] == path[n]:
            n += 1
        prefix = prefix[:n]
    return prefix


def _kind_of(hier_id: str) -> str:
    try:
        return hierid.parse(hier_id).kind
    except ValueError:
        return "country" if hier_id == hierid.COUNTRY else "platform"


def root_cause(signals: list[Signal], result: Walk) -> tuple[RootCause | None, bool]:
    """(root cause, located). `located` is True when the registry resolved an element."""
    if not result.elements:
        return None, False
    located = any(e.verified for e in result.elements)
    paths = [e.path for e in result.elements]
    prefix = common_prefix(paths)
    if prefix:
        lce = prefix[-1]
    elif all(p[0].startswith(f"{hierid.COUNTRY}.central") for p in paths):
        lce = f"{hierid.COUNTRY}.central"
    else:
        lce = hierid.COUNTRY
    lce_kind = _kind_of(lce)
    parts = lce.split(".")
    city = parts[1] if len(parts) > 1 and parts[1] != "central" else ("central" if "central" in parts else None)

    below = [e for e in result.elements if e.hier_id == lce or lce in e.path]
    exact = next((e for e in below if e.hier_id == lce), None)
    host = exact.host if exact else next((e.host for e in below if e.station == lce and e.host), None)
    ip = exact.ip if exact else next((e.ip for e in below if e.station == lce and e.ip), None)
    edges = {e.edge for e in below if e.edge}
    edge = next(iter(edges)) if len(edges) == 1 else None

    cards = _unique(s.card for s in signals if s.card)
    fraud_card = (cards and any(s.kind == "fraud" for s in signals)
                  and (len(result.elements) > 1 and lce_kind != "charger"))
    n_signals = sum(1 for s in signals if not s.response)

    if fraud_card:
        key_type, key = "card", cards[0]
        places = ", ".join(e.hier_id for e in result.elements[:5])
        explanation = (f"card {key} appears at {len(result.elements)} chargers that no single "
                       f"element contains ({places}); the card is the common cause")
    else:
        only_ip = lce in result.from_ip and len(result.elements) == 1
        key_type, key = ("srcip", ip or lce) if only_ip else ("hier_id", lce)
        if len(result.elements) == 1:
            explanation = f"all {n_signals} signal(s) concern {lce} ({lce_kind})"
        else:
            explanation = (f"{n_signals} signal(s) concern {len(result.elements)} elements; their "
                           f"lowest common element is {lce} ({lce_kind})")
        if only_ip:
            explanation += " reached through its address"
    if host:
        explanation += f", host {host}"
    if result.unknown_ips:
        explanation += "; source address(es) not in the registry: " + ", ".join(result.unknown_ips[:3])
    if result.errors:
        explanation += "; registry unavailable for some look-ups, path derived from the ids"
    return RootCause(key_type=key_type, key=key, hier_id=None if fraud_card else lce, host=host, ip=ip,
                     city=city, edge=edge, explanation=explanation), located
