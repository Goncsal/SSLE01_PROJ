"""All CSMS settings, read from environment variables (docs/interfaces.md, section 3)."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from evcommon import config


@dataclass(frozen=True)
class Settings:
    """Typed view of the environment. Tests build one with `from_env({...})`."""

    api_token: str
    responder_token: str = ""   # optional, isolate-only (section 4.2); empty = off
    instance: str = "local"
    bind_host: str = "127.0.0.1"
    ocpp_port: int = 9000
    api_port: int = 8000
    database_url: str = "sqlite:///./data/csms.db"
    registry_url: str = "http://127.0.0.1:8001"
    billing_url: str = ""
    billing_token: str = ""
    fraud_url: str = ""
    fraud_token: str = ""
    trusted_proxies: tuple[str, ...] = ()
    allow_direct: bool = False
    plain_cities: tuple[str, ...] = ("aveiro",)
    enforce_station_ip: bool = False
    check_concurrent_tx: bool = True
    heartbeat_interval: int = 30
    meter_interval: int = 10
    power_tolerance: float = 1.10
    rate_per_s: float = 1.0
    rate_burst: int = 20
    rate_kick_after_s: float = 30.0
    registry_cache_s: float = 30.0
    registry_fail_open: bool = False
    billing_fail_open: bool = False
    cmd_timeout_s: float = 10.0
    meter_flush_s: float = 60.0
    pod_heartbeat_s: float = 5.0
    pod_stale_s: float = 15.0
    blocked_cards: frozenset[str] = field(default_factory=frozenset)
    probe_timeout_s: float = 5.0   # how long a duplicate-id probe waits for a pong

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        billing_url = config.get_str("BILLING_URL", "", env).strip()
        fraud_url = config.get_str("FRAUD_URL", "", env).strip()
        api_token = config.require_token("CSMS_API_TOKEN", env)
        responder_token = config.get_str("CSMS_RESPONDER_TOKEN", "", env).strip()
        if responder_token:
            responder_token = config.require_token("CSMS_RESPONDER_TOKEN", env)
            if responder_token == api_token:
                raise config.ConfigError("CSMS_RESPONDER_TOKEN must differ from CSMS_API_TOKEN")
        return cls(
            api_token=api_token,
            responder_token=responder_token,
            instance=config.instance_id(env),
            bind_host=config.bind_host(env),
            ocpp_port=config.get_int("CSMS_OCPP_LISTEN_PORT", 9000, env),
            api_port=config.get_int("CSMS_API_LISTEN_PORT", 8000, env),
            database_url=config.database_url("sqlite:///./data/csms.db", env),
            registry_url=config.get_str("REGISTRY_URL", "http://127.0.0.1:8001", env),
            billing_url=billing_url,
            billing_token=config.require_token("BILLING_API_TOKEN", env) if billing_url else "",
            fraud_url=fraud_url,
            fraud_token=config.require_token("FRAUD_API_TOKEN", env) if fraud_url else "",
            trusted_proxies=tuple(config.get_list("CSMS_TRUSTED_PROXIES", env=env)),
            allow_direct=config.get_bool("CSMS_ALLOW_DIRECT", False, env),
            plain_cities=tuple(config.get_list("CSMS_PLAIN_CITIES", ["aveiro"], env)),
            enforce_station_ip=config.get_bool("CSMS_ENFORCE_STATION_IP", False, env),
            check_concurrent_tx=config.get_bool("CSMS_CHECK_CONCURRENT_TX", True, env),
            heartbeat_interval=config.get_int("CSMS_HEARTBEAT_INTERVAL", 30, env),
            meter_interval=config.get_int("CSMS_METER_INTERVAL", 10, env),
            power_tolerance=config.get_float("CSMS_POWER_TOLERANCE", 1.10, env),
            rate_per_s=config.get_float("CSMS_RATE_LIMIT_PER_S", 1.0, env),
            rate_burst=config.get_int("CSMS_RATE_BURST", 20, env),
            rate_kick_after_s=config.get_float("CSMS_RATE_KICK_AFTER_S", 30.0, env),
            registry_cache_s=config.get_float("CSMS_REGISTRY_CACHE_S", 30.0, env),
            registry_fail_open=config.get_bool("CSMS_REGISTRY_FAIL_OPEN", False, env),
            billing_fail_open=config.get_bool("CSMS_BILLING_FAIL_OPEN", False, env),
            cmd_timeout_s=config.get_float("CSMS_CMD_TIMEOUT_S", 10.0, env),
            meter_flush_s=config.get_float("CSMS_METER_FLUSH_S", 60.0, env),
            pod_heartbeat_s=config.get_float("CSMS_POD_HEARTBEAT_S", 5.0, env),
            pod_stale_s=config.get_float("CSMS_POD_STALE_S", 15.0, env),
            blocked_cards=frozenset(config.get_list("CSMS_LOCAL_BLOCKED_CARDS", env=env)),
        )
