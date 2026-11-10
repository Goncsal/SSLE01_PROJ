"""Settings read from the environment (docs/interfaces.md, section 3.2)."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from evcommon import config


def _check_token(name: str, value: str) -> None:
    if value and len(value) < config.MIN_TOKEN_LEN:
        raise config.ConfigError(f"{name} must be at least {config.MIN_TOKEN_LEN} characters")


@dataclass(frozen=True)
class Settings:
    api_token: str
    database_url: str = "sqlite:///./data/fraud.db"
    bind_host: str = "127.0.0.1"
    port: int = 8003
    # scoring and thresholds (section 4.4)
    alert_score: float = 0.7
    max_kmh: float = 200.0           # impossible_travel ALERTS above this speed
    block_kmh: float = 400.0         # ... and auto-blocks the card only at or above this one
    concurrent_max_age_s: float = 3600.0   # an unstopped earlier session counts as overlapping for this long
    energy_tol: float = 1.15
    peer_factor: float = 0.5
    grid_factor: float = 0.8
    window_h: float = 6.0
    min_sessions: int = 20
    max_sessions_h: int = 8
    dry_run: bool = False
    # responses: isolate-only tokens, never admin ones
    billing_url: str = ""
    billing_responder_token: str = ""
    registry_url: str = ""
    registry_responder_token: str = ""
    csms_api_url: str = ""
    csms_responder_token: str = ""
    correlator_url: str = ""
    correlator_api_token: str = ""
    # additions to the contract (documented in the README)
    max_actions_h: int = 10          # automatic block/quarantine actions per hour
    prometheus_url: str = ""         # feeder-meter source for grid_gap; empty = rule inactive
    grid_poll_s: float = 30.0
    http_timeout_s: float = 3.0
    # grid_gap guards against timing lag (reported energy is booked at session stop)
    grid_min_sessions: int = 2        # completed sessions in the window before grid_gap may fire
    grid_min_reported_wh: float = 500.0   # ... and at least this much reported energy
    grid_settle_s: float = 900.0      # newest feeder energy left out (sessions not stopped yet)
    grid_sustain_s: float = 600.0     # the gap must also have held this long ago
    # power_gap (the quarantine trigger): near-real-time feeder power vs reported power
    power_gap_w: float = 1000.0       # feeder minus reported power at least this many watts ...
    power_gap_ratio: float = 0.3      # ... and at least this share of the feeder power (reported <= 70 %)
    power_gap_window_s: float = 900.0     # look-back window of the samples
    power_gap_min_samples: int = 10   # active (feeder >= power_gap_w) samples needed in the window
    power_gap_frac: float = 0.8       # share of those samples that must show the gap

    def __post_init__(self) -> None:
        _check_token("FRAUD_API_TOKEN", self.api_token)
        if not self.api_token:
            raise config.ConfigError("FRAUD_API_TOKEN is required but not set")
        for name, val in (("BILLING_RESPONDER_TOKEN", self.billing_responder_token),
                          ("REGISTRY_RESPONDER_TOKEN", self.registry_responder_token),
                          ("CSMS_RESPONDER_TOKEN", self.csms_responder_token),
                          ("CORRELATOR_API_TOKEN", self.correlator_api_token)):
            _check_token(name, val)
        if self.block_kmh < self.max_kmh:
            raise config.ConfigError("FRAUD_BLOCK_KMH must be at least FRAUD_MAX_KMH")
        if not 0 < self.alert_score <= 1:
            raise config.ConfigError("FRAUD_ALERT_SCORE must be in (0, 1]")

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        s = lambda name: config.get_str(name, "", env).strip()  # noqa: E731
        d = cls.__dataclass_fields__
        return cls(
            api_token=config.require_token("FRAUD_API_TOKEN", env),
            database_url=config.database_url("sqlite:///./data/fraud.db", env),
            bind_host=config.bind_host(env),
            port=config.get_int("FRAUD_LISTEN_PORT", 8003, env),
            alert_score=config.get_float("FRAUD_ALERT_SCORE", d["alert_score"].default, env),
            max_kmh=config.get_float("FRAUD_MAX_KMH", d["max_kmh"].default, env),
            block_kmh=config.get_float("FRAUD_BLOCK_KMH", d["block_kmh"].default, env),
            concurrent_max_age_s=config.get_float("FRAUD_CONCURRENT_MAX_AGE_S",
                                                  d["concurrent_max_age_s"].default, env),
            energy_tol=config.get_float("FRAUD_ENERGY_TOL", d["energy_tol"].default, env),
            peer_factor=config.get_float("FRAUD_PEER_FACTOR", d["peer_factor"].default, env),
            grid_factor=config.get_float("FRAUD_GRID_FACTOR", d["grid_factor"].default, env),
            window_h=config.get_float("FRAUD_WINDOW_H", d["window_h"].default, env),
            min_sessions=config.get_int("FRAUD_MIN_SESSIONS", d["min_sessions"].default, env),
            max_sessions_h=config.get_int("FRAUD_MAX_SESSIONS_H", d["max_sessions_h"].default, env),
            dry_run=config.get_bool("FRAUD_DRY_RUN", False, env),
            billing_url=s("BILLING_URL"), billing_responder_token=s("BILLING_RESPONDER_TOKEN"),
            registry_url=s("REGISTRY_URL"), registry_responder_token=s("REGISTRY_RESPONDER_TOKEN"),
            csms_api_url=s("CSMS_API_URL"), csms_responder_token=s("CSMS_RESPONDER_TOKEN"),
            correlator_url=s("CORRELATOR_URL"), correlator_api_token=s("CORRELATOR_API_TOKEN"),
            max_actions_h=config.get_int("FRAUD_MAX_ACTIONS_H", d["max_actions_h"].default, env),
            prometheus_url=s("FRAUD_PROMETHEUS_URL"),
            grid_poll_s=config.get_float("FRAUD_GRID_POLL_S", d["grid_poll_s"].default, env),
            grid_min_sessions=config.get_int("FRAUD_GRID_MIN_SESSIONS", d["grid_min_sessions"].default, env),
            grid_min_reported_wh=config.get_float("FRAUD_GRID_MIN_REPORTED_WH",
                                                  d["grid_min_reported_wh"].default, env),
            grid_settle_s=config.get_float("FRAUD_GRID_SETTLE_S", d["grid_settle_s"].default, env),
            grid_sustain_s=config.get_float("FRAUD_GRID_SUSTAIN_S", d["grid_sustain_s"].default, env),
            power_gap_w=config.get_float("FRAUD_POWER_GAP_W", d["power_gap_w"].default, env),
            power_gap_ratio=config.get_float("FRAUD_POWER_GAP_RATIO", d["power_gap_ratio"].default, env),
            power_gap_window_s=config.get_float("FRAUD_POWER_GAP_WINDOW_S",
                                                d["power_gap_window_s"].default, env),
            power_gap_min_samples=config.get_int("FRAUD_POWER_GAP_MIN_SAMPLES",
                                                 d["power_gap_min_samples"].default, env),
            power_gap_frac=config.get_float("FRAUD_POWER_GAP_FRAC", d["power_gap_frac"].default, env),
        )
