"""Tests for the station simulator.

Plain functions running asyncio.run(); short intervals throughout so the
whole file runs in a few seconds. Most tests build a ChargerSim directly
(bypassing the __main__ CLI) against the local stub_csms, which keeps them
fast and gives direct access to the charger's internal state (e.g. the true
energy register, to check the tamper modes).
"""

from __future__ import annotations

import asyncio
import random
import sys
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import stub_c2  # noqa: E402
import stub_csms  # noqa: E402

from evcommon.jsonlog import setup_logging  # noqa: E402
from evcommon.roster import load_roster  # noqa: E402
from evsim import __main__ as cli  # noqa: E402
from evsim import attacks, metrics  # noqa: E402
from evsim.charger import ChargerOptions, ChargerSim  # noqa: E402

FIXTURE_ROSTER = Path(__file__).parent / "fixtures" / "roster.yaml"
STATION_LABELS = {"hier_id": "pt.porto.st-001", "city": "porto", "station": "st-001"}


class Recorder:
    """Collects every message the stub CSMS receives."""

    def __init__(self) -> None:
        self.messages: list[tuple[float, str, str, dict]] = []

    def __call__(self, action: str, charger_id: str, payload: dict) -> None:
        self.messages.append((time.monotonic(), action, charger_id, dict(payload)))

    def of(self, action: str, charger_id: str | None = None) -> list[tuple[float, str, str, dict]]:
        return [m for m in self.messages if m[1] == action and (charger_id is None or m[2] == charger_id)]

    def count(self, action: str, charger_id: str | None = None) -> int:
        return len(self.of(action, charger_id))


def _entry(roster, hier_id: str):
    return next(e for e in roster.iter_chargers() if e.hier_id == hier_id)


def _opts(csms_url: str, **kw) -> ChargerOptions:
    defaults = dict(pki_dir=None, reconnect_max=2.0, reject_backoff=0.4, meter_interval=0.2,
                    arrival_rate=36000.0, session_minutes=0.02, home_bias=1.0)
    defaults.update(kw)
    return ChargerOptions(csms_url=csms_url, **defaults)


async def _serve(port: int = 0, **kw):
    server = await stub_csms.create_server("127.0.0.1", port, **kw)
    return server, server.sockets[0].getsockname()[1]


async def _close(server) -> None:
    server.close()
    await server.wait_closed()


async def _run_for(sims: list[ChargerSim], seconds: float, extra_tasks=()) -> asyncio.Event:
    stopping = asyncio.Event()
    tasks = [asyncio.create_task(s.run(stopping)) for s in sims]
    tasks += [asyncio.create_task(t(stopping)) for t in extra_tasks]
    await asyncio.sleep(seconds)
    stopping.set()
    await asyncio.gather(*tasks, return_exceptions=True)
    return stopping


async def _wait_until(predicate, timeout: float = 5.0, interval: float = 0.02) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(interval)
    return False


# ---- boot, status, heartbeat ------------------------------------------------

def test_boot_status_and_heartbeat(tmp_path):
    asyncio.run(_boot_status_and_heartbeat(tmp_path))


async def _boot_status_and_heartbeat(tmp_path):
    rec = Recorder()
    roster = load_roster(FIXTURE_ROSTER)
    entry = _entry(roster, "pt.porto.st-001.cp-1")
    log = setup_logging("sim", instance="t-boot", log_dir=tmp_path, stdout=False)
    grid = metrics.GridMeter(STATION_LABELS)
    server, port = await _serve(boot_interval=1, on_message=rec)
    try:
        opts = _opts(f"ws://127.0.0.1:{port}/ocpp", arrival_rate=0.001)
        sim = ChargerSim(entry, roster, opts, random.Random(1), log, grid)
        await _run_for([sim], 2.5)
    finally:
        await _close(server)

    cid = entry.hier_id
    assert rec.count("BootNotification", cid) == 1
    status0 = [m[3] for m in rec.of("StatusNotification", cid) if m[3]["connector_id"] == 0]
    status1 = [m[3] for m in rec.of("StatusNotification", cid) if m[3]["connector_id"] == 1]
    assert status0 and status0[0]["status"] == "Available"
    assert status1 and status1[0]["status"] == "Available"
    assert rec.count("Heartbeat", cid) >= 1


# ---- a full charging session ------------------------------------------------

def test_full_session(tmp_path):
    asyncio.run(_full_session(tmp_path))


