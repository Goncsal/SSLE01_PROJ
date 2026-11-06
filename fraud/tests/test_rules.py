"""The six rules as pure functions, each firing and quiet on its boundary."""

import datetime as dt

from conftest import CARD, T0
from evfraud import rules
from evfraud.geo import haversine_km
from evfraud.rules import CardEv
from evfraud.settings import Settings

CFG = Settings(api_token="t" * 24)
PORTO_LL = (41.1496, -8.6109)        # roster: pt.porto.st-001
LISBOA_LL = (38.711, -9.1366)        # roster: pt.lisboa.st-001
KM = haversine_km(*PORTO_LL, *LISBOA_LL)


def ev(city, hid, ll, ts=T0):
    return CardEv(ts, CARD, city, hid, ll[0], ll[1], None, 1)


def travel(minutes, cfg=CFG, prior_city="porto"):
    prior = ev(prior_city, "pt.porto.st-001.cp-1", PORTO_LL)
    here = ev("lisboa", "pt.lisboa.st-001.cp-1", LISBOA_LL, T0 + dt.timedelta(minutes=minutes))
    return rules.impossible_travel(here, [prior], cfg)


def test_distance_is_realistic():
    assert 270 < KM < 280


def test_impossible_travel_fires_and_is_quiet_on_the_boundary():
    limit_min = KM / CFG.max_kmh * 60            # about 82 minutes
    f = travel(limit_min - 1)
    assert f and f.rule == "impossible_travel" and f.severity == "critical" and f.action == "block_card"
    assert f.evidence["speed_kmh"] > CFG.max_kmh
    assert 270 < f.evidence["distance_km"] < 280
    assert f.other_hier_id == "pt.porto.st-001.cp-1"
    assert travel(limit_min + 1) is None          # slower than the limit: a real trip
    assert travel(limit_min * 5) is None


def test_impossible_travel_needs_two_cities_and_a_history():
    assert travel(1, prior_city="lisboa") is None            # same city is never "travel"
    here = ev("lisboa", "pt.lisboa.st-001.cp-1", LISBOA_LL)
    assert rules.impossible_travel(here, [], CFG) is None    # a single cross-city start alone
    no_coords = CardEv(T0, CARD, "porto", "pt.porto.st-001.cp-1")
    assert rules.impossible_travel(here, [no_coords], CFG) is None


def test_impossible_travel_follows_the_threshold():
    assert travel(30, cfg=Settings(api_token="t" * 24, max_kmh=1000)) is None
    assert travel(30, cfg=Settings(api_token="t" * 24, max_kmh=100)) is not None


def over(energy, duration=3600, max_kw=22.0):
    return rules.over_energy(hier_id="pt.porto.st-001.cp-1", city="porto", card=CARD,
                             transaction_id=7, energy_wh=energy, duration_s=duration,
                             max_kw=max_kw, cfg=CFG)


def test_over_energy_boundary():
    limit = 22.0 * 1000 * 1.15                   # 25300 Wh in an hour
    assert over(limit) is None
    f = over(limit + 1)
    assert f and f.action == "flag_session" and f.severity == "warning" and f.transaction_id == 7
    assert over(None) is None and over(100, duration=None) is None and over(10**9, max_kw=None) is None


def anomaly(reason):
    return rules.meter_backwards(hier_id="pt.porto.st-001.cp-1", city="porto", card=CARD,
                                 transaction_id=7, reason=reason)


def test_meter_backwards_reasons():
    assert anomaly("backwards") and anomaly("stop_mismatch")
    for other in ("over_power", "over_energy", "no_transaction", None):
        assert anomaly(other) is None


def peer(own, peers, cfg=CFG):
    return rules.peer_deviation(station_id="pt.porto.st-001", city="porto", own_wh=own,
                                peer_wh=peers, cfg=cfg)


def test_peer_deviation_needs_min_sessions_and_a_real_gap():
    peers = [10000.0] * 20
    f = peer([4000.0] * 3, peers)
    assert f and f.rule == "peer_deviation" and f.action is None
    assert peer([4000.0] * 3, peers[:19]) is None            # FRAUD_MIN_SESSIONS = 20
    assert peer([5000.0] * 3, peers) is None                 # exactly half the median: quiet
    assert peer([4999.0] * 3, peers) is not None
    assert peer([100.0] * 2, peers) is None                  # too few own sessions
    assert peer([100.0] * 3, [0.0] * 20) is None             # a dead city proves nothing
    assert peer([4000.0] * 3, peers[:5], Settings(api_token="t" * 24, min_sessions=5)) is not None


def gap(reported, grid):
    return rules.grid_gap(station_id="pt.porto.st-001", city="porto", reported_wh=reported,
                          grid_wh=grid, cfg=CFG)


def test_grid_gap_boundary():
    f = gap(7999, 10000)
    assert f and f.severity == "critical" and f.action == "quarantine"
    assert f.evidence["ratio"] <= 0.8
    assert gap(8000, 10000) is None                          # exactly 0.8: quiet
    assert gap(10000, 10000) is None
    assert gap(0, 500) is None                               # feeder barely moved


def velocity(n):
    starts = [T0 + dt.timedelta(minutes=i) for i in range(n)]
    return rules.card_velocity(card=CARD, city="porto", hier_id=None, starts=starts,
                               now=starts[-1], cfg=CFG)


def test_card_velocity_boundary():
    assert velocity(8) is None                               # 8 in an hour is allowed
    f = velocity(9)
    assert f and f.rule == "card_velocity" and f.action is None


def test_card_velocity_counts_only_the_last_hour():
    starts = [T0 + dt.timedelta(minutes=10 * i) for i in range(12)]    # 12 starts over 110 min
    f = rules.card_velocity(card=CARD, city=None, hier_id=None, starts=starts,
                            now=starts[-1], cfg=CFG)
    assert f is None                                          # only 6 fall in the last hour
