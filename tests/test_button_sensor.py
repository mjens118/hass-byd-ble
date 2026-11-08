import pytest
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er

from custom_components.byd_ble.client import CarNotInRange
from custom_components.byd_ble.const import DOMAIN

from .helpers import ADDRESS, FakeClient, setup_entry


def eid(hass, domain, suffix):
    return er.async_get(hass).async_get_entity_id(domain, DOMAIN, f"{ADDRESS}_{suffix}")


async def test_flash_enabled_horn_and_boot_disabled_by_default(hass):
    await setup_entry(hass, FakeClient())
    reg = er.async_get(hass)
    assert hass.states.get(eid(hass, "button", "flash")) is not None
    for suffix in ("horn", "boot"):
        entry = reg.async_get(eid(hass, "button", suffix))
        assert entry.disabled_by is er.RegistryEntryDisabler.INTEGRATION


async def test_flash_press_sends_one_command(hass):
    fake = FakeClient()
    fake.results = [{"status": "ok", "door_state": 0xFF, "result": 1}]
    await setup_entry(hass, fake)
    await hass.services.async_call("button", "press", {"entity_id": eid(hass, "button", "flash")}, blocking=True)
    assert fake.sent == ["flash"]


async def test_button_errors_surface(hass):
    fake = FakeClient()
    fake.exc = CarNotInRange("car not seen")
    await setup_entry(hass, fake)
    with pytest.raises(HomeAssistantError, match="over Bluetooth"):
        await hass.services.async_call("button", "press", {"entity_id": eid(hass, "button", "flash")}, blocking=True)


async def test_in_range_sensor_reflects_client(hass):
    await setup_entry(hass, FakeClient(in_range=True))
    assert hass.states.get(eid(hass, "binary_sensor", "in_range")).state == "on"


async def test_button_press_with_a_refused_key_starts_reauth(hass):
    from custom_components.byd_ble.client import KeyRejected

    fake = FakeClient()
    fake.exc = KeyRejected("no")
    await setup_entry(hass, fake)
    with pytest.raises(HomeAssistantError):
        await hass.services.async_call("button", "press", {"entity_id": eid(hass, "button", "flash")}, blocking=True)
    await hass.async_block_till_done()
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert [f["context"]["source"] for f in flows] == ["reauth"]


async def test_each_button_sends_its_own_command(hass):
    from homeassistant.helpers import entity_registry as er_

    fake = FakeClient()
    fake.results = [{"status": "ok", "door_state": 0xFF, "result": 1}] * 3
    await setup_entry(hass, fake)
    reg = er_.async_get(hass)
    for suffix in ("horn", "boot"):  # disabled by default: enable, then reload so they exist
        reg.async_update_entity(eid(hass, "button", suffix), disabled_by=None)
    entry = hass.config_entries.async_entries(DOMAIN)[0]
    from unittest.mock import patch

    with patch("custom_components.byd_ble.BydBleClient", return_value=fake):
        await hass.config_entries.async_reload(entry.entry_id)
        await hass.async_block_till_done()
    for suffix in ("flash", "horn", "boot"):
        await hass.services.async_call("button", "press", {"entity_id": eid(hass, "button", suffix)}, blocking=True)
    assert fake.sent == ["flash", "horn", "boot"]