async def _full_session(tmp_path):
    rec = Recorder()
    roster = load_roster(FIXTURE_ROSTER)
    entry = _entry(roster, "pt.porto.st-001.cp-1")
    log = setup_logging("sim", instance="t-session", log_dir=tmp_path, stdout=False)
    grid = metrics.GridMeter(STATION_LABELS)
    server, port = await _serve(on_message=rec)
    try:
        opts = _opts(f"ws://127.0.0.1:{port}/ocpp", meter_interval=0.15, session_minutes=0.05)
        sim = ChargerSim(entry, roster, opts, random.Random(2), log, grid)
        await _run_for([sim], 3.0)
    finally:
        await _close(server)

    cid = entry.hier_id
    assert rec.count("Authorize", cid) >= 1
    starts = rec.of("StartTransaction", cid)
    assert starts
    meters = rec.of("MeterValues", cid)
    assert len(meters) >= 2
    registers = []
    for _, _, _, payload in meters:
        sample = payload["meter_value"][0]["sampled_value"]
        measurands = {s["measurand"] for s in sample}
        assert measurands == {"Energy.Active.Import.Register", "Power.Active.Import"}
        energy = next(s for s in sample if s["measurand"] == "Energy.Active.Import.Register")
        assert isinstance(energy["value"], str)
        assert energy["value"] == str(int(energy["value"]))  # an integer string
        registers.append(int(energy["value"]))
    assert registers == sorted(registers)  # strictly non-decreasing

    stops = rec.of("StopTransaction", cid)
    assert stops
    assert stops[0][3]["meter_stop"] >= registers[-1]


# ---- blocked card -----------------------------------------------------------

def test_blocked_card(tmp_path):
    asyncio.run(_blocked_card(tmp_path))


async def _blocked_card(tmp_path):
    blocked_card = "04C3D4E5F6A7B8"
    roster_yaml = tmp_path / "roster.yaml"
    roster_yaml.write_text(f"""
version: 1
country: pt
cities:
  - city: testville
    stations:
      - station: st-001
        name: Test
        lat: 40.0
        lon: -8.0
        chargers:
          - {{charger: cp-1, max_kw: 22, connectors: 1}}
cards:
  - {{card: {blocked_card}, holder: d, home_city: testville, blocked: true}}
""")
    rec = Recorder()
    roster = load_roster(roster_yaml)
    entry = _entry(roster, "pt.testville.st-001.cp-1")
    log = setup_logging("sim", instance="t-blocked", log_dir=tmp_path / "logs", stdout=False)
    grid = metrics.GridMeter({"hier_id": "pt.testville.st-001", "city": "testville", "station": "st-001"})
    server, port = await _serve(blocked_cards={blocked_card}, on_message=rec)
    try:
        opts = _opts(f"ws://127.0.0.1:{port}/ocpp")
        sim = ChargerSim(entry, roster, opts, random.Random(3), log, grid)
        await _run_for([sim], 1.5)
    finally:
        await _close(server)

    cid = entry.hier_id
    assert rec.count("Authorize", cid) >= 1
    assert rec.count("StartTransaction", cid) == 0
    log_text = (tmp_path / "logs" / "sim-t-blocked.json").read_text()
    assert '"event":"sim.auth_rejected"' in log_text
    assert '"result":"Blocked"' in log_text


# ---- RemoteStop, ChangeAvailability, Reset ----------------------------------

def test_remote_stop_and_availability_and_reset(tmp_path):
    asyncio.run(_remote_stop_and_availability_and_reset(tmp_path))


async def _remote_stop_and_availability_and_reset(tmp_path):
    rec = Recorder()
    roster = load_roster(FIXTURE_ROSTER)
    entry = _entry(roster, "pt.porto.st-001.cp-1")
    log = setup_logging("sim", instance="t-cmd", log_dir=tmp_path, stdout=False)
    grid = metrics.GridMeter(STATION_LABELS)
    server, port = await _serve(on_message=rec)
    try:
        opts = _opts(f"ws://127.0.0.1:{port}/ocpp", session_minutes=1.0, meter_interval=0.15)
        sim = ChargerSim(entry, roster, opts, random.Random(4), log, grid)
        stopping = asyncio.Event()
        task = asyncio.create_task(sim.run(stopping))

        # Remote stop: wait for a transaction, then stop it.
        await _wait_until(lambda: sim.active_tx.get(1) is not None, timeout=5.0)
        tx_id = sim.active_tx[1]
        assert sim.request_remote_stop(tx_id) is True
        await _wait_until(lambda: sim.active_tx.get(1) is None, timeout=5.0)
        stop = rec.of("StopTransaction", entry.hier_id)[-1][3]
        assert stop["reason"] == "Remote"

        # ChangeAvailability while idle: immediate Unavailable.
        assert await _wait_until(lambda: sim.status.get(1) == "Available", timeout=5.0)
        status = sim.request_availability(1, "Inoperative")
        assert status == "Accepted"
        assert await _wait_until(lambda: sim.status.get(1) == "Unavailable", timeout=5.0)

        # Back to Operative, start a session, then ChangeAvailability is Scheduled.
        sim.request_availability(1, "Operative")
        await _wait_until(lambda: sim.active_tx.get(1) is not None, timeout=5.0)
        status = sim.request_availability(1, "Inoperative")
        assert status == "Scheduled"
        await _wait_until(lambda: sim.status.get(1) == "Unavailable", timeout=5.0)
        sim.request_availability(1, "Operative")

        # Reset: reconnect and a new BootNotification.
        boots_before = rec.count("BootNotification", entry.hier_id)
        sim.request_reset("Soft")
        await _wait_until(lambda: rec.count("BootNotification", entry.hier_id) > boots_before, timeout=5.0)

        stopping.set()
        await asyncio.gather(task, return_exceptions=True)
    finally:
        await _close(server)


