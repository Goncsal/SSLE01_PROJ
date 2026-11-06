"""Client for the registry: `GET /nodes/{id}` with a small TTL cache (section 4.1)."""

from __future__ import annotations

import time

import httpx
from evcommon.models import Node

REQUEST_TIMEOUT_S = 2.0
MAX_CACHE_ENTRIES = 10_000


class RegistryError(Exception):
    """The registry could not answer (down, slow or broken)."""


class RegistryClient:
    """Looks chargers up in the registry. Unknown ids are cached too (scanners)."""

    def __init__(self, base_url: str, cache_s: float, telemetry):
        self.cache_s = cache_s
        self.telemetry = telemetry
        self._http = httpx.AsyncClient(base_url=base_url.rstrip("/"), timeout=REQUEST_TIMEOUT_S)
        self._cache: dict[str, tuple[float, Node | None]] = {}

    async def get(self, hier_id: str) -> Node | None:
        """The registry record, or None if the id is unknown. Raises RegistryError."""
        hit = self._cache.get(hier_id)
        if hit and hit[0] > time.monotonic():
            return hit[1]
        try:
            with self.telemetry.timed("registry"):
                resp = await self._http.get(f"/nodes/{hier_id}")
            if resp.status_code == 404:
                node = None
            elif resp.status_code == 200:
                node = Node.model_validate(resp.json())
            else:
                raise RegistryError(f"registry answered HTTP {resp.status_code}")
        except (httpx.HTTPError, ValueError) as exc:    # pydantic's ValidationError is a ValueError
            self.telemetry.dependency_error("registry", type(exc).__name__)
            raise RegistryError(str(exc)) from exc
        except RegistryError as exc:
            self.telemetry.dependency_error("registry", str(exc))
            raise
        if len(self._cache) >= MAX_CACHE_ENTRIES:
            self._cache.clear()
        self._cache[hier_id] = (time.monotonic() + self.cache_s, node)
        return node

    def drop(self, hier_id: str) -> None:
        """Forget one entry, so the next handshake asks the registry again."""
        self._cache.pop(hier_id, None)

    async def aclose(self) -> None:
        await self._http.aclose()
