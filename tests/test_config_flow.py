import json
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.data_entry_flow import FlowResultType

from custom_components.byd_ble.client import AuthFailed, CannotConnect
from custom_components.byd_ble.config_flow import parse_credentials
from custom_components.byd_ble.const import DOMAIN

CREDS = {"country": "AU", "mac": "aa:bb:cc:dd:ee:ff", "dkey": "00112233445566778899AABBCCDDEEFF",
         "key_number": 0, "key_valid_to": 4102415999000, "watch_imei": "AB" * 16}


def test_parse_credentials():
    ok = parse_credentials(json.dumps(CREDS))
    assert ok == {
        "dkey": CREDS["dkey"], "key_number": 0, "mac": "AA:BB:CC:DD:EE:FF", "key_valid_to": 4102415999,
        "watch_imei": "AB" * 16, "country": "AU",
    }
    # an identity or region that is not what the provisioning tool writes is dropped, not trusted
    odd = parse_credentials(json.dumps({**CREDS, "watch_imei": "not-an-imei", "country": "ZZ"}))
    assert odd["watch_imei"] is None and odd["country"] is None
    assert parse_credentials("not json") is None
    assert parse_credentials(json.dumps({"mac": "x"})) is None
    assert parse_credentials(json.dumps({"dkey": "zz"})) is None
    assert parse_credentials(json.dumps({"dkey": "00" * 40})) is None


async def choose(hass, result, step):
    """Pick an entry from a menu step."""
    assert result["type"] is FlowResultType.MENU and step in result["menu_options"]
    return await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": step})


async def start(hass, creds=CREDS, fallback=None):
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    assert result["type"] is FlowResultType.MENU and result["menu_options"] == ["qr", "paste"]
    result = await choose(hass, result, "paste")
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "paste"
    data = {"credentials": json.dumps(creds)}
    if fallback:
        data["fallback_lock"] = fallback
    return await hass.config_entries.flow.async_configure(result["flow_id"], data)


async def test_happy_path_saves_key_and_fallback(hass):
    with patch("custom_components.byd_ble.config_flow.BydBleClient.check_auth", new=AsyncMock()), \
         patch("custom_components.byd_ble.config_flow.bluetooth.async_discovered_service_info", return_value=[]), \
         patch("custom_components.byd_ble.async_setup_entry", return_value=True):
        result = await start(hass, fallback="lock.garage_byd_sealion_8_lock")
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {
        "address": "AA:BB:CC:DD:EE:FF", "dkey": CREDS["dkey"], "key_number": 0, "key_valid_to": 4102415999,
        "watch_imei": "AB" * 16, "country": "AU",
    }
    assert result["options"] == {"fallback_lock": "lock.garage_byd_sealion_8_lock"}


@pytest.mark.parametrize("exc,err", [(CannotConnect("x"), "cannot_connect"), (AuthFailed("x"), "auth_failed")])
async def test_verification_errors_save_nothing(hass, exc, err):
    with patch("custom_components.byd_ble.config_flow.BydBleClient.check_auth", new=AsyncMock(side_effect=exc)), \
         patch("custom_components.byd_ble.config_flow.bluetooth.async_discovered_service_info", return_value=[]):
        result = await start(hass)
    assert result["type"] is FlowResultType.FORM and result["errors"] == {"base": err}
    assert hass.config_entries.async_entries(DOMAIN) == []


async def test_bad_json_and_no_mac_no_car(hass):
    r = await start(hass, creds={"nope": 1})
    assert r["errors"] == {"credentials": "invalid_credentials"}
    no_mac = {k: v for k, v in CREDS.items() if k != "mac"}
    with patch("custom_components.byd_ble.config_flow.bluetooth.async_discovered_service_info", return_value=[]):
        r = await start(hass, creds=no_mac)
    assert r["errors"] == {"base": "no_car"}


async def test_options_flow_sets_fallback(hass):
    from .helpers import FakeClient, setup_entry

    entry = await setup_entry(hass, FakeClient())
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    with patch("custom_components.byd_ble.async_setup_entry", return_value=True):
        result = await hass.config_entries.options.async_configure(result["flow_id"], {"fallback_lock": "lock.cloud"})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options == {"fallback_lock": "lock.cloud"}