# ---- refused handshake -------------------------------------------------------

def test_refused_handshake_retries_after_backoff(tmp_path):
    asyncio.run(_refused_handshake(tmp_path))


async def _refused_handshake(tmp_path):
    roster = load_roster(FIXTURE_ROSTER)
    entry = _entry(roster, "pt.porto.st-001.cp-1")
    log = setup_logging("sim", instance="t-reject", log_dir=tmp_path, stdout=False)
    grid = metrics.GridMeter(STATION_LABELS)
    server, port = await _serve(reject_path_substr="cp-1")
    try:
        opts = _opts(f"ws://127.0.0.1:{port}/ocpp", reject_backoff=0.5)
        sim = ChargerSim(entry, roster, opts, random.Random(5), log, grid)
        await _run_for([sim], 1.7)
    finally:
        await _close(server)

    lines = [ln for ln in (tmp_path / "sim-t-reject.json").read_text().splitlines()
             if '"event":"sim.rejected"' in ln]
    assert len(lines) >= 2
    assert all('"http_status":403' in ln for ln in lines)


# ---- tamper: scale and rollback ----------------------------------------------

def test_tamper_scale_under_reports(tmp_path):
    asyncio.run(_tamper_scale(tmp_path))


async def _tamper_scale(tmp_path):
    rec = Recorder()
    roster = load_roster(FIXTURE_ROSTER)
    entry = _entry(roster, "pt.porto.st-001.cp-1")
    log = setup_logging("sim", instance="t-tamper-scale", log_dir=tmp_path, stdout=False)
    grid = metrics.GridMeter(STATION_LABELS)
    server, port = await _serve(on_message=rec)
    try:
        opts = _opts(f"ws://127.0.0.1:{port}/ocpp", meter_interval=0.15, session_minutes=0.05)
        tamper = attacks.TamperConfig(mode="scale", factor=0.3)
        sim = ChargerSim(entry, roster, opts, random.Random(6), log, grid, tamper=tamper)
        true_register_before = sim.registers[1]
        await _run_for([sim], 2.0)
    finally:
        await _close(server)

    stop = rec.of("StopTransaction", entry.hier_id)[0][3]
    start = rec.of("StartTransaction", entry.hier_id)[0][3]
    reported_delta = stop["meter_stop"] - start["meter_start"]
    true_delta = sim.registers[1] - true_register_before
    ratio = reported_delta / true_delta
    assert 0.15 <= ratio <= 0.55  # clearly under-reporting, around the 0.3 factor


def test_tamper_rollback_decreases_once(tmp_path):
    asyncio.run(_tamper_rollback(tmp_path))


async def _tamper_rollback(tmp_path):
    rec = Recorder()
    roster = load_roster(FIXTURE_ROSTER)
    entry = _entry(roster, "pt.porto.st-001.cp-1")
    log = setup_logging("sim", instance="t-tamper-rb", log_dir=tmp_path, stdout=False)
    grid = metrics.GridMeter(STATION_LABELS)
    server, port = await _serve(on_message=rec)
    try:
        opts = _opts(f"ws://127.0.0.1:{port}/ocpp", meter_interval=0.05, session_minutes=0.025)
        tamper = attacks.TamperConfig(mode="rollback", factor=1.0)
        sim = ChargerSim(entry, roster, opts, random.Random(7), log, grid, tamper=tamper)
        await _run_for([sim], 3.0)
    finally:
        await _close(server)

    meters = rec.of("MeterValues", entry.hier_id)
    registers = [int(next(s for s in m[3]["meter_value"][0]["sampled_value"]
                          if s["measurand"] == "Energy.Active.Import.Register")["value"])
                for m in meters]
    decreases = sum(1 for a, b in zip(registers, registers[1:]) if b < a)
    assert decreases == 1


