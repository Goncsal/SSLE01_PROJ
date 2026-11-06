"""Handshake refusals and acceptances (section 5.2)."""

import asyncio

from helpers import (CHARGER, FakeRegistry, connected, edge_headers, make_node, refused_status,
                     running)

EDGE = {"CSMS_TRUSTED_PROXIES": "127.0.0.1", "CSMS_ALLOW_DIRECT": "false"}


def test_bad_id_is_404(tmp_path):
    async def go():
        async with running(tmp_path) as srv:
            assert await refused_status(srv, path="/ocpp/not-an-id") == 404
            assert await refused_status(srv, path="/elsewhere") == 404
            assert srv.events("ident.bad_id")[0]["http_status"] == 404
    asyncio.run(go())


def test_unknown_charger_is_404(tmp_path):
    async def go():
        async with running(tmp_path, FakeRegistry()) as srv:
            assert await refused_status(srv) == 404
            assert srv.events("ident.unknown")[0]["hier_id"] == CHARGER
    asyncio.run(go())


def test_quarantined_charger_is_403(tmp_path):
    async def go():
        node = make_node(effective_state="quarantined", state="active")
        async with running(tmp_path, FakeRegistry(node)) as srv:
            assert await refused_status(srv) == 403
            assert srv.events("ident.quarantined")
    asyncio.run(go())


def test_registry_down_is_503_and_logged(tmp_path):
    async def go():
        registry = FakeRegistry(make_node())
        registry.down = True
        async with running(tmp_path, registry) as srv:
            assert await refused_status(srv) == 503
    asyncio.run(go())


def test_registry_down_fail_open_accepts(tmp_path):
    async def go():
        registry = FakeRegistry(make_node())
        registry.down = True
        async with running(tmp_path, registry, CSMS_REGISTRY_FAIL_OPEN="true") as srv:
            async with connected(srv):
                assert srv.events("conn.open")
    asyncio.run(go())


def test_untrusted_peer_is_403(tmp_path):
    async def go():
        async with running(tmp_path, CSMS_ALLOW_DIRECT="false") as srv:
            assert await refused_status(srv) == 403
            assert srv.events("ident.untrusted_peer")
    asyncio.run(go())


def test_spoofed_header_is_logged_and_ignored(tmp_path):
    async def go():
        async with running(tmp_path) as srv:      # direct allowed, peer not an edge
            async with connected(srv, headers={"X-Real-IP": "6.6.6.6"}):
                assert srv.events("ident.spoofed_header")[0]["reason"] == "X-Real-IP"
                assert srv.events("conn.open")[0]["srcip"] == "127.0.0.1"
    asyncio.run(go())


def test_edge_city_mismatch(tmp_path):
    async def go():
        async with running(tmp_path, **EDGE) as srv:
            assert await refused_status(srv, headers=edge_headers(edge="pt.aveiro.edge")) == 403
            assert srv.events("ident.city_mismatch")
    asyncio.run(go())


def test_no_cert_in_tls_city(tmp_path):
    async def go():
        async with running(tmp_path, **EDGE) as srv:
            assert await refused_status(srv, headers=edge_headers(verify="NONE")) == 403
            assert srv.events("ident.no_cert")
    asyncio.run(go())


def test_cert_cn_mismatch(tmp_path):
    async def go():
        async with running(tmp_path, **EDGE) as srv:
            headers = edge_headers(cn="pt.porto.st-003.cp-1")
            assert await refused_status(srv, headers=headers) == 403
            assert srv.events("ident.cert_mismatch")[0]["cert_cn"] == "pt.porto.st-003.cp-1"
    asyncio.run(go())


def test_cert_serial_mismatch(tmp_path):
    async def go():
        registry = FakeRegistry(make_node(cert_serial="AABB"))
        async with running(tmp_path, registry, **EDGE) as srv:
            assert await refused_status(srv, headers=edge_headers(serial="1A2B3C")) == 403
            assert srv.events("ident.cert_serial_mismatch")[0]["expected"] == "AABB"
    asyncio.run(go())


def test_good_certificate_is_accepted(tmp_path):
    async def go():
        registry = FakeRegistry(make_node(cert_serial="1A2B3C"))
        async with running(tmp_path, registry, **EDGE) as srv:
            async with connected(srv, headers=edge_headers()):
                opened = srv.events("conn.open")[0]
                assert opened["srcip"] == "10.31.0.13" and opened["tls"] is True
                assert opened["edge"] == "pt.porto.edge" and opened["cert_cn"] == CHARGER
    asyncio.run(go())


def test_plain_city_without_cert_is_accepted(tmp_path):
    async def go():
        hid = "pt.aveiro.st-001.cp-1"
        async with running(tmp_path, FakeRegistry(make_node(hid)), **EDGE) as srv:
            headers = edge_headers(hid, edge="pt.aveiro.edge", verify="NONE")
            async with connected(srv, hid, headers=headers):
                assert srv.events("conn.open")[0]["tls"] is False
    asyncio.run(go())


def test_station_ip_mismatch_is_logged_or_enforced(tmp_path):
    async def go():
        station = make_node("pt.porto.st-003", ip="10.31.0.99")
        registry = FakeRegistry(make_node(), station)
        async with running(tmp_path, registry, **EDGE) as srv:
            async with connected(srv, headers=edge_headers()):
                assert srv.events("ident.ip_mismatch")[0]["expected"] == "10.31.0.99"
        async with running(tmp_path, registry, CSMS_ENFORCE_STATION_IP="true", **EDGE) as srv:
            assert await refused_status(srv, headers=edge_headers()) == 403
    asyncio.run(go())


def test_duplicate_id_alive_is_409(tmp_path):
    async def go():
        async with running(tmp_path) as srv:
            async with connected(srv):
                assert await refused_status(srv) == 409
                assert srv.events("charger.duplicate_id")[0]["other_srcip"] == "127.0.0.1"
                metrics = (await srv.api.get("/metrics")).text
                assert 'evcharge_csms_duplicate_id_total{city="porto",outcome="rejected"} 1.0' in metrics
    asyncio.run(go())


def test_duplicate_id_dead_is_replaced(tmp_path):
    async def go():
        async with running(tmp_path, probe_timeout_s=0.5) as srv:
            async with connected(srv) as old:
                # the old client stops reading, so it never answers the server's ping
                old._connection.transport.pause_reading()
                async with connected(srv, boot=False):
                    assert srv.events("conn.replaced")[0]["hier_id"] == CHARGER
                    rows = (await srv.api.get("/connections")).json()
                    assert rows["count"] == 1
    asyncio.run(go())


def test_serial_is_compared_normalised(tmp_path):
    from evcsms.identity import normalise_serial
    assert normalise_serial("00A1B2") == normalise_serial("A1:B2") == normalise_serial("a1b2") == "A1B2"

    async def go():
        registry = FakeRegistry(make_node(cert_serial="00A1B2"))
        async with running(tmp_path, registry, **EDGE) as srv:
            async with connected(srv, headers=edge_headers(serial="A1:B2")):
                pass
    asyncio.run(go())
