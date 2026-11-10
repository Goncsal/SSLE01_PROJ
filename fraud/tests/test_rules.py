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


def travel(minutes, cfg=CFG, prior_city="porto", stopped=frozenset({1})):
    """Porto, then Lisboa `minutes` later. By default the Porto session (tx 1) has stopped."""
    prior = ev(prior_city, "pt.porto.st-001.cp-1", PORTO_LL)
    here = ev("lisboa", "pt.lisboa.st-001.cp-1", LISBOA_LL, T0 + dt.timedelta(minutes=minutes))
    return rules.impossible_travel(here, [prior], cfg, stopped)


def minutes_for(kmh):
    return KM / kmh * 60


def test_distance_is_realistic():
    assert 270 < KM < 280


def test_impossible_travel_fires_and_is_quiet_on_the_boundary():
    limit_min = KM / CFG.max_kmh * 60            # about 82 minutes
    f = travel(limit_min - 1)
    assert f and f.rule == "impossible_travel" and f.severity == "critical"
    assert f.evidence["speed_kmh"] > CFG.max_kmh
    assert 270 < f.evidence["distance_km"] < 280
    assert f.other_hier_id == "pt.porto.st-001.cp-1"
    assert travel(limit_min + 1) is None          # slower than the limit: a real trip
    assert travel(limit_min * 5) is None


def test_201_kmh_alerts_but_does_not_auto_block():
    """The honest case seen live: sequential roaming right above the alert speed."""
    for kmh in (201, 250, 399):
        f = travel(minutes_for(kmh))
        assert f and f.rule == "impossible_travel", kmh
        assert f.action is None and f.evidence["high_confidence"] is False
        assert f.evidence["concurrent"] is False
        assert f.dedup_key.endswith(":alert")
    assert travel(minutes_for(200.5)) is not None          # still an alert just above 200
    assert travel(minutes_for(199)) is None                # below the alert speed: nothing


def test_high_implied_speed_auto_blocks_at_block_kmh():
    f = travel(minutes_for(401))
    assert f and f.action == "block_card" and f.evidence["high_confidence"] is True
    assert f.dedup_key.endswith(":block")
    assert travel(minutes_for(1312)).action == "block_card"          # the live clone
    assert travel(minutes_for(399)).action is None                   # just under FRAUD_BLOCK_KMH
    low = Settings(api_token="t" * 24, block_kmh=250.0)
    assert travel(minutes_for(251), cfg=low).action == "block_card"
    assert travel(minutes_for(249), cfg=low).action is None


def test_overlapping_sessions_auto_block_below_block_speed():
    # 55 minutes apart is about 300 km/h (alert only on its own), but the Porto session (tx 1)
    # never stopped: the same card charging in two places at once
    f = travel(minutes_for(300), stopped=frozenset())
    assert f and f.action == "block_card" and f.evidence["concurrent"] is True
    assert f.evidence["speed_kmh"] < CFG.block_kmh and "overlapping" in f.detail
    assert travel(minutes_for(300), stopped=frozenset({1})).action is None   # sequential: alert only
    # a session with no stop for longer than the age bound is a lost stop event, not a clone
    aged = Settings(api_token="t" * 24, concurrent_max_age_s=60.0)
    assert travel(minutes_for(300), cfg=aged, stopped=frozenset()).action is None
    # overlap with a slow trip is still caught: 3 h apart (90 km/h) inside a 4 h age bound
    wide = Settings(api_token="t" * 24, concurrent_max_age_s=4 * 3600)
    f = travel(minutes_for(90), cfg=wide, stopped=frozenset())
    assert f and f.action == "block_card" and f.evidence["speed_kmh"] < CFG.max_kmh


def test_block_kmh_must_not_undercut_the_alert_speed():
    import pytest
    from evcommon import config
    with pytest.raises(config.ConfigError):
        Settings(api_token="t" * 24, max_kmh=300.0, block_kmh=250.0)


def test_impossible_travel_needs_two_cities_and_a_history():
    assert travel(1, prior_city="lisboa") is None            # same city is never "travel"
    here = ev("lisboa", "pt.lisboa.st-001.cp-1", LISBOA_LL)
    assert rules.impossible_travel(here, [], CFG) is None    # a single cross-city start alone
    no_coords = CardEv(T0, CARD, "porto", "pt.porto.st-001.cp-1")
    assert rules.impossible_travel(here, [no_coords], CFG) is None


