import pytest

from evcommon import hierid


@pytest.mark.parametrize("value,kind", [
    ("pt.porto", "city"),
    ("pt.porto.st-003", "station"),
    ("pt.porto.st-003.cp-2", "charger"),
    ("pt.lisboa.edge", "edge"),
    ("pt.central.k3s-master", "central"),
    ("pt.vila-real.st-120.cp-12", "charger"),
])
def test_parse_valid(value, kind):
    hid = hierid.parse(value)
    assert hid.kind == kind
    assert str(hid) == value
    assert hierid.HIER_ID_PATTERN.match(value)


@pytest.mark.parametrize("value", [
    "", "pt", "pt.central", "es.porto", "pt.Porto", "pt.porto.st-3", "pt.porto.st-000",
    "pt.porto.st-003.cp-0", "pt.porto.st-003.cp-100", "pt.porto.st-003.cp-1.x",
    "pt.central.k3s_master", "pt.edge", "pt.porto.edge.cp-1", "pt..st-001", "pt.porto-.st-001",
])
def test_parse_invalid(value):
    with pytest.raises(ValueError):
        hierid.parse(value)
    assert not hierid.is_valid(value)


def test_prefixes():
    assert hierid.is_prefix("pt")
    assert hierid.is_prefix("pt.central")
    assert hierid.is_prefix("pt.porto.st-003")
    assert not hierid.is_prefix("pt.porto.st")
    assert not hierid.is_prefix("pt.porto.")
    assert hierid.is_under("pt.porto.st-003.cp-2", "pt.porto")
    assert hierid.is_under("pt.porto", "pt.porto")
    assert not hierid.is_under("pt.portoalegre.st-001", "pt.porto")


def test_relations_and_labels():
    hid = hierid.parse("pt.porto.st-003.cp-2")
    assert str(hid.parent) == "pt.porto.st-003"
    assert str(hid.station_id) == "pt.porto.st-003"
    assert str(hid.edge_id) == "pt.porto.edge"
    assert hid.ancestry() == ["pt.porto", "pt.porto.st-003", "pt.porto.st-003.cp-2"]
    assert hid.labels() == {"hier_id": "pt.porto.st-003.cp-2", "city": "porto",
                            "station": "st-003", "charger": "cp-2"}
    assert hierid.parse("pt.central.wazuh").labels() == {"hier_id": "pt.central.wazuh"}
    assert hid.cn == hid.wazuh_agent_name == "pt.porto.st-003.cp-2"


@pytest.mark.parametrize("value,dns,instance", [
    ("pt.porto", "porto.pt.lab", None),
    ("pt.porto.st-003", "st-003.porto.pt.lab", "porto-st-003"),
    ("pt.porto.st-003.cp-2", "cp-2.st-003.porto.pt.lab", "porto-st-003"),
    ("pt.aveiro.edge", "edge.aveiro.pt.lab", "aveiro-edge"),
    ("pt.central.k3s-master", "k3s-master.incus", "k3s-master"),
])
def test_dns_and_incus(value, dns, instance):
    hid = hierid.parse(value)
    assert hid.dns_name() == dns
    assert hid.incus_instance() == instance
    assert str(hierid.from_dns(dns)) == value
    assert str(hierid.from_dns(dns.upper() + ".")) == value


def test_builders():
    assert hierid.charger("porto", "st-003", "cp-2") == "pt.porto.st-003.cp-2"
    assert hierid.station("lisboa", "st-001") == "pt.lisboa.st-001"
    assert hierid.edge("aveiro") == "pt.aveiro.edge"
    assert hierid.central("csms") == "pt.central.csms"
    with pytest.raises(ValueError):
        hierid.city("central")


def test_path_id():
    assert str(hierid.path_id("/ocpp/pt.porto.st-003.cp-2")) == "pt.porto.st-003.cp-2"
    assert str(hierid.path_id("/ocpp/pt.porto.st-003.cp-2/")) == "pt.porto.st-003.cp-2"
    for bad in ("/ocpp/pt.porto.st-003", "/x/pt.porto.st-003.cp-2", "/ocpp/../etc"):
        with pytest.raises(ValueError):
            hierid.path_id(bad)


@pytest.mark.parametrize("dn,cn", [
    ("CN=pt.porto.st-003.cp-2,OU=porto,O=EVCharge Lab", "pt.porto.st-003.cp-2"),
    ("O=EVCharge Lab,OU=porto,CN=pt.porto.st-003.cp-2", "pt.porto.st-003.cp-2"),
    ("/O=EVCharge Lab/OU=porto/CN=pt.porto.st-003.cp-2", "pt.porto.st-003.cp-2"),
    ("O=Lab\\, Inc,CN=pt.porto.st-001.cp-1", "pt.porto.st-001.cp-1"),
    ("O=EVCharge Lab", None),
    ("", None),
    (None, None),
])
def test_cn_from_dn(dn, cn):
    assert hierid.cn_from_dn(dn) == cn
