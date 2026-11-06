"""Sessions and the database (sections 5.3 and 8.3), on SQLite."""

import asyncio
import datetime as dt

from helpers import (CARD, CHARGER, OTHER_CARD, connected, insert_row, open_session_row, running,
                     table_rows)

BLOCKED = {"CSMS_LOCAL_BLOCKED_CARDS": OTHER_CARD}


def test_rejected_start_then_stop_is_not_an_anomaly(tmp_path):
    async def go():
        async with running(tmp_path, **BLOCKED) as srv:
            async with connected(srv) as cp:
                started = await cp.start_tx(card=OTHER_CARD)
                assert started.id_tag_info["status"] == "Blocked"
                await cp.stop_tx(started.transaction_id, 1000)
            assert srv.events("session.reject")
            assert not srv.events("meter.anomaly") and not srv.events("session.stop")
    asyncio.run(go())


def test_rejected_stop_on_a_new_connection_is_not_an_anomaly(tmp_path):
    async def go():
        async with running(tmp_path, **BLOCKED) as srv:
            async with connected(srv) as cp:
                tx = (await cp.start_tx(card=OTHER_CARD)).transaction_id
            async with connected(srv) as other:       # a new ChargePoint object knows nothing
                await other.status("Available", connector_id=0)
                await other.stop_tx(tx, 1000)
            assert not srv.events("meter.anomaly")
            assert table_rows(srv, "sessions", transaction_id=tx)[0]["state"] == "rejected"
    asyncio.run(go())


def test_stop_of_an_unknown_transaction_is_still_an_anomaly(tmp_path):
    async def go():
        async with running(tmp_path) as srv:
            async with connected(srv) as cp:
                await cp.stop_tx(4242, 1000)
            assert srv.events("meter.anomaly")[0]["reason"] == "no_transaction"
    asyncio.run(go())


def test_orphan_is_closed_as_power_loss_at_the_next_boot(tmp_path):
    async def go():
        async with running(tmp_path) as srv:
            async with connected(srv) as cp:
                tx = (await cp.start_tx()).transaction_id      # the connection is lost, no Stop
            assert table_rows(srv, "sessions", transaction_id=tx)[0]["state"] == "open"
            async with connected(srv) as cp:                   # boots again
                row = table_rows(srv, "sessions", transaction_id=tx)[0]
                assert row["state"] == "closed" and row["stop_reason"] == "PowerLoss"
                stop = srv.events("session.stop")[0]
                assert stop["reason"] == "PowerLoss" and stop["transaction_id"] == tx
                # the card is free again: no false ConcurrentTx
                assert (await cp.start_tx()).id_tag_info["status"] == "Accepted"
    asyncio.run(go())


def test_startup_keeps_other_replicas_rows(tmp_path):
    async def go():
        url = f"sqlite:///{tmp_path}/csms.db"
        async with running(tmp_path):                  # creates the tables
            pass
        now = dt.datetime.now(dt.UTC)
        insert_row(url, "csms_pods", pod="csms-other", started_at=now, heartbeat_at=now)
        for pod, hier_id in (("csms-other", "pt.porto.st-001.cp-1"), ("test", CHARGER)):
            insert_row(url, "connections", hier_id=hier_id, city="porto", station="st-001",
                       pod=pod, src_ip="10.0.0.1", tls=False, since=now, last_seen=now)
        session = open_session_row(url, hier_id="pt.porto.st-001.cp-1")
        async with running(tmp_path) as srv:           # same INSTANCE_ID "test" and database
            ids = {r["hier_id"] for r in table_rows(srv, "connections")}
            assert ids == {"pt.porto.st-001.cp-1"}     # only its own leftover is gone
            assert table_rows(srv, "sessions", transaction_id=session)[0]["state"] == "open"
    asyncio.run(go())


def test_meter_flush_writes_the_last_reading(tmp_path):
    async def go():
        async with running(tmp_path, CSMS_METER_FLUSH_S="0.3") as srv:
            async with connected(srv) as cp:
                tx = (await cp.start_tx()).transaction_id
                assert table_rows(srv, "sessions", transaction_id=tx)[0]["last_meter_wh"] is None
                await cp.meter(tx, 1070, 15000, 10)
                await asyncio.sleep(0.8)
                row = table_rows(srv, "sessions", transaction_id=tx)[0]
                assert row["last_meter_wh"] == 1070 and row["last_meter_at"] is not None
                assert row["peak_power_w"] == 15000
    asyncio.run(go())


def test_open_transaction_is_reloaded_from_the_database(tmp_path):
    async def go():
        async with running(tmp_path) as srv:
            async with connected(srv) as cp:
                # a transaction this process never saw, as if another replica had started it
                last = dt.datetime.now(dt.UTC)
                tx = open_session_row(srv.settings.database_url, meter_start_wh=1000,
                                      last_meter_wh=1100, last_meter_at=last)
                await cp.meter(tx, 1150, 20000, 10)
                await cp.meter(tx, 1100, 20000, 20)          # below the baseline 1150: backwards
                assert [e["reason"] for e in srv.events("meter.anomaly")] == ["backwards"]
                await cp.stop_tx(tx, 1200)
                row = table_rows(srv, "sessions", transaction_id=tx)[0]
                assert row["state"] == "closed" and row["meter_stop_wh"] == 1200
                assert srv.events("session.stop")[0]["energy_wh"] == 200
    asyncio.run(go())


def test_concurrent_tx_is_decided_by_the_database(tmp_path):
    async def go():
        async with running(tmp_path) as srv:
            async with connected(srv) as cp:
                open_session_row(srv.settings.database_url, hier_id="pt.porto.st-009.cp-1")
                assert (await cp.start_tx()).id_tag_info["status"] == "ConcurrentTx"
    asyncio.run(go())
