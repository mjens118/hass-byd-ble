"""Setup, unload, reload behaviour and diagnostics."""
from unittest.mock import patch

from homeassistant.config_entries import ConfigEntryState
from custom_components.byd_ble.const import DOMAIN
from custom_components.byd_ble.diagnostics import async_get_config_entry_diagnostics

from .helpers import ADDRESS, FakeClient, setup_entry

DKEY = "00112233445566778899AABBCCDDEEFF"


async def test_unload_and_reload(hass):
    entry = await setup_entry(hass, FakeClient())
    assert entry.state is ConfigEntryState.LOADED
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert entry.state is ConfigEntryState.NOT_LOADED


async def test_corrupt_stored_key_asks_for_a_new_one_instead_of_crashing(hass):
    entry = await setup_entry(hass, FakeClient(), data={"dkey": "not-hex"}, expect_loaded=False)
    assert entry.state is ConfigEntryState.SETUP_ERROR
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert [f["context"]["source"] for f in flows] == ["reauth"]


async def test_changing_options_reloads_but_other_updates_do_not(hass):
    entry = await setup_entry(hass, FakeClient())
    with patch.object(hass.config_entries, "async_reload", wraps=hass.config_entries.async_reload) as reload:
        hass.config_entries.async_update_entry(entry, title="Renamed")  # not an options change
        await hass.async_block_till_done()
        assert reload.await_count == 0
        # Changed through the options flow, the way a person does it (newer Home Assistant reloads from the flow).
        result = await hass.config_entries.options.async_init(entry.entry_id)
        await hass.config_entries.options.async_configure(result["flow_id"], {"fallback_lock": "lock.cloud"})
        await hass.async_block_till_done()
        assert reload.await_count == 1


async def test_no_update_listener_where_the_options_flow_reloads(hass):
    """Home Assistant 2026.12 stops allowing an update listener alongside reload-and-abort."""
    from homeassistant import config_entries

    entry = await setup_entry(hass, FakeClient())
    has_reloading_flow = hasattr(config_entries, "OptionsFlowWithReload")
    assert bool(entry.update_listeners) is (not has_reloading_flow)


async def test_diagnostics_never_contain_the_key_the_code_or_the_address(hass):
    fake = FakeClient()
    entry = await setup_entry(
        hass,
        fake,
        options={"unlock_code": "246810", "fallback_lock": "lock.cloud"},
        data={"key_valid_to": 4102415999, "watch_imei": "AB" * 16, "country": "AU"},
    )
    diag = await async_get_config_entry_diagnostics(hass, entry)
    text = str(diag)
    assert DKEY not in text and "246810" not in text and ADDRESS not in text
    assert "AB" * 16 not in text and diag["entry"]["data"]["watch_imei"] == "**REDACTED**"
    assert diag["entry"]["data"]["country"] == "AU"  # useful in a report and not secret
    assert diag["entry"]["data"]["dkey"] == "**REDACTED**"
    assert diag["entry"]["options"]["unlock_code"] == "**REDACTED**"
    assert diag["entry"]["options"]["fallback_lock"] == "lock.cloud"  # useful and not secret
    assert diag["entry"]["data"]["key_valid_to"] == 4102415999
    assert diag["in_range"] is True
    assert diag["lock"]["state"] in ("unknown", "locked", "unlocked")


async def test_diagnostics_hide_the_address_wherever_home_assistant_copies_it(hass):
    """Regression: a real download leaked the address through discovery_keys and manufacturer IDs.

    The car advertises the first two bytes of its own address as the manufacturer "company ID", read
    little-endian, so AA:BB:CC:DD:EE:FF would show up as 0xBBAA.
    """
    from types import SimpleNamespace

    from homeassistant.helpers.discovery_flow import DiscoveryKey

    company_id = 0xBBAA
    advertisement = SimpleNamespace(
        name="BYD BLE3",
        rssi=-60,
        service_uuids=["42594420-4155-544f-e0a9-e50e24dcca9e"],
        manufacturer_data={company_id: b"\x01\x02\x03"},
    )
    entry = await setup_entry(
        hass,
        FakeClient(),
        discovery_keys={"bluetooth": (DiscoveryKey(domain="bluetooth", key=ADDRESS, version=1),)},
    )
    with patch("custom_components.byd_ble.diagnostics.bluetooth.async_last_service_info", return_value=advertisement):
        diag = await async_get_config_entry_diagnostics(hass, entry)

    text = str(diag)
    assert ADDRESS not in text and ADDRESS.lower() not in text
    assert str(company_id) not in text and hex(company_id) not in text
    assert "discovery_keys" not in text or "**REDACTED**" in str(diag["entry"]["discovery_keys"])
    assert diag["advertisement"] == {
        "name": "BYD BLE3",
        "rssi": -60,
        "service_uuids": ["42594420-4155-544f-e0a9-e50e24dcca9e"],
        "has_manufacturer_data": True,
    }


async def test_every_translation_key_the_code_uses_exists_in_en_json():
    import json
    from pathlib import Path

    root = Path(__file__).parent.parent / "custom_components" / "byd_ble"
    en = json.loads((root / "translations" / "en.json").read_text())
    icons = json.loads((root / "icons.json").read_text())

    from custom_components.byd_ble.button import BUTTONS

    for button in BUTTONS:
        assert button.translation_key in en["entity"]["button"], button.key
        assert button.translation_key in icons["entity"]["button"], button.key
    assert "in_range" in en["entity"]["binary_sensor"]
    assert "key_expires" in en["entity"]["sensor"]
    for key in ("key_expiring", "key_expired"):
        assert {"title", "description"} <= en["issues"][key].keys()
    steps = en["config"]["step"]
    for step in ("user", "reauth_confirm", "reconfigure"):  # menus: QR code or paste
        assert set(steps[step]["menu_options"]) == {"qr", "paste"}
    assert "credentials" in steps["paste"]["data"]
    assert {"country", "understood"} <= steps["qr"]["data"].keys()
    assert "qr_code" in steps["qr_scan"]["data"] and "qr_verify" in steps
    # the consent text has to say the uncomfortable parts out loud
    consent = steps["qr"]["description"]
    for phrase in ("Samsung smartwatch", "terms of use", "may replace it", "Authorisation details"):
        assert phrase in consent, phrase
    for err in (
        "invalid_credentials", "no_car", "cannot_connect", "auth_failed", "must_understand", "cannot_reach_byd",
        "byd_refused", "not_approved_yet", "qr_expired", "no_key",
    ):
        assert err in en["config"]["error"]
    from custom_components.byd_ble.provisioning import SERVERS

    assert set(en["selector"]["country"]["options"]) == {c.lower() for c in SERVERS}
    for reason in ("already_configured", "reauth_successful", "reconfigure_successful"):
        assert reason in en["config"]["abort"]
