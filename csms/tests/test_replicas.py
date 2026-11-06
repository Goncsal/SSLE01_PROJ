"""Several replicas on one Postgres. Skipped unless TEST_DATABASE_URL is set."""

import asyncio
import contextlib
import datetime as dt
import os

import pytest
from helpers import (CHARGER, FakeRegistry, connected, insert_row, make_node, refused_status,
                     running, table_rows)

PG_URL = os.environ.get("TEST_DATABASE_URL", "")
pytestmark = pytest.mark.skipif(not PG_URL, reason="TEST_DATABASE_URL is not set")

# replica A and B: OCPP and API ports reserved for these tests
A = {"INSTANCE_ID": "csms-a", "CSMS_OCPP_LISTEN_PORT": "19101", "CSMS_API_LISTEN_PORT": "18101"}
B = {"INSTANCE_ID": "csms-b", "CSMS_OCPP_LISTEN_PORT": "19102", "CSMS_API_LISTEN_PORT": "18102"}
FAST = {"DATABASE_URL": PG_URL, "CSMS_POD_HEARTBEAT_S": "0.5", "CSMS_POD_STALE_S": "2",
        "CSMS_CMD_TIMEOUT_S": "5"}


def empty_database() -> None:
    from evcsms import db
    from sqlalchemy import create_engine
    engine = create_engine(PG_URL)
    with engine.begin() as conn:
        db.metadata.drop_all(conn)
    engine.dispose()


@contextlib.asynccontextmanager
async def two_replicas(tmp_path, registry=None):
    """Replicas A and B on the same empty database."""
    empty_database()
    registry = registry or FakeRegistry(make_node(), make_node("pt.porto.st-003.cp-3"))
    async with contextlib.AsyncExitStack() as stack:
        a = await stack.enter_async_context(running(tmp_path, registry, **A, **FAST))
        b = await stack.enter_async_context(running(tmp_path, registry, **B, **FAST))
        yield a, b


def test_command_reaches_a_charger_on_the_other_replica(tmp_path):
    async def go():
        async with two_replicas(tmp_path) as (a, b), b.api as api_b:
            async with connected(a):
                body = {"connector_id": 0, "type": "Inoperative", "source": "wazuh"}
                resp = await api_b.post(f"/commands/{CHARGER}/change-availability", json=body)
                assert resp.status_code == 200
                assert resp.json()["status"] == "Accepted" and resp.json()["pod"] == "csms-a"
                row = table_rows(a, "commands")[0]
                assert row["target_pod"] == "csms-a" and row["status"] == "done"
                # remote-stop: refused by the CSMS itself (409), then accepted for a real transaction
                resp = await api_b.post(f"/commands/{CHARGER}/remote-stop", json={})
                assert resp.status_code == 409 and resp.json()["status"] == "Rejected"
    asyncio.run(go())


def test_command_for_a_charger_nobody_holds_is_404(tmp_path):
    async def go():
        async with two_replicas(tmp_path) as (a, b), b.api as api_b:
            resp = await api_b.post(f"/commands/{CHARGER}/disconnect", json={})
            assert resp.status_code == 404
    asyncio.run(go())


def test_disconnect_through_the_other_replica_closes_the_socket(tmp_path):
    async def go():
        async with two_replicas(tmp_path) as (a, b), b.api as api_b:
            async with connected(a) as cp:
                assert (await api_b.post(f"/commands/{CHARGER}/disconnect", json={})).status_code == 200
                await cp._connection.wait_closed()
                assert cp._connection.close_code == 1008
    asyncio.run(go())


def test_connections_lists_every_replica(tmp_path):
    async def go():
        async with two_replicas(tmp_path) as (a, b), a.api as api_a:
            async with connected(a), connected(b, "pt.porto.st-003.cp-3"):
                items = (await api_a.get("/connections")).json()["items"]
                assert {i["pod"] for i in items} == {"csms-a", "csms-b"}
    asyncio.run(go())


def test_duplicate_on_the_other_replica_is_409_while_the_first_is_alive(tmp_path):
    async def go():
        async with two_replicas(tmp_path) as (a, b):
            async with connected(a):
                assert await refused_status(b) == 409
                assert any(e["other_srcip"] == "127.0.0.1" for e in b.events("charger.duplicate_id"))
                assert not b.events("conn.replaced")
            # the first one left cleanly, so its row is gone and the id is free
            await asyncio.sleep(0.3)
            async with connected(b, boot=False):
                pass
    asyncio.run(go())


def test_duplicate_is_accepted_when_the_owner_replica_is_dead(tmp_path):
    async def go():
        async with two_replicas(tmp_path) as (a, b):
            now = dt.datetime.now(dt.UTC)
            long_ago = now - dt.timedelta(minutes=5)
            # rows left by a replica that crashed: one never heartbeat again, one has no pod row
            insert_row(PG_URL, "csms_pods", pod="csms-dead", started_at=long_ago, heartbeat_at=long_ago)
            for pod, hier_id in (("csms-dead", CHARGER), ("csms-gone", "pt.porto.st-003.cp-3")):
                insert_row(PG_URL, "connections", hier_id=hier_id, city="porto", station="st-003",
                           pod=pod, src_ip="10.9.9.9", tls=False, since=long_ago, last_seen=long_ago)
            async with connected(a, boot=False), connected(b, "pt.porto.st-003.cp-3", boot=False):
                assert len(a.events("conn.replaced")) == 2      # one log file for both replicas
                assert {r["pod"] for r in table_rows(a, "connections")} == {"csms-a", "csms-b"}
    asyncio.run(go())


def test_pod_stopped_cleanly_leaves_no_rows(tmp_path):
    async def go():
        empty_database()
        async with running(tmp_path, **A, **FAST) as a:
            async with connected(a):
                assert table_rows(a, "csms_pods")[0]["pod"] == "csms-a"
        rows = table_rows(a, "connections"), table_rows(a, "csms_pods")
        assert rows == ([], [])
    asyncio.run(go())


def test_concurrent_tx_is_seen_across_replicas(tmp_path):
    async def go():
        async with two_replicas(tmp_path) as (a, b):
            async with connected(a) as cp_a, connected(b, "pt.porto.st-003.cp-3") as cp_b:
                assert (await cp_a.start_tx()).id_tag_info["status"] == "Accepted"
                assert (await cp_b.start_tx()).id_tag_info["status"] == "ConcurrentTx"
    asyncio.run(go())


def test_transaction_started_on_one_replica_is_stopped_on_the_other(tmp_path):
    async def go():
        async with two_replicas(tmp_path) as (a, b):
            async with connected(a) as cp_a:
                tx = (await cp_a.start_tx()).transaction_id
                await cp_a.meter(tx, 1050, 10000, 10)
            # the charger moves to replica B without rebooting and carries on
            async with connected(b, boot=False) as cp_b:
                await cp_b.meter(tx, 1100, 10000, 20)
                await cp_b.stop_tx(tx, 1150)
            assert not a.events("meter.anomaly")
            row = table_rows(a, "sessions", transaction_id=tx)[0]
            assert row["state"] == "closed" and row["meter_stop_wh"] == 1150
    asyncio.run(go())
