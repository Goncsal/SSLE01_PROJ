"""Settings read from the environment (docs/interfaces.md, section 3.2)."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from evcommon import config


@dataclass(frozen=True)
class Settings:
    api_token: str
    registry_url: str
    csms_api_url: str = ""         # reserved: read-only connection look-ups (not used by the walk yet)
    csms_api_token: str = ""
    window_s: float = 120.0        # join window
    skew_s: float = 3.0            # extra clock-skew tolerance
    close_s: float = 600.0         # auto-close after this quiet time
    database_url: str = ""         # empty = no closed-incident history
    bind_host: str = "127.0.0.1"
    port: int = 8004

    def __post_init__(self) -> None:
        if len(self.api_token) < config.MIN_TOKEN_LEN:
            raise config.ConfigError(f"CORRELATOR_API_TOKEN must be at least {config.MIN_TOKEN_LEN} characters")
        if min(self.window_s, self.close_s) <= 0 or self.skew_s < 0:
            raise config.ConfigError("CORRELATOR_WINDOW_S and CORRELATOR_CLOSE_S must be positive, "
                                     "CORRELATOR_SKEW_S must not be negative")

    @property
    def join_tolerance_s(self) -> float:
        """Two signals are joinable when their timestamps differ by at most this."""
        return self.window_s + self.skew_s

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        # The history is only kept when DATABASE_URL is set explicitly, so a bare run
        # does not create ./data/correlator.db.
        url = config.database_url("", env) if config.get_str("DATABASE_URL", "", env).strip() else ""
        return cls(
            api_token=config.require_token("CORRELATOR_API_TOKEN", env),
            registry_url=config.require("REGISTRY_URL", env).rstrip("/"),
            csms_api_url=config.get_str("CSMS_API_URL", "", env).rstrip("/"),
            csms_api_token=config.get_str("CSMS_API_TOKEN", "", env),
            window_s=config.get_float("CORRELATOR_WINDOW_S", 120.0, env),
            skew_s=config.get_float("CORRELATOR_SKEW_S", 3.0, env),
            close_s=config.get_float("CORRELATOR_CLOSE_S", 600.0, env),
            database_url=url,
            bind_host=config.bind_host(env),
            port=config.get_int("CORRELATOR_LISTEN_PORT", 8004, env),
        )
