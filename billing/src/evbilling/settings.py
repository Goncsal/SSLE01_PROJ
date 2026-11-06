"""Settings read from the environment (docs/interfaces.md, section 3.2)."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from evcommon import config


@dataclass(frozen=True)
class Settings:
    api_token: str
    database_url: str = "sqlite:///./data/billing.db"
    responder_token: str = ""        # optional, isolate-only (section 4.3); empty = off
    seed_roster: str = ""            # roster path; empty = no card seeding
    bind_host: str = "127.0.0.1"
    port: int = 8002

    def __post_init__(self) -> None:
        if len(self.api_token) < config.MIN_TOKEN_LEN:
            raise config.ConfigError(f"BILLING_API_TOKEN must be at least {config.MIN_TOKEN_LEN} characters")
        if self.responder_token:
            if len(self.responder_token) < config.MIN_TOKEN_LEN:
                raise config.ConfigError(
                    f"BILLING_RESPONDER_TOKEN must be at least {config.MIN_TOKEN_LEN} characters")
            if self.responder_token == self.api_token:
                raise config.ConfigError("BILLING_RESPONDER_TOKEN must differ from BILLING_API_TOKEN")

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        return cls(
            api_token=config.require_token("BILLING_API_TOKEN", env),
            responder_token=config.get_str("BILLING_RESPONDER_TOKEN", "", env).strip(),
            database_url=config.database_url("sqlite:///./data/billing.db", env),
            seed_roster=config.get_str("BILLING_SEED_ROSTER", "", env),
            bind_host=config.bind_host(env),
            port=config.get_int("BILLING_LISTEN_PORT", 8002, env),
        )
