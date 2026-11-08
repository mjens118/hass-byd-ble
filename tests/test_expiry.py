"""Key expiry: parsing the server's date, the sensor, and the Repairs warnings."""
from datetime import timedelta

import pytest
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.byd_ble import EXPIRY_CHECK_INTERVAL
from custom_components.byd_ble.const import DOMAIN
from custom_components.byd_ble.expiry import parse_valid_to

from .helpers import ADDRESS, FakeClient, setup_entry


@pytest.mark.parametrize(
    "value,expected",
    [
        (4102415999000, 4102415999),  # epoch milliseconds (what the provisioning tool saves)
        (4102415999, 4102415999),  # epoch seconds
        ("4102415999000", 4102415999),  # either, as a string
        ("2100-01-01T00:00:00+00:00", 4102444800),
        ("2100-01-01T00:00:00", 4102444800),  # no zone: taken as UTC
        (None, None),
        ("", None),
        ("soon", None),
        (True, None),
        (-5, None),  # 1970
        (1e30, None),  # absurd
        ({"x": 1}, None),
    ],
)
def test_parse_valid_to(value, expected):
    assert parse_valid_to(value) == expected


def _in(days):
    return int((dt_util.utcnow() + timedelta(days=days)).timestamp())


def issue(hass, entry, kind):
    return ir.async_get(hass).async_get_issue(DOMAIN, f"{kind}_{entry.entry_id}")


async def test_sensor_shows_the_expiry_when_known(hass):
    when = _in(100)
    await setup_entry(hass, FakeClient(), data={"key_valid_to": when})
    sensor_id = er.async_get(hass).async_get_entity_id("sensor", DOMAIN, f"{ADDRESS}_key_expires")
    state = hass.states.get(sensor_id)
    assert state is not None and dt_util.parse_datetime(state.state).timestamp() == pytest.approx(when, abs=1)


async def test_no_sensor_when_the_expiry_is_unknown(hass):
    await setup_entry(hass, FakeClient())
    assert er.async_get(hass).async_get_entity_id("sensor", DOMAIN, f"{ADDRESS}_key_expires") is None


async def test_no_warning_for_a_key_that_is_good_for_months(hass):
    entry = await setup_entry(hass, FakeClient(), data={"key_valid_to": _in(100)})
    assert issue(hass, entry, "key_expiring") is None and issue(hass, entry, "key_expired") is None


async def test_warning_inside_the_warning_window(hass):
    entry = await setup_entry(hass, FakeClient(), data={"key_valid_to": _in(10)})
    found = issue(hass, entry, "key_expiring")
    assert found is not None and found.severity is ir.IssueSeverity.WARNING and not found.is_fixable
    assert issue(hass, entry, "key_expired") is None


async def test_error_once_expired(hass):
    entry = await setup_entry(hass, FakeClient(), data={"key_valid_to": _in(-1)})
    found = issue(hass, entry, "key_expired")
    assert found is not None and found.severity is ir.IssueSeverity.ERROR
    assert issue(hass, entry, "key_expiring") is None


async def test_warning_appears_on_a_long_running_instance(hass, freezer):
    """No restart needed: the periodic check notices the key getting close."""
    entry = await setup_entry(hass, FakeClient(), data={"key_valid_to": _in(20)})
    assert issue(hass, entry, "key_expiring") is None
    freezer.tick(timedelta(days=7))
    async_fire_time_changed(hass, dt_util.utcnow() + EXPIRY_CHECK_INTERVAL)
    await hass.async_block_till_done()
    assert issue(hass, entry, "key_expiring") is not None
    freezer.tick(timedelta(days=14))
    async_fire_time_changed(hass, dt_util.utcnow() + EXPIRY_CHECK_INTERVAL)
    await hass.async_block_till_done()
    assert issue(hass, entry, "key_expired") is not None and issue(hass, entry, "key_expiring") is None


async def test_replacing_the_key_clears_the_warning(hass):
    entry = await setup_entry(hass, FakeClient(), data={"key_valid_to": _in(3)})
    assert issue(hass, entry, "key_expiring") is not None
    hass.config_entries.async_update_entry(entry, data={**entry.data, "key_valid_to": _in(365)})
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert issue(hass, entry, "key_expiring") is None


async def test_removing_the_entry_clears_its_issues(hass):
    entry = await setup_entry(hass, FakeClient(), data={"key_valid_to": _in(3)})
    assert issue(hass, entry, "key_expiring") is not None
    await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()
    assert issue(hass, entry, "key_expiring") is None
