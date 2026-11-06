"""OCPP behaviour: a full session, cards, meter anomalies, rate limit."""

import asyncio
import json

import websockets.asyncio.client as ws_client
from helpers import CARD, CHARGER, OTHER_CARD, FakeRegistry, connected, make_node, running


def test_full_session_end_to_end(tmp_path):
    async def go():
        async with running(tmp_path) as srv, srv.api as api:
            async with connected(srv) as cp:
                await cp.status("Available")
                assert (await cp.authorize()).id_tag_info["status"] == "Accepted"
                tx = (await cp.start_tx()).transaction_id
                await cp.status("Charging")
                await cp.meter(tx, 1050, 20000, 10)
                await cp.meter(tx, 1100, 21000, 20)

                item = (await api.get("/connections")).json()["items"][0]
                assert item["hier_id"] == CHARGER and item["booted"] is True
                assert item["status"] == "Charging" and item["active_transaction_id"] == tx
                assert item["tls"] is False and item["src_ip"] == "127.0.0.1"

                text = (await api.get("/metrics")).text
                st = 'city="porto",hier_id="pt.porto.st-003",station="st-003"'
                assert f"evcharge_csms_power_watts{{{st}}} 21000.0" in text
                assert f"evcharge_csms_energy_wh_total{{{st}}} 100.0" in text
                assert f"evcharge_csms_sessions_active{{{st}}} 1.0" in text

                await cp.stop_tx(tx, 1150)
                assert (await api.get("/connections")).json()["items"][0]["active_transaction_id"] is None
            await asyncio.sleep(0.2)
            assert (await api.get("/connections")).json()["count"] == 0
            for name in ("conn.open", "charger.boot", "charger.status", "auth.accept",
                         "session.start", "session.stop", "conn.close"):
                assert srv.events(name), name
            stop = srv.events("session.stop")[0]
            assert stop["energy_wh"] == 150 and stop["transaction_id"] == tx
            assert not srv.events("meter.anomaly")
    asyncio.run(go())


def test_boot_serial_mismatch_and_fault_are_logged(tmp_path):
    async def go():
        async with running(tmp_path) as srv:
            async with connected(srv, boot=False) as cp:
                await cp.boot(serial="OTHER-SERIAL")
                await cp.status("Faulted")
            assert srv.events("charger.boot_mismatch")[0]["expected"] == "EVL22-0032"
            assert srv.events("charger.fault")
    asyncio.run(go())


def test_blocked_card_is_rejected(tmp_path):
    async def go():
        async with running(tmp_path, CSMS_LOCAL_BLOCKED_CARDS=OTHER_CARD) as srv:
            async with connected(srv) as cp:
                assert (await cp.authorize(OTHER_CARD)).id_tag_info["status"] == "Blocked"
                started = await cp.start_tx(OTHER_CARD)
                assert started.id_tag_info["status"] == "Blocked"
                assert started.transaction_id > 0
            assert srv.events("auth.reject")[0]["result"] == "Blocked"
            assert srv.events("session.reject")
            assert not srv.events("session.start")
    asyncio.run(go())


def test_concurrent_transaction_is_rejected(tmp_path):
    async def go():
        other = "pt.porto.st-003.cp-1"
        async with running(tmp_path, FakeRegistry(make_node(), make_node(other))) as srv:
            async with connected(srv) as first, connected(srv, other) as second:
                assert (await first.start_tx(CARD)).id_tag_info["status"] == "Accepted"
                assert (await second.start_tx(CARD)).id_tag_info["status"] == "ConcurrentTx"
            assert srv.events("auth.reject")[0]["result"] == "ConcurrentTx"
    asyncio.run(go())


def test_meter_going_backwards_is_flagged(tmp_path):
    async def go():
        async with running(tmp_path) as srv:
            async with connected(srv) as cp:
                tx = (await cp.start_tx(meter_start=5000)).transaction_id
                await cp.meter(tx, 5050, 10000, 10)
                await cp.meter(tx, 5030, 10000, 20)          # lower than before
                await cp.stop_tx(tx, 5020)                  # lower than the last reading
            reasons = [e["reason"] for e in srv.events("meter.anomaly")]
            assert reasons == ["backwards", "stop_mismatch"]
            first = srv.events("meter.anomaly")[0]
            assert first["meter_wh"] == 5030 and first["prev_meter_wh"] == 5050 and first["card"] == CARD
    asyncio.run(go())


def test_over_power_over_energy_and_no_transaction(tmp_path):
    async def go():
        async with running(tmp_path) as srv:      # charger is rated 22 kW
            async with connected(srv) as cp:
                tx = (await cp.start_tx(meter_start=0)).transaction_id
                await cp.meter(tx, 5, 40000, 10)             # 40 kW on a 22 kW charger
                await cp.meter(tx, 900000, 10000, 20)        # 900 kWh in a moment
                await cp.meter(tx + 99, 100, 100)        # not an open transaction
            reasons = [e["reason"] for e in srv.events("meter.anomaly")]
            assert reasons == ["over_power", "over_energy", "no_transaction"]
    asyncio.run(go())


def test_unknown_action_and_schema_violation(tmp_path):
    async def go():
        async with running(tmp_path) as srv:
            async with ws_client.connect(srv.url(), subprotocols=["ocpp1.6"]) as ws:
                await ws.send(json.dumps([2, "a", "Heartbeat", {}]))      # before boot
                assert json.loads(await ws.recv())[0] == 3
                await ws.send(json.dumps([2, "b", "DataTransfer", {"vendorId": "x"}]))
                assert json.loads(await ws.recv())[2] == "NotImplemented"
                await ws.send(json.dumps([2, "c", "BootNotification", {"chargePointModel": "m"}]))
                assert json.loads(await ws.recv())[0] == 4                # missing vendor
            reasons = [e["reason"] for e in srv.events("ocpp.invalid")]
            assert reasons == ["before_boot", "not_implemented", "schema"]
    asyncio.run(go())


def test_rate_limit_answers_call_error_then_kicks(tmp_path):
    async def go():
        env = dict(CSMS_RATE_BURST="3", CSMS_RATE_LIMIT_PER_S="0.1", CSMS_RATE_KICK_AFTER_S="1")
        async with running(tmp_path, **env) as srv:
            async with ws_client.connect(srv.url(), subprotocols=["ocpp1.6"]) as ws:
                answers = []
                for i in range(6):
                    await ws.send(json.dumps([2, f"id{i}", "Heartbeat", {}]))
                    answers.append(json.loads(await asyncio.wait_for(ws.recv(), 2)))
                assert [a[0] for a in answers] == [3, 3, 3, 4, 4, 4]
                assert answers[3][2:4] == ["GenericError", "rate limited"]
                for i in range(20):                       # keep flooding until kicked
                    try:
                        await ws.send(json.dumps([2, f"x{i}", "Heartbeat", {}]))
                        await asyncio.sleep(0.2)
                    except Exception:
                        break
                await ws.wait_closed()
                assert ws.close_code == 1008
            assert len(srv.events("ocpp.rate_limited")) == 1       # at most one line per 10 s
            assert srv.events("conn.kicked")[0]["reason"] == "rate_limit"
            assert 'evcharge_csms_rate_limited_total{city="porto"}' in (await srv.api.get("/metrics")).text
    asyncio.run(go())