# ---- clone card --------------------------------------------------------------

def test_clone_card(tmp_path):
    asyncio.run(_clone_card(tmp_path))


async def _clone_card(tmp_path):
    rec = Recorder()
    roster = load_roster(FIXTURE_ROSTER)
    entry = _entry(roster, "pt.porto.st-001.cp-1")
    log = setup_logging("sim", instance="t-clone", log_dir=tmp_path, stdout=False)
    grid = metrics.GridMeter(STATION_LABELS)
    clone_card = "04AAAAAAAAAAAA"
    server, port = await _serve(on_message=rec)
    try:
        opts = _opts(f"ws://127.0.0.1:{port}/ocpp", meter_interval=0.3, session_minutes=0.01)
        clone_state = attacks.CloneState(card=clone_card, sessions=2)
        sim = ChargerSim(entry, roster, opts, random.Random(8), log, grid, clone_state=clone_state)
        await _run_for([sim], 9.0)
    finally:
        await _close(server)

    cards = [m[3]["id_tag"] for m in rec.of("Authorize", entry.hier_id)]
    assert len(cards) >= 3
    assert cards[0] == clone_card
    assert cards[1] == clone_card
    assert cards[2] != clone_card

    log_text = (tmp_path / "sim-t-clone.json").read_text()
    assert f'"card":"{clone_card}"' not in log_text  # the cloned sessions are never logged


# ---- botnet: flood and surge -------------------------------------------------

def test_botnet_flood_raises_meter_rate(tmp_path):
    asyncio.run(_botnet_flood(tmp_path))


async def _botnet_flood(tmp_path):
    rec = Recorder()
    roster = load_roster(FIXTURE_ROSTER)
    entry = _entry(roster, "pt.porto.st-001.cp-1")
    log = setup_logging("sim", instance="t-flood", log_dir=tmp_path, stdout=False)
    grid = metrics.GridMeter(STATION_LABELS)

    c2 = ThreadingHTTPServer(("127.0.0.1", 0), stub_c2.make_handler("flood", rate_hz=25, duration_s=4))
    threading.Thread(target=c2.serve_forever, daemon=True).start()
    c2_url = f"http://127.0.0.1:{c2.server_address[1]}"

    server, port = await _serve(on_message=rec)
    try:
        bot_state = attacks.BotState()
        opts = _opts(f"ws://127.0.0.1:{port}/ocpp", meter_interval=1.0, session_minutes=1.0)
        sim = ChargerSim(entry, roster, opts, random.Random(9), log, grid, bot_state=bot_state)

        async def noop_surge(duration_s):
            return None

        await _run_for([sim], 3.0, extra_tasks=(
            lambda stopping: attacks.run_botnet(
                "pt.porto.st-001", [entry.hier_id], c2_url, 0.15, bot_state, noop_surge, stopping),
        ))
    finally:
        await _close(server)
        c2.shutdown()
        c2.server_close()

    meters = rec.of("MeterValues", entry.hier_id)
    assert len(meters) >= 4
    last_two_s = [t for t, *_ in meters if t >= meters[-1][0] - 1.0]
    rate = len(last_two_s) / 1.0
    assert rate >= 10  # well above the 1/meter_interval=1 baseline


def test_botnet_surge_starts_sessions_without_logging(tmp_path):
    asyncio.run(_botnet_surge(tmp_path))


async def _botnet_surge(tmp_path):
    rec = Recorder()
    roster = load_roster(FIXTURE_ROSTER)
    entry = _entry(roster, "pt.porto.st-001.cp-1")
    log = setup_logging("sim", instance="t-surge", log_dir=tmp_path, stdout=False)
    grid = metrics.GridMeter(STATION_LABELS)

    c2 = ThreadingHTTPServer(("127.0.0.1", 0), stub_c2.make_handler("surge", rate_hz=0, duration_s=3))
    threading.Thread(target=c2.serve_forever, daemon=True).start()
    c2_url = f"http://127.0.0.1:{c2.server_address[1]}"

    server, port = await _serve(on_message=rec)
    try:
        bot_state = attacks.BotState()
        opts = _opts(f"ws://127.0.0.1:{port}/ocpp", arrival_rate=0.0001, meter_interval=0.3)
        sim = ChargerSim(entry, roster, opts, random.Random(10), log, grid, bot_state=bot_state)

        async def on_surge(duration_s):
            await sim.force_session(1, duration_s, stopping_event)

        stopping_event = asyncio.Event()
        task = asyncio.create_task(sim.run(stopping_event))
        bot_task = asyncio.create_task(attacks.run_botnet(
            "pt.porto.st-001", [entry.hier_id], c2_url, 0.15, bot_state, on_surge, stopping_event))
        await asyncio.sleep(2.0)
        stopping_event.set()
        await asyncio.gather(task, bot_task, return_exceptions=True)
    finally:
        await _close(server)
        c2.shutdown()
        c2.server_close()

    assert rec.count("StartTransaction", entry.hier_id) >= 1
    log_text = (tmp_path / "sim-t-surge.json").read_text()
    assert "sim.session_start" not in log_text
    assert "sim.session_stop" not in log_text


