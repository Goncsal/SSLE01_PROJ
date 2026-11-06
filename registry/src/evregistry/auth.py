"""Bearer-token authentication and delegation (docs/interfaces.md, sections 1.3 and 4)."""

from __future__ import annotations

import hmac
from dataclasses import dataclass

from evcommon import hierid

from .settings import Settings


RESPONDER = "responder"


@dataclass(frozen=True)
class Principal:
    operator: str        # "admin", "responder" or a city name
    scope: str           # "pt" or "pt.<city>"

    def may_isolate(self, hier_id: str) -> bool:
        """Quarantine (never release): the responder may isolate any element."""
        return self.operator == RESPONDER or self.may_write(hier_id)

    def may_write(self, hier_id: str) -> bool:
        """Admin: anything. City X: only ids strictly below pt.X."""
        if self.operator == "admin":
            return True
        if self.operator == RESPONDER:
            return False
        return hier_id != self.scope and hierid.is_under(hier_id, self.scope)


def identify(settings: Settings, header: str | None) -> Principal | None:
    """Match an Authorization header against every token in constant time."""
    scheme, _, token = (header or "").partition(" ")
    if scheme.lower() != "bearer" or not token:
        return None
    given = token.strip().encode()
    found: Principal | None = None
    # no early exit: the time does not depend on which token matched
    if hmac.compare_digest(given, settings.admin_token.encode()):
        found = Principal("admin", hierid.COUNTRY)
    for city, secret in settings.city_tokens.items():
        if hmac.compare_digest(given, secret.encode()):
            found = Principal(city, hierid.city(city))
    if settings.responder_token and hmac.compare_digest(given, settings.responder_token.encode()):
        found = Principal(RESPONDER, hierid.COUNTRY)
    return found