def test_impossible_travel_follows_the_threshold():
    assert travel(30, cfg=Settings(api_token="t" * 24, max_kmh=1000, block_kmh=1000)) is None
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


def gap(reported, grid, sessions=5, prior=None):
    prior = prior or (reported, grid)
    return rules.grid_gap(station_id="pt.porto.st-001", city="porto", reported_wh=reported,
                          grid_wh=grid, sessions=sessions, prior_reported_wh=prior[0],
                          prior_grid_wh=prior[1], cfg=CFG)


def test_grid_gap_boundary():
    f = gap(7999, 10000)
    assert f and f.severity == "warning" and f.action is None          # alert only, never isolates
    assert f.score < rules.WEIGHTS["power_gap"]
    assert f.evidence["ratio"] <= 0.8
    assert gap(8000, 10000) is None                          # exactly 0.8: quiet
    assert gap(10000, 10000) is None
    assert gap(0, 500) is None                               # feeder barely moved


def test_grid_gap_quiet_without_completed_sessions():
    # the live false positive: a session still charging, nothing reported yet
    assert gap(0, 1265, sessions=0) is None
    assert gap(0, 50000, sessions=0) is None                 # however much the feeder moved
    assert gap(300, 50000, sessions=5) is None               # under the reported-energy floor
    assert gap(4000, 10000, sessions=1) is None              # under the session floor
    assert gap(4000, 10000, sessions=2) is not None


def test_grid_gap_quiet_on_a_transient_lag():
    # low now, but the same test a few minutes ago was fine: the gap is closing
    assert gap(5200, 10000, prior=(9500, 10000)) is None
    assert gap(5200, 10000, prior=(5000, 900)) is None       # nothing settled on the feeder before
    assert gap(5200, 10000, prior=(0, 10000)) is None        # nothing booked before: sessions just stopped
    assert gap(5200, 10000, prior=(5000, 10000)) is not None  # held at both points


def pg(samples, now=None, cfg=CFG):
    """samples: (seconds before `now`, feeder W, gap W)."""
    now = now or T0
    rows = [(now - dt.timedelta(seconds=s), f, g) for s, f, g in samples]
    return rules.power_gap(station_id="pt.porto.st-001", city="porto", samples=rows, now=now, cfg=cfg)


def run(feeder, gap_w, n=20, step=30):
    return [(step * i, feeder, gap_w) for i in range(n)]


def test_power_gap_fires_on_a_sustained_half_scale_tamper():
    f = pg(run(12000.0, 6000.0))                  # feeder twice the reported power
    assert f and f.rule == "power_gap" and f.severity == "critical" and f.action == "quarantine"
    assert f.evidence["gap_ratio"] == 0.5 and f.evidence["samples"] == 20


def test_power_gap_quiet_for_honest_stations():
    assert pg(run(12000.0, 0.0)) is None                       # feeder == reported, the live norm
    assert pg(run(12000.0, 500.0)) is None                     # under the watt floor
    assert pg(run(50000.0, 2000.0)) is None                    # 4 % of a big feeder: under the ratio
    assert pg(run(12000.0, -800.0)) is None                    # reported above feeder
    assert pg(run(12000.0, 6000.0, n=9)) is None               # not enough charging samples yet
    assert pg(run(600.0, 600.0)) is None                       # barely charging: nothing to compare


def test_power_gap_needs_to_be_sustained():
    closed = [(30 * i, 12000.0, 6000.0 if i >= 12 else 0.0) for i in range(20)]
    assert pg(closed) is None                                  # a gap that closed (or just opened): 8 of 20
    ok = [(30 * i, 12000.0, 6000.0 if i % 5 else 0.0) for i in range(20)]
    assert pg(ok) is not None                                  # 80 % of samples: exactly at the bar
    bad = [(30 * i, 12000.0, 6000.0 if i % 4 else 0.0) for i in range(20)]
    assert pg(bad) is None                                     # 15 of 20 = 75 %, under the bar
    assert pg([(30 * i, 12000.0, 6000.0 if i % 3 else 0.0) for i in range(20)]) is None   # 13/20 = 65 %
    old = [(2000 + 30 * i, 12000.0, 6000.0) for i in range(20)]
    assert pg(old) is None                                     # outside the look-back window
    idle = run(12000.0, 6000.0, n=12) + [(400 + 30 * i, 0.0, 0.0) for i in range(8)]
    assert pg(idle) is not None                                # idle samples do not dilute


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
