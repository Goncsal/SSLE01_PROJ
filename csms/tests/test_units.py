"""Plain-function tests: settings, the meter checks, the card and trusted-proxy helpers."""

import asyncio
import datetime as dt

import pytest
from evcommon.config import ConfigError
from evcsms.billing import LocalBilling
from evcsms.identity import TrustedProxies
from evcsms.sessions import Transaction, check_reading
from evcsms.settings import Settings

T0 = dt.datetime(2026, 11, 6, 9, 0, tzinfo=dt.UTC)


def tx():
    return Transaction(1, "pt.porto.st-003.cp-2", 1, "04A1B2C3D4E5F6", 1000, T0)


def test_settings_require_a_token():
    with pytest.raises(ConfigError):
        Settings.from_env({})
    assert Settings.from_env({"CSMS_API_TOKEN": "x" * 16}).ocpp_port == 9000


def test_meter_checks():
    later = T0 + dt.timedelta(seconds=10)
    assert check_reading(tx(), 1050, 20000, later, 22, 1.1) == []
    assert [a.reason for a in check_reading(tx(), 900, 20000, later, 22, 1.1)] == ["backwards"]
    assert [a.reason for a in check_reading(tx(), 1050, 30000, later, 22, 1.1)] == ["over_power"]
    assert [a.reason for a in check_reading(tx(), 90000, 1000, later, 22, 1.1)] == ["over_energy"]
    assert check_reading(tx(), 1050, 99999, later, None, 1.1) == []     # rating unknown


def test_local_billing():
    billing = LocalBilling({"04FFEEDDCCBBAA"})
    assert asyncio.run(billing.authorize("04A1B2C3D4E5F6", "x")) == "Accepted"
    assert asyncio.run(billing.authorize("04FFEEDDCCBBAA", "x")) == "Blocked"
    assert asyncio.run(billing.authorize("not-a-card", "x")) == "Invalid"


def test_trusted_proxies_accept_ips_cidrs_and_names():
    proxies = TrustedProxies(["10.0.0.1", "192.168.5.0/24", "localhost"])
    async def check():
        return [await proxies.contains(ip) for ip in
                ("10.0.0.1", "192.168.5.77", "127.0.0.1", "::ffff:10.0.0.1", "10.0.0.2")]
    assert asyncio.run(check()) == [True, True, True, True, False]
