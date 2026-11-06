"""Charger identity: the handshake checks of section 5.2.

`IdentityChecker.decide` is the decision logic (checks 1 to 9): it takes the request path,
headers and socket peer and returns an `Identity` or a `Refusal`. `process_request` wraps
it for the WebSocket server, adds check 10 (duplicate id) and does the logging and the
metrics. A refused charger never gets an OCPP session.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import ipaddress
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from http import HTTPStatus

from evcommon import hierid
from evcommon.models import Node

from .db import utcnow
from .registry_client import RegistryError
from .telemetry import city_of

EDGE_HEADERS = ("X-Real-IP", "X-Client-Cert-DN", "X-Client-Cert-Serial",
                "X-Client-Cert-Verify", "X-Edge-Id")
PROXY_REFRESH_S = 300
UPGRADE_GRACE_S = 10      # a claimed connection must be served within this time


@dataclass
class Identity:
    """Who a connection is, as decided by the handshake checks."""
    hier_id: str
    city: str
    station: str
    srcip: str
    peer: str
    edge: str | None = None
    tls: bool = False
    cert_cn: str | None = None
    cert_serial: str | None = None
    node: Node | None = None
    claimed_at: dt.datetime | None = None    # `since` of its row in `connections`


@dataclass
class Refusal:
    """A refused handshake: HTTP status, log event and the fields of the log line."""
    status: int
    event: str                       # ident.* event
    reason: str                      # label of the identity_rejections metric
    fields: dict = field(default_factory=dict)


class TrustedProxies:
    """The edge addresses from CSMS_TRUSTED_PROXIES (IPs, CIDRs or host names).

    Host names are resolved on first use and again every 5 minutes.
    """

    def __init__(self, entries):
        self.entries = list(entries)
        self._networks: list = []
        self._resolved_at = -1e9

    async def contains(self, ip: str) -> bool:
        if time.monotonic() - self._resolved_at > PROXY_REFRESH_S:
            await self._resolve()
        addr = normalise_ip(ip)
        return addr is not None and any(addr in net for net in self._networks)

    async def _resolve(self) -> None:
        networks = []
        for entry in self.entries:
            try:
                networks.append(ipaddress.ip_network(entry, strict=False))
                continue
            except ValueError:
                pass
            try:
                infos = await asyncio.get_running_loop().getaddrinfo(entry, None)
            except OSError:
                continue                     # unresolvable now: this entry is just not trusted
            for info in infos:
                addr = normalise_ip(info[4][0])
                if addr is not None:
                    networks.append(ipaddress.ip_network(addr))
        self._networks = networks
        self._resolved_at = time.monotonic()


def normalise_serial(value: str | None) -> str:
    """Certificate serial for comparison: upper case, no colons, no leading zeros.

    nginx and openssl may print the same serial as `00A1B2` or `A1:B2`.
    """
    return (value or "").replace(":", "").strip().upper().lstrip("0")


def normalise_ip(value: str | None):
    """An ip_address object (IPv4-mapped IPv6 turned into IPv4), or None."""
    try:
        addr = ipaddress.ip_address((value or "").strip())
    except ValueError:
        return None
    return addr.ipv4_mapped or addr if addr.version == 6 else addr


class IdentityChecker:
    def __init__(self, ctx, bus):
        self.ctx = ctx
        self.bus = bus
        self.settings = ctx.settings
        self.proxies = TrustedProxies(ctx.settings.trusted_proxies)
        self._release_timers: set[asyncio.Task] = set()

    def close(self) -> None:
        for timer in list(self._release_timers):
            timer.cancel()

    # ---- checks 1 to 9: a pure decision --------------------------------------------

    async def decide(self, path: str, headers: Mapping[str, str], peer: str) -> Identity | Refusal:
        s = self.settings
        # 1. Is the peer one of our edges? Only then are the edge headers believed.
        via_edge = await self.proxies.contains(peer)
        if via_edge:
            real_ip = normalise_ip(headers.get("X-Real-IP"))
            srcip = str(real_ip) if real_ip else peer
        else:
            srcip = peer
            for name in EDGE_HEADERS:
                if name in headers:
                    self.ctx.log.event("ident.spoofed_header", "edge header from a non-edge peer",
                                       level="warning", srcip=srcip, reason=name)
            if not s.allow_direct:
                return Refusal(HTTPStatus.FORBIDDEN, "ident.untrusted_peer", "untrusted_peer",
                               {"srcip": srcip, "peer": peer})

        # 2. The path names a charger.
        try:
            hid = hierid.path_id(path)
        except ValueError:
            return Refusal(HTTPStatus.NOT_FOUND, "ident.bad_id", "bad_id",
                           {"srcip": srcip, "path": path[:200]})
        who = {"hier_id": str(hid), "srcip": srcip}

        # 3. The registry knows it.
        try:
            node = await self.ctx.registry.get(str(hid))
        except RegistryError:
            if not s.registry_fail_open:
                return Refusal(HTTPStatus.SERVICE_UNAVAILABLE, "dependency.error", "", who)
            node = None                      # fail open: no registry facts to check against
        else:
            if node is None:
                return Refusal(HTTPStatus.NOT_FOUND, "ident.unknown", "unknown", who)
            # 4. It is not quarantined (own state or an ancestor's).
            if node.effective_state != "active":
                return Refusal(HTTPStatus.FORBIDDEN, "ident.quarantined", "quarantined", who)

        ident = Identity(str(hid), hid.city, hid.station, srcip, peer, node=node)
        if not via_edge:
            return await self._check_station_ip(ident, who)         # local dev: checks 5-8 skipped

        # 5. The edge belongs to the charger's city.
        ident.edge = headers.get("X-Edge-Id") or None
        if edge_city(ident.edge) != hid.city:
            return Refusal(HTTPStatus.FORBIDDEN, "ident.city_mismatch", "city_mismatch",
                           {**who, "edge": ident.edge})
        # 6. A verified client certificate, except in plain cities.
        verify = (headers.get("X-Client-Cert-Verify") or "NONE").strip()
        presented = verify == "SUCCESS"
        if not presented and (hid.city not in s.plain_cities or verify not in ("NONE", "")):
            return Refusal(HTTPStatus.FORBIDDEN, "ident.no_cert", "no_cert",
                           {**who, "edge": ident.edge})
        if presented:
            ident.tls = True
            ident.cert_cn = hierid.cn_from_dn(headers.get("X-Client-Cert-DN"))
            ident.cert_serial = normalise_serial(headers.get("X-Client-Cert-Serial")) or None
            # 7. The certificate belongs to this very id.
            if ident.cert_cn != str(hid):
                return Refusal(HTTPStatus.FORBIDDEN, "ident.cert_mismatch", "cert_mismatch",
                               {**who, "cert_cn": ident.cert_cn})
            # 8. It is the certificate the registry has on record.
            if (node and node.cert_serial
                    and normalise_serial(node.cert_serial) != normalise_serial(ident.cert_serial)):
                return Refusal(HTTPStatus.FORBIDDEN, "ident.cert_serial_mismatch",
                               "cert_serial_mismatch",
                               {**who, "cert_serial": ident.cert_serial, "expected": node.cert_serial})
        return await self._check_station_ip(ident, who)

    async def _check_station_ip(self, ident: Identity, who: dict) -> Identity | Refusal:
        """Check 9: the charger connects from the address its station has in the registry."""
        if ident.node is None:
            return ident
        try:
            station = await self.ctx.registry.get(str(hierid.parse(ident.hier_id).parent))
        except RegistryError:
            return ident                     # the charger lookup worked a moment ago; skip
        if station and station.ip and station.ip != ident.srcip:
            if self.settings.enforce_station_ip:
                return Refusal(HTTPStatus.FORBIDDEN, "ident.ip_mismatch", "ip_mismatch",
                               {**who, "expected": station.ip})
            self.ctx.log.event("ident.ip_mismatch", "charger connected from an unexpected address",
                               level="warning", expected=station.ip, **who)
        return ident

    # ---- the WebSocket hook: all checks, logging, metrics -------------------------------

    async def process_request(self, connection, request):
        """`process_request` of the WebSocket server: None to accept, a response to refuse."""
        peer = str((connection.remote_address or ("",))[0])
        # the path is checked as given; a query string is not part of the id
        path = request.path.split("?", 1)[0]
        result = await self.decide(path, request.headers, peer)
        if isinstance(result, Identity):
            refusal = await self._claim_connection(result)
            if refusal is None:
                connection.ident = result
                return None
            result = refusal
        return self._refuse(connection, result, path)

    def _refuse(self, connection, refusal: Refusal, path: str):
        hier_id = refusal.fields.get("hier_id")
        city = city_of(hier_id) if hier_id else _city_of_path(path)
        self.ctx.metrics.connections.labels(city=city, result="rejected").inc()
        if refusal.reason:
            self.ctx.metrics.rejections.labels(city=city, reason=refusal.reason).inc()
        if refusal.event == "dependency.error":
            text = "registry unavailable\n"       # already logged by the registry client
        else:
            text = "refused\n"
            self.ctx.log.event(refusal.event, f"handshake refused: {refusal.reason}",
                               level="warning", http_status=int(refusal.status), **refusal.fields)
        return connection.respond(refusal.status, text)

    # ---- check 10: duplicate id ---------------------------------------------------------

    async def _claim_connection(self, ident: Identity) -> Refusal | None:
        """Register the id in `connections`. The primary key refuses a second connection.

        When the id is taken, probe its owner: alive refuses the newcomer (409), dead (or
        its replica gone) is replaced and the insert is tried again.
        """
        db = self.ctx.db
        ident.claimed_at = utcnow()
        owner = None
        for _ in range(3):
            if await db.connection_claim(ident, ident.claimed_at):
                self._release_if_not_served(ident)
                return None
            owner = await db.connection_owner(ident.hier_id)
            if owner is None:
                continue                     # the old row went away between the two queries
            if await self._owner_is_alive(owner):
                break
            await self._replace(ident, owner)
        self.ctx.metrics.duplicates.labels(city=ident.city, outcome="rejected").inc()
        return Refusal(HTTPStatus.CONFLICT, "charger.duplicate_id", "duplicate_id",
                       {"hier_id": ident.hier_id, "srcip": ident.srcip,
                        "other_srcip": owner["src_ip"] if owner else None, "edge": ident.edge})

    async def _owner_is_alive(self, owner: dict) -> bool:
        hier_id = owner["hier_id"]
        if owner["pod"] == self.settings.instance:
            old = self.ctx.hub.get(hier_id)
            if old is None:
                return True                  # claimed a moment ago, the upgrade is still running
            return await self.ctx.hub.is_alive(old, self.settings.probe_timeout_s)
        if not owner["pod_alive"]:
            return False                     # its replica stopped heartbeating: the row is stale
        return await self.bus.probe(hier_id)

    async def _replace(self, ident: Identity, owner: dict) -> None:
        self.ctx.metrics.duplicates.labels(city=ident.city, outcome="replaced").inc()
        self.ctx.log.event("conn.replaced", "dead connection replaced by a new one",
                           hier_id=ident.hier_id, srcip=ident.srcip, other_srcip=owner["src_ip"])
        old = self.ctx.hub.get(ident.hier_id) if owner["pod"] == self.settings.instance else None
        if old is not None:
            self.ctx.hub.remove(old)
            old.close_reason = "replaced"
            self.ctx.spawn(old.close_socket(1001, "replaced"))
        await self.ctx.db.connection_delete(owner)

    def _release_if_not_served(self, ident: Identity) -> None:
        """Free the claimed row if the WebSocket upgrade never completes."""
        timer = asyncio.create_task(self._release_later(ident))
        self._release_timers.add(timer)
        timer.add_done_callback(self._release_timers.discard)

    async def _release_later(self, ident: Identity) -> None:
        await asyncio.sleep(UPGRADE_GRACE_S)
        served = self.ctx.hub.get(ident.hier_id)
        if served is None or served.ident is not ident:
            await self.ctx.db.connection_close(ident.hier_id, ident.claimed_at)


def edge_city(edge_id: str | None) -> str | None:
    """City of an edge hier-id such as pt.porto.edge; None if it is not one."""
    try:
        hid = hierid.parse(edge_id or "")
    except ValueError:
        return None
    return hid.city if hid.kind == "edge" else None


def _city_of_path(path: str) -> str:
    try:
        return hierid.path_id(path).city or "unknown"
    except ValueError:
        return "unknown"