# ---- discovery, reauth, reconfigure, options ----
from time import monotonic  # noqa: E402

from bleak.backends.device import BLEDevice  # noqa: E402
from bleak.backends.scanner import AdvertisementData  # noqa: E402
from homeassistant.components.bluetooth import BluetoothServiceInfoBleak  # noqa: E402
from homeassistant.config_entries import SOURCE_BLUETOOTH, ConfigEntryState  # noqa: E402

from custom_components.byd_ble.const import SERVICE_UUID  # noqa: E402

from .helpers import ADDRESS, FakeClient, setup_entry  # noqa: E402

NEW_CREDS = {**CREDS, "dkey": "FFEEDDCCBBAA99887766554433221100", "key_number": 2, "key_valid_to": 4133980799000}


def _check(side_effect=None):
    return patch("custom_components.byd_ble.config_flow.BydBleClient.check_auth", new=AsyncMock(side_effect=side_effect))


def _ble_device(address, name):
    try:
        return BLEDevice(address, name, {})
    except TypeError:  # older bleak (Home Assistant 2025.1) still requires rssi
        return BLEDevice(address, name, {}, -60)


def _discovery(address=ADDRESS, name="BYD BLE3"):
    return BluetoothServiceInfoBleak(
        name=name, address=address, rssi=-60, manufacturer_data={}, service_data={},
        service_uuids=[SERVICE_UUID], source="local", device=_ble_device(address, name),
        advertisement=AdvertisementData(
            local_name=name, manufacturer_data={}, service_data={}, service_uuids=[SERVICE_UUID],
            rssi=-60, tx_power=None, platform_data=(),
        ),
        connectable=True, time=monotonic(), tx_power=None,
    )


async def test_bluetooth_discovery_uses_the_seen_address_not_the_credentials_mac(hass):
    """The car advertises with its own address; that is what we must connect to."""
    seen = "11:22:33:44:55:66"
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_BLUETOOTH}, data=_discovery(seen)
    )
    assert result["type"] is FlowResultType.MENU and result["step_id"] == "user"
    result = await choose(hass, result, "paste")
    with _check(), patch("custom_components.byd_ble.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"credentials": json.dumps(CREDS)}  # credentials say aa:bb:cc:dd:ee:ff
        )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"]["address"] == seen


async def test_bluetooth_discovery_aborts_if_already_configured(hass):
    await setup_entry(hass, FakeClient())
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_BLUETOOTH}, data=_discovery(ADDRESS)
    )
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "already_configured"


async def test_same_car_cannot_be_added_twice_via_user_flow(hass):
    await setup_entry(hass, FakeClient())
    with _check() as check, patch(
        "custom_components.byd_ble.config_flow.bluetooth.async_discovered_service_info", return_value=[]
    ):
        result = await start(hass)  # credentials carry aa:bb:cc:dd:ee:ff, the configured car
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "already_configured"
    check.assert_not_called()  # and we did not even wake the car


async def test_key_without_an_expiry_date_is_saved_without_one(hass):
    no_expiry = {k: v for k, v in CREDS.items() if k != "key_valid_to"}
    with _check(), patch(
        "custom_components.byd_ble.config_flow.bluetooth.async_discovered_service_info", return_value=[]
    ), patch("custom_components.byd_ble.async_setup_entry", return_value=True):
        result = await start(hass, creds=no_expiry)
    assert result["type"] is FlowResultType.CREATE_ENTRY and "key_valid_to" not in result["data"]


async def _start_reauth(hass, entry):
    entry.async_start_reauth(hass)
    await hass.async_block_till_done()
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert len(flows) == 1 and flows[0]["context"]["source"] == "reauth"
    assert flows[0]["step_id"] == "reauth_confirm"
    result = await hass.config_entries.flow.async_configure(flows[0]["flow_id"], {"next_step_id": "paste"})
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "paste"
    return result["flow_id"]