# ---- /metrics labels ----------------------------------------------------------

def test_metrics_have_hier_labels(tmp_path):
    asyncio.run(_metrics_labels(tmp_path))


async def _metrics_labels(tmp_path):
    import prometheus_client as pc

    rec = Recorder()
    roster = load_roster(FIXTURE_ROSTER)
    entry = _entry(roster, "pt.porto.st-001.cp-2")
    log = setup_logging("sim", instance="t-metrics", log_dir=tmp_path, stdout=False)
    grid = metrics.GridMeter(STATION_LABELS)
    server, port = await _serve(on_message=rec)
    try:
        opts = _opts(f"ws://127.0.0.1:{port}/ocpp", arrival_rate=0.001)
        sim = ChargerSim(entry, roster, opts, random.Random(11), log, grid)
        await _run_for([sim], 1.0)
    finally:
        await _close(server)

    labels = {"hier_id": "pt.porto.st-001.cp-2", "city": "porto", "station": "st-001", "charger": "cp-2"}
    value = pc.REGISTRY.get_sample_value("evcharge_sim_charger_connected", labels)
    assert value == 0.0  # the charger disconnected cleanly at the end of the run


# ---- CLI argument validation --------------------------------------------------

def test_cli_rejects_unknown_station():
    assert cli.main(["--roster", str(FIXTURE_ROSTER), "--station", "pt.nowhere.st-001"]) == 2


def test_cli_rejects_missing_roster():
    assert cli.main(["--station", "pt.porto.st-001"]) == 2


def test_cli_rejects_bad_tamper_factor():
    assert cli.main(["--roster", str(FIXTURE_ROSTER), "--station", "pt.porto.st-001",
                     "--tamper", "1.5"]) == 2


def test_sleep_or_cancel_leaves_no_pending_tasks() -> None:
    async def scenario() -> list[asyncio.Task]:
        ev1, ev2 = asyncio.Event(), asyncio.Event()
        task = asyncio.ensure_future(ChargerSim._sleep_or(None, 30.0, ev1, ev2))
        await asyncio.sleep(0.05)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        await asyncio.sleep(0)
        me = asyncio.current_task()
        return [t for t in asyncio.all_tasks() if t is not me and not t.done()]

    assert asyncio.run(scenario()) == []


# ---- honest card selection is city-local --------------------------------------

def test_pick_card_is_city_local():
    from evsim import driver
    roster = load_roster(FIXTURE_ROSTER)
    rng = random.Random(1)
    for city in {c.home_city for c in roster.cards}:
        home = {c.card for c in roster.cards_for_city(city)}
        for bias in (1.0, 0.8, 0.0):
            assert {driver.pick_card(rng, roster, city, bias) for _ in range(300)} <= home


def test_two_cities_never_share_an_honest_card():
    from evsim import driver
    roster = load_roster(FIXTURE_ROSTER)
    cities = sorted({c.home_city for c in roster.cards})
    assert len(cities) >= 2
    rng = random.Random(2)
    used = {c: {driver.pick_card(rng, roster, c, 0.8) for _ in range(400)} for c in cities}
    for i, a in enumerate(cities):
        for b in cities[i + 1:]:
            assert not (used[a] & used[b])


def test_clone_pattern_stands_out_against_city_local_traffic():
    """A cloned card seen in a second city is the only cross-city card."""
    from evsim import driver
    roster = load_roster(FIXTURE_ROSTER)
    cities = sorted({c.home_city for c in roster.cards})
    rng = random.Random(3)
    honest = {c: {driver.pick_card(rng, roster, c, 1.0) for _ in range(300)} for c in cities}
    victim = next(iter(honest[cities[0]]))
    state = attacks.CloneState(card=victim, sessions=1)
    assert state.take() == victim                      # injected, bypasses honest selection
    seen_in = [c for c in cities if victim in honest[c]] + [cities[1]]  # clone used in city 2
    assert len(set(seen_in)) == 2                      # two cities, same card: impossible travel
