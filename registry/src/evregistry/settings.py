"""Settings read from the environment (docs/interfaces.md, section 3.2)."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from evcommon import config, hierid

TOKEN_PREFIX = "REGISTRY_TOKEN_"


@dataclass(frozen=True)
class Settings:
    admin_token: str
    city_tokens: dict[str, str] = field(default_factory=dict)   # city -> token
    responder_token: str = ""      # optional, isolate-only (section 4.1); empty = off
    database_url: str = "sqlite:///./data/registry.db"
    bind_host: str = "127.0.0.1"
    port: int = 8001

    def __post_init__(self) -> None:
        tokens = [self.admin_token, *self.city_tokens.values()]
        if self.responder_token:
            tokens.append(self.responder_token)
        if any(len(t) < config.MIN_TOKEN_LEN for t in tokens):
            raise config.ConfigError(f"every token must be at least {config.MIN_TOKEN_LEN} characters")
        if len(set(tokens)) != len(tokens):
            raise config.ConfigError("registry tokens must all be different")
        for city in self.city_tokens:
            hierid.city(city)   # ValueError if not a valid city name

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        city_tokens: dict[str, str] = {}
        for suffix, token in config.prefixed(TOKEN_PREFIX, env).items():
            city = suffix.lower().replace("_", "-")
            try:
                hierid.city(city)
            except ValueError as e:
                raise config.ConfigError(f"{TOKEN_PREFIX}{suffix}: {e}") from e
            city_tokens[city] = token
        return cls(
            admin_token=config.require_token("REGISTRY_ADMIN_TOKEN", env),
            city_tokens=city_tokens,
            responder_token=(config.require_token("REGISTRY_RESPONDER_TOKEN", env)
                             if config.get_str("REGISTRY_RESPONDER_TOKEN", "", env).strip() else ""),
            database_url=config.database_url("sqlite:///./data/registry.db", env),
            bind_host=config.bind_host(env),
            port=config.get_int("REGISTRY_LISTEN_PORT", 8001, env),
        )
