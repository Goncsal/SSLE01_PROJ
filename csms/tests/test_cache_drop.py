"""A command drops the registry cache entry of its charger on every replica (section 4.2)."""

import asyncio
import contextlib
import os
import time

import pytest
from evcsms.bus import PgNotifyBus
from evcommon.models import Node
from helpers import CHARGER, FakeRegistry, connected, make_node, refused_status, running

PG_URL = os.environ.get("TEST_DATABASE_URL", "")


def test_command_drops_the_cache_entry_but_probe_does_not(tmp_path):
    async def go():
        registry = FakeRegistry(make_node())
        async with running(tmp_path, registry) as srv, srv.api as api:
            async with connected(srv):
                registry.dropped.clear()
                assert (await api.post(f"/commands/{CHARGER}/change-availability",
                                       json={"connector_id": 0, "type": "Inoperative"})).status_code == 200
                assert registry.dropped == [CHARGER]
    asyncio.run(go())


def test_cache_drop_notification_drops_the_entry():
    registry = FakeRegistry()
    ctx = type("Ctx", (), {"registry": registry})()
    bus = PgNotifyBus(ctx, "postgresql+psycopg://u:p@db/x")
    bus._on_notify("csms_cache_drop", CHARGER)
    assert registry.dropped == [CHARGER]


class CachingRegistry(FakeRegistry):
    """Like the real client: remembers an answer for 30 s. Counts the lookups."""

    def __init__(self, *nodes: Node):
        super().__init__(*nodes)
        self.lookups = 0
        self.cache: dict[str, tuple[float, Node | None]] = {}

    async def get(self, hier_id: str):
        hit = self.cache.get(hier_id)
        if hit and hit[0] > time.monotonic():
            return hit[1]
        self.lookups += 1
        node = await super().get(hier_id)
        self.cache[hier_id] = (time.monotonic() + 30, node)
        return node

    def drop(self, hier_id: str) -> None:
        super().drop(hier_id)
        self.cache.pop(hier_id, None)


@pytest.mark.skipif(not PG_URL, reason="TEST_DATABASE_URL is not set")
def test_cache_drop_reaches_the_other_replica(tmp_path):
    from test_replicas import A, B, FAST, empty_database

    async def go():
        empty_database()
        reg_a, reg_b = CachingRegistry(make_node()), CachingRegistry(make_node())
        async with contextlib.AsyncExitStack() as stack:
            a = await stack.enter_async_context(
                running(tmp_path, reg_a, **{**A, "CSMS_OCPP_LISTEN_PORT": "19201",
                                            "CSMS_API_LISTEN_PORT": "18201"}, **FAST))
            b = await stack.enter_async_context(
                running(tmp_path, reg_b, **{**B, "CSMS_OCPP_LISTEN_PORT": "19202",
                                            "CSMS_API_LISTEN_PORT": "18202"}, **FAST))
            async with connected(b):
                pass                                    # B has the id cached as active
            await asyncio.sleep(0.3)
            assert CHARGER in reg_b.cache
            before = reg_b.lookups                      # the charger and its station
            # quarantine in the registry, then a command through A
            reg_a.nodes[CHARGER] = reg_b.nodes[CHARGER] = make_node(
                state="quarantined", effective_state="quarantined")
            async with a.api as api:
                resp = await api.post(f"/commands/{CHARGER}/disconnect", json={})
                assert resp.status_code == 404         # nobody holds it, the drop still happens
            for _ in range(50):
                if CHARGER not in reg_b.cache:
                    break
                await asyncio.sleep(0.1)
            assert CHARGER not in reg_b.cache
            assert await refused_status(b) == 403       # B asked the registry again
            assert reg_b.lookups > before
    asyncio.run(go())
