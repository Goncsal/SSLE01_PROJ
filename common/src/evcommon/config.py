"""Small helpers to read configuration from environment variables.

Every service reads its settings from the environment (docs/interfaces.md,
section 3). Secrets are never given defaults: use `require()` for them.
"""

from __future__ import annotations

import os
import re
import socket
from collections.abc import Mapping
from pathlib import Path

_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off", ""}

MIN_TOKEN_LEN = 16


class ConfigError(RuntimeError):
    """Raised when a required setting is missing or malformed."""


def _env(env: Mapping[str, str] | None) -> Mapping[str, str]:
    return os.environ if env is None else env


def get_str(name: str, default: str = "", env: Mapping[str, str] | None = None) -> str:
    return _env(env).get(name, default)


def get_int(name: str, default: int, env: Mapping[str, str] | None = None) -> int:
    raw = _env(env).get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError as e:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from e


def get_float(name: str, default: float, env: Mapping[str, str] | None = None) -> float:
    raw = _env(env).get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except ValueError as e:
        raise ConfigError(f"{name} must be a number, got {raw!r}") from e


def get_bool(name: str, default: bool, env: Mapping[str, str] | None = None) -> bool:
    raw = _env(env).get(name)
    if raw is None:
        return default
    val = raw.strip().lower()
    if val in _TRUE:
        return True
    if val in _FALSE:
        return False
    raise ConfigError(f"{name} must be a boolean, got {raw!r}")


def get_list(name: str, default: list[str] | None = None, env: Mapping[str, str] | None = None) -> list[str]:
    """Comma-separated list; blanks are dropped."""
    raw = _env(env).get(name)
    if raw is None:
        return list(default or [])
    return [item.strip() for item in raw.split(",") if item.strip()]


def require(name: str, env: Mapping[str, str] | None = None) -> str:
    """A setting with no default (URLs of required peers, secrets)."""
    val = _env(env).get(name, "").strip()
    if not val:
        raise ConfigError(f"{name} is required but not set")
    return val


def require_token(name: str, env: Mapping[str, str] | None = None) -> str:
    """A bearer token: required and at least MIN_TOKEN_LEN characters."""
    val = require(name, env)
    if len(val) < MIN_TOKEN_LEN:
        raise ConfigError(f"{name} must be at least {MIN_TOKEN_LEN} characters")
    return val


def prefixed(prefix: str, env: Mapping[str, str] | None = None) -> dict[str, str]:
    """All variables NAME=value whose name matches ^<prefix>[A-Z0-9_]+$, keyed by the suffix.

    Used e.g. for REGISTRY_TOKEN_<CITY>.
    """
    pat = re.compile(rf"^{re.escape(prefix)}([A-Z0-9_]+)$")
    out: dict[str, str] = {}
    for key, val in _env(env).items():
        m = pat.match(key)
        if m and val.strip():
            out[m.group(1)] = val.strip()
    return out


def instance_id(env: Mapping[str, str] | None = None) -> str:
    """INSTANCE_ID (the pod name in K8s via the downward API), else the hostname."""
    return get_str("INSTANCE_ID", "", env) or socket.gethostname()


def log_dir(env: Mapping[str, str] | None = None) -> Path:
    return Path(get_str("LOG_DIR", "./logs", env))


def bind_host(env: Mapping[str, str] | None = None) -> str:
    return get_str("BIND_HOST", "127.0.0.1", env)


def database_url(default: str, env: Mapping[str, str] | None = None) -> str:
    """DATABASE_URL normalised for SQLAlchemy 2 (sync engine).

    - sqlite:///relative/or/absolute.db: the parent directory is created.
    - postgres:// and postgresql:// are rewritten to postgresql+psycopg:// (psycopg 3).
    """
    url = get_str("DATABASE_URL", "", env) or default
    return normalise_database_url(url)


def normalise_database_url(url: str, create_dirs: bool = True) -> str:
    if url.startswith("postgres://"):
        url = "postgresql+psycopg://" + url[len("postgres://"):]
    elif url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url[len("postgresql://"):]
    elif url.startswith("sqlite:///") and create_dirs:
        path = url[len("sqlite:///"):]
        if path and path != ":memory:":
            Path(path).expanduser().parent.mkdir(parents=True, exist_ok=True)
    elif not (url.startswith("sqlite") or url.startswith("postgresql+")):
        raise ConfigError(f"unsupported DATABASE_URL scheme: {url.split(':', 1)[0]}")
    return url
