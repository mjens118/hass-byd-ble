"""The QR setup flow, reauth and reconfigure, with BYD and the car both faked.

The provisioning client itself is covered in test_provisioning.py. Here the question is what the flow does
with each answer: when it may contact BYD at all, when it shows a fresh code, what it keeps when the car is
not answering, and that nothing is saved until the key has worked on the car.
"""
import logging
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.data_entry_flow import FlowResultType

from custom_components.byd_ble import provisioning as p
from custom_components.byd_ble.client import AuthFailed, CannotConnect
from custom_components.byd_ble.const import DOMAIN

from .helpers import ADDRESS, FakeClient, setup_entry
from .test_config_flow import _check, _discovery, choose

DKEY = "FFEEDDCCBBAA99887766554433221100"
MAC = "AA:BB:CC:DD:EE:FF"
ISSUED_IMEI = "EF" * 16
FALLBACK = "lock.garage_byd_sealion_8_lock"


class Byd:
    """Scripts what the fake provisioner does, and counts what the flow asked for."""

    def __init__(self):
        self.statuses = [p.APPROVED]  # check results in turn; the last one repeats
        self.create_exc = None
        self.check_exc = None
        self.fetch_excs = []  # raised in turn by fetch_key, then it succeeds
        self.key = p.IssuedKey(dkey=DKEY, key_number=2, mac=MAC, key_valid_to=4133980799000)
        self.qr_count = 0
        self.fetch_count = 0
        self.imeis = []
        self.countries = []


@pytest.fixture
def byd():
    script = Byd()

    class FakeProvisioner:
        def __init__(self, session, country, watch_imei=None):
            self.watch_imei = watch_imei or ISSUED_IMEI
            script.imeis.append(watch_imei)
            script.countries.append(country)

        async def async_create_qr(self):
            if script.create_exc:
                raise script.create_exc
            script.qr_count += 1
            return f"U{script.qr_count}", f"watchQRCode://U{script.qr_count}"

        async def async_check_qr(self, qr_uuid):
            if script.check_exc:
                raise script.check_exc
            return script.statuses.pop(0) if len(script.statuses) > 1 else script.statuses[0]

        async def async_fetch_key(self, qr_uuid):
            script.fetch_count += 1
            if script.fetch_excs:
                raise script.fetch_excs.pop(0)
            return script.key

    # The fake never touches the session, so do not create a real one (it starts a DNS resolver thread).
    with patch("custom_components.byd_ble.config_flow.WatchProvisioner", FakeProvisioner), patch(
        "custom_components.byd_ble.config_flow.async_get_clientsession", return_value=object()
    ), patch("custom_components.byd_ble.config_flow.APPROVAL_CHECK_DELAY", 0), patch(
        "custom_components.byd_ble.config_flow.bluetooth.async_discovered_service_info", return_value=[]
    ):
        yield script


def qr_in(result):
    """The text the form asks the frontend to draw as a QR code."""
    (selector,) = [v for k, v in result["data_schema"].schema.items() if str(k) == "qr_code"]
    return selector.config["data"]


async def to_consent(hass):
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    result = await choose(hass, result, "qr")
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "qr"
    return result


async def consent(hass, result, **extra):
    data = {"country": "au", "understood": True, **extra}
    return await hass.config_entries.flow.async_configure(result["flow_id"], data)


async def submit(hass, result):
    return await hass.config_entries.flow.async_configure(result["flow_id"], {})


async def to_scan(hass, **extra):
    result = await consent(hass, await to_consent(hass), **extra)
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "qr_scan"
    return result


# ---- new setup ----


async def test_qr_happy_path_saves_the_key_the_watch_identity_and_the_fallback(hass, byd, caplog):
    result = await to_scan(hass, fallback_lock=FALLBACK)
    assert qr_in(result) == "watchQRCode://U1"
    with _check() as check, patch("custom_components.byd_ble.async_setup_entry", return_value=True), \
            caplog.at_level(logging.DEBUG):
        result = await submit(hass, result)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    check.assert_awaited_once()  # tested on the car before saving
    assert result["data"] == {
        "address": MAC, "dkey": DKEY, "key_number": 2, "key_valid_to": 4133980799,
        "watch_imei": ISSUED_IMEI, "country": "AU",
    }
    assert result["options"] == {"fallback_lock": FALLBACK}
    assert "understood" not in result["data"]
    assert DKEY not in caplog.text


async def test_nothing_is_sent_to_byd_until_the_consent_box_is_ticked(hass, byd):
    result = await to_consent(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"country": "au", "understood": False}
    )
    assert result["type"] is FlowResultType.FORM and result["errors"] == {"understood": "must_understand"}
    assert byd.qr_count == 0 and byd.countries == []  # BYD was not contacted


async def test_not_approved_yet_keeps_the_same_code_then_succeeds(hass, byd):
    byd.statuses = [p.PENDING, p.PENDING, p.PENDING, p.PENDING, p.APPROVED]
    result = await to_scan(hass)
    result = await submit(hass, result)  # four looks, all still waiting
    assert result["type"] is FlowResultType.FORM and result["errors"] == {"base": "not_approved_yet"}
    assert qr_in(result) == "watchQRCode://U1" and byd.qr_count == 1
    with _check(), patch("custom_components.byd_ble.async_setup_entry", return_value=True):
        result = await submit(hass, result)
    assert result["type"] is FlowResultType.CREATE_ENTRY


@pytest.mark.parametrize("status", [p.REJECTED])
async def test_a_declined_or_expired_code_is_replaced_with_a_fresh_one(hass, byd, status):
    byd.statuses = [status]
    result = await to_scan(hass)
    result = await submit(hass, result)
    assert result["errors"] == {"base": "qr_expired"}
    assert qr_in(result) == "watchQRCode://U2"  # never leave a dead code on screen


