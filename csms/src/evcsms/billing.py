"""Billing client. The CSMS talks to billing through one small interface.

`LocalBilling` is the in-process stub of section 4.3.1 (used when BILLING_URL is empty).
`HttpBilling` calls the billing service of section 4.3 (Phase 2). Either way a failed
authorisation raises `BillingError` and the caller decides (see CSMS_BILLING_FAIL_OPEN).
"""

from __future__ import annotations

import re
from typing import Protocol

import httpx
from evcommon.models import CARD_REGEX, AuthorizeRequest, AuthorizeResponse, SessionStart, SessionStop

REQUEST_TIMEOUT_S = 2.0
_CARD_RE = re.compile(CARD_REGEX)


class BillingError(Exception):
    """Billing did not answer."""


class Billing(Protocol):
    async def authorize(self, card: str, hier_id: str) -> str:
        """OCPP authorisation status: Accepted, Blocked, Expired or Invalid."""

    async def session_start(self, body: SessionStart) -> None: ...

    async def session_stop(self, transaction_id: int, body: SessionStop) -> None: ...

    async def aclose(self) -> None: ...


class LocalBilling:
    """Every well-formed card is accepted unless it is on the blocked list."""

    def __init__(self, blocked_cards=()):
        self.blocked = set(blocked_cards)

    async def authorize(self, card: str, hier_id: str) -> str:
        if not _CARD_RE.match(card):
            return "Invalid"
        return "Blocked" if card in self.blocked else "Accepted"

    async def session_start(self, body: SessionStart) -> None:
        return None

    async def session_stop(self, transaction_id: int, body: SessionStop) -> None:
        return None

    async def aclose(self) -> None:
        return None


class HttpBilling:
    """The billing service over HTTP with a bearer token."""

    def __init__(self, base_url: str, token: str, telemetry):
        self.telemetry = telemetry
        self._http = httpx.AsyncClient(
            base_url=base_url.rstrip("/"), timeout=REQUEST_TIMEOUT_S,
            headers={"Authorization": f"Bearer {token}"})

    async def _post(self, path: str, body) -> dict:
        try:
            with self.telemetry.timed("billing"):
                resp = await self._http.post(path, json=body.model_dump(mode="json"))
            resp.raise_for_status()
            return resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            self.telemetry.dependency_error("billing", type(exc).__name__)
            raise BillingError(str(exc)) from exc

    async def authorize(self, card: str, hier_id: str) -> str:
        if not _CARD_RE.match(card):
            return "Invalid"
        data = await self._post("/authorize", AuthorizeRequest(card=card, hier_id=hier_id))
        return AuthorizeResponse.model_validate(data).status

    async def session_start(self, body: SessionStart) -> None:
        await self._post("/sessions", body)

    async def session_stop(self, transaction_id: int, body: SessionStop) -> None:
        await self._post(f"/sessions/{transaction_id}/stop", body)

    async def aclose(self) -> None:
        await self._http.aclose()
