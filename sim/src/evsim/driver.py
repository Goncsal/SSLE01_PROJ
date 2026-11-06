"""Driver arrivals and charging sessions (docs/interfaces.md, section 9.3).

Pure, seeded helpers: every call takes the random.Random to use, so a run is
reproducible with --seed and easy to unit test without a network connection.
"""

from __future__ import annotations

import math
import random

from evcommon.roster import Roster

CAR_POWER_KW = (7.4, 11, 22, 50, 100, 150)
REJECTED_STOP_RATIO = 0.20  # fraction of normal sessions that end EVDisconnected


def arrival_interval_s(rng: random.Random, arrival_rate_per_hour: float) -> float:
    """Exponential wait, in seconds, for the next driver on one connector."""
    rate_per_s = max(arrival_rate_per_hour, 1e-9) / 3600.0
    return rng.expovariate(rate_per_s)


def session_minutes(rng: random.Random, mean_minutes: float) -> float:
    """Log-normal session length, clipped to [0.25, 4] times the mean."""
    sigma = 0.5
    mu = math.log(max(mean_minutes, 1e-6)) - sigma**2 / 2
    minutes = rng.lognormvariate(mu, sigma)
    lo, hi = 0.25 * mean_minutes, 4 * mean_minutes
    return min(max(minutes, lo), hi)


def car_power_kw(rng: random.Random) -> float:
    return rng.choice(CAR_POWER_KW)


def charger_power_w(rng: random.Random, car_kw: float, max_kw: float) -> float:
    """Instantaneous power for one sample: the car's limit capped by the
    charger, scaled by a steady per-session factor and resampled slightly."""
    base = min(car_kw, max_kw) * rng.uniform(0.90, 1.00)
    return base * 1000.0


def jitter_power_w(rng: random.Random, power_w: float) -> float:
    """+-2% resample applied at every meter sample."""
    return power_w * rng.uniform(0.98, 1.02)


def stop_reason(rng: random.Random) -> str:
    return "EVDisconnected" if rng.random() < REJECTED_STOP_RATIO else "Local"


def pick_card(rng: random.Random, roster: Roster, home_city: str, home_bias: float) -> str:
    """A driver's card: from this station's city with probability home_bias,
    else from any city (blocked cards stay in the pool: real rejections)."""
    if not roster.cards:
        raise ValueError("roster has no cards")
    home = roster.cards_for_city(home_city)
    if home and rng.random() < home_bias:
        return rng.choice(home).card
    return rng.choice(roster.cards).card