async def test_a_code_left_waiting_past_its_lifetime_is_replaced(hass, byd):
    byd.statuses = [p.PENDING]
    result = await to_scan(hass)
    with patch("custom_components.byd_ble.config_flow.QR_TIMEOUT_SECONDS", -1):
        result = await submit(hass, result)
    assert result["errors"] == {"base": "qr_expired"} and qr_in(result) == "watchQRCode://U2"


@pytest.mark.parametrize(
    "exc,err",
    [(p.ServerUnreachable("down"), "cannot_reach_byd"), (p.ServerRefused("1005"), "byd_refused")],
)
async def test_byd_problems_when_asking_for_a_code_stay_on_the_consent_form(hass, byd, exc, err):
    byd.create_exc = exc
    result = await consent(hass, await to_consent(hass))
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "qr"
    assert result["errors"] == {"base": err}


@pytest.mark.parametrize(
    "exc,err",
    [(p.ServerUnreachable("down"), "cannot_reach_byd"), (p.ServerRefused("1005"), "byd_refused")],
)
async def test_byd_problems_when_checking_the_code_keep_the_code(hass, byd, exc, err):
    byd.check_exc = exc
    result = await submit(hass, await to_scan(hass))
    assert result["errors"] == {"base": err} and qr_in(result) == "watchQRCode://U1"


async def test_approved_but_no_key_gets_a_fresh_code(hass, byd):
    byd.fetch_excs = [p.NoKeyReturned("empty")]
    result = await submit(hass, await to_scan(hass))
    assert result["errors"] == {"base": "no_key"} and qr_in(result) == "watchQRCode://U2"
    assert hass.config_entries.async_entries(DOMAIN) == []


async def test_network_drop_after_approval_retries_the_same_approved_code(hass, byd):
    byd.fetch_excs = [p.ServerUnreachable("down")]
    result = await submit(hass, await to_scan(hass))
    assert result["errors"] == {"base": "cannot_reach_byd"} and qr_in(result) == "watchQRCode://U1"
    with _check(), patch("custom_components.byd_ble.async_setup_entry", return_value=True):
        result = await submit(hass, result)
    assert result["type"] is FlowResultType.CREATE_ENTRY and byd.qr_count == 1


async def test_a_car_that_is_not_answering_keeps_the_key_and_retries_without_a_new_code(hass, byd):
    result = await to_scan(hass)
    with _check(CannotConnect("asleep")) as check:
        result = await submit(hass, result)
    assert result["type"] is FlowResultType.FORM and result["step_id"] == "qr_verify"
    assert result["errors"] == {"base": "cannot_connect"}
    assert hass.config_entries.async_entries(DOMAIN) == []  # nothing saved yet
    check.assert_awaited_once()

    with _check(), patch("custom_components.byd_ble.async_setup_entry", return_value=True):
        result = await submit(hass, result)
    assert result["type"] is FlowResultType.CREATE_ENTRY and result["data"]["dkey"] == DKEY
    assert byd.qr_count == 1 and byd.fetch_count == 1  # no second code, no second key


async def test_a_key_the_car_refuses_is_not_saved(hass, byd):
    with _check(AuthFailed("no")):
        result = await submit(hass, await to_scan(hass))
    assert result["step_id"] == "qr_verify" and result["errors"] == {"base": "auth_failed"}
    assert hass.config_entries.async_entries(DOMAIN) == []


async def test_a_car_that_is_already_set_up_stops_before_contacting_byd(hass, byd):
    await setup_entry(hass, FakeClient())
    with patch(
        "custom_components.byd_ble.config_flow.bluetooth.async_discovered_service_info",
        return_value=[_discovery(ADDRESS)],
    ):
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
        result = await choose(hass, result, "qr")
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "already_configured"
    assert byd.qr_count == 0


# ---- replacing the key on an existing entry ----


async def test_reconfigure_by_qr_reuses_the_watch_identity_and_replaces_the_key(hass, byd):
    entry = await setup_entry(hass, FakeClient(), data={"watch_imei": "CD" * 16, "key_valid_to": 1893455999})
    result = await entry.start_reconfigure_flow(hass)
    result = await choose(hass, result, "qr")
    assert "fallback_lock" not in {str(k) for k in result["data_schema"].schema}  # set in options, not here
    result = await consent(hass, result)
    assert byd.imeis == ["CD" * 16]  # BYD sees the same "watch" as before
    with _check() as check:
        result = await submit(hass, result)
        await hass.async_block_till_done()
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "reconfigure_successful"
    check.assert_awaited_once()
    assert entry.data["dkey"] == DKEY and entry.data["key_number"] == 2
    assert entry.data["watch_imei"] == "CD" * 16  # the same identity, as the fake returns it unchanged
    assert entry.data["key_valid_to"] == 4133980799
    assert entry.data["address"] == ADDRESS  # the car does not change, whatever MAC BYD reports


async def test_reauth_by_qr_with_a_refused_key_keeps_the_old_key(hass, byd):
    entry = await setup_entry(hass, FakeClient())
    entry.async_start_reauth(hass)
    await hass.async_block_till_done()
    (flow,) = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    result = await hass.config_entries.flow.async_configure(flow["flow_id"], {"next_step_id": "qr"})
    result = await consent(hass, result)
    with _check(AuthFailed("no")):
        result = await submit(hass, result)
    assert result["step_id"] == "qr_verify" and result["errors"] == {"base": "auth_failed"}
    assert entry.data["dkey"] == "00112233445566778899AABBCCDDEEFF"


async def test_the_chosen_region_is_what_gets_used(hass, byd):
    await to_scan(hass, country="nz")
    assert byd.countries == ["NZ"]