async def test_reauth_replaces_the_key_after_checking_it_against_the_car(hass):
    entry = await setup_entry(hass, FakeClient(), data={"key_valid_to": 1893455999})
    flow_id = await _start_reauth(hass, entry)
    with _check() as check:
        result = await hass.config_entries.flow.async_configure(flow_id, {"credentials": json.dumps(NEW_CREDS)})
        await hass.async_block_till_done()
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "reauth_successful"
    check.assert_awaited_once()
    assert entry.data["dkey"] == NEW_CREDS["dkey"] and entry.data["key_number"] == 2
    assert entry.data["key_valid_to"] == 4133980799
    assert entry.data["address"] == ADDRESS  # the car does not change


async def test_reauth_with_a_key_the_car_refuses_keeps_the_old_key(hass):
    entry = await setup_entry(hass, FakeClient())
    flow_id = await _start_reauth(hass, entry)
    with _check(AuthFailed("no")):
        result = await hass.config_entries.flow.async_configure(flow_id, {"credentials": json.dumps(NEW_CREDS)})
    assert result["type"] is FlowResultType.FORM and result["errors"] == {"base": "auth_failed"}
    assert entry.data["dkey"] == "00112233445566778899AABBCCDDEEFF"


async def test_reauth_with_junk_shows_an_error(hass):
    entry = await setup_entry(hass, FakeClient())
    flow_id = await _start_reauth(hass, entry)
    result = await hass.config_entries.flow.async_configure(flow_id, {"credentials": "nope"})
    assert result["errors"] == {"credentials": "invalid_credentials"}


async def test_reauth_drops_the_old_expiry_when_the_new_key_has_none(hass):
    entry = await setup_entry(hass, FakeClient(), data={"key_valid_to": 1893455999})
    flow_id = await _start_reauth(hass, entry)
    new = {k: v for k, v in NEW_CREDS.items() if k != "key_valid_to"}
    with _check():
        await hass.config_entries.flow.async_configure(flow_id, {"credentials": json.dumps(new)})
        await hass.async_block_till_done()
    assert "key_valid_to" not in entry.data


async def test_reconfigure_replaces_the_key_and_reloads_once(hass):
    entry = await setup_entry(hass, FakeClient())
    result = await entry.start_reconfigure_flow(hass)
    assert result["type"] is FlowResultType.MENU and result["step_id"] == "reconfigure"
    result = await choose(hass, result, "paste")
    with _check(), patch.object(
        hass.config_entries, "async_reload", wraps=hass.config_entries.async_reload
    ) as reload:
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"credentials": json.dumps(NEW_CREDS)}
        )
        await hass.async_block_till_done()
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "reconfigure_successful"
    assert entry.data["dkey"] == NEW_CREDS["dkey"]
    assert reload.await_count == 1  # the options listener must not add a second reload
    assert entry.state is ConfigEntryState.LOADED


async def test_options_unlock_code_is_validated_stored_and_clearable(hass):
    entry = await setup_entry(hass, FakeClient())
    with patch("custom_components.byd_ble.async_setup_entry", return_value=True):
        result = await hass.config_entries.options.async_init(entry.entry_id)
        bad = await hass.config_entries.options.async_configure(result["flow_id"], {"unlock_code": "12"})
        assert bad["type"] is FlowResultType.FORM and bad["errors"] == {"unlock_code": "invalid_code"}
        bad = await hass.config_entries.options.async_configure(result["flow_id"], {"unlock_code": "12ab"})
        assert bad["errors"] == {"unlock_code": "invalid_code"}

        ok = await hass.config_entries.options.async_configure(
            result["flow_id"], {"unlock_code": " 123456 ", "fallback_lock": "lock.cloud"}
        )
        assert ok["type"] is FlowResultType.CREATE_ENTRY
        assert entry.options == {"fallback_lock": "lock.cloud", "unlock_code": "123456"}

        result = await hass.config_entries.options.async_init(entry.entry_id)
        cleared = await hass.config_entries.options.async_configure(result["flow_id"], {})
        assert cleared["type"] is FlowResultType.CREATE_ENTRY
        assert entry.options == {}
