from unittest.mock import patch

import pytest
from homeassistant.core import ServiceRegistry
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er

from custom_components.byd_ble.client import AuthFailed, Busy, CannotConnect
from custom_components.byd_ble.const import DOMAIN

from .helpers import ADDRESS, FakeClient, setup_entry

CLOUD = "lock.cloud_car"


@pytest.fixture
def cloud():
    """Records (service, data) for calls aimed at the cloud lock; every other call passes through."""
    calls = []
    real = ServiceRegistry.async_call

    async def spy(self, domain, service, service_data=None, blocking=False, *a, **k):
        if service_data and service_data.get("entity_id") == CLOUD:
            calls.append((service, service_data))
            return None
        return await real(self, domain, service, service_data, blocking, *a, **k)

    with patch.object(ServiceRegistry, "async_call", spy):
        yield calls


def lock_id(hass):
    return er.async_get(hass).async_get_entity_id("lock", DOMAIN, f"{ADDRESS}_lock")


async def call(hass, service):
    await hass.services.async_call("lock", service, {"entity_id": lock_id(hass)}, blocking=True)


async def test_lock_and_unlock_over_bluetooth(hass):
    fake = FakeClient()
    fake.results = [{"status": "ok", "door_state": 0xC0, "result": 1}, {"status": "ok", "door_state": 0xFF, "result": 1}]
    await setup_entry(hass, fake)
    await call(hass, "lock")
    st = hass.states.get(lock_id(hass))
    assert st.state == "locked"
    assert st.attributes["last_route"] == "bluetooth" and st.attributes["door_state_raw"] == "0xc0"
    await call(hass, "unlock")
    assert hass.states.get(lock_id(hass)).state == "unlocked"
    assert fake.sent == ["lock", "unlock"]


async def test_out_of_range_without_fallback_fails_and_sends_nothing(hass):
    fake = FakeClient(in_range=False)
    await setup_entry(hass, fake)
    with pytest.raises(HomeAssistantError, match="out of Bluetooth range"):
        await call(hass, "lock")
    assert fake.sent == []


async def test_out_of_range_uses_cloud_fallback(hass, cloud):
    fake = FakeClient(in_range=False)
    await setup_entry(hass, fake, options={"fallback_lock": CLOUD})
    await call(hass, "lock")
    assert fake.sent == []
    assert cloud == [("lock", {"entity_id": CLOUD})]
    st = hass.states.get(lock_id(hass))
    assert st.state == "locked" and st.attributes["last_route"] == "cloud"


@pytest.mark.parametrize("exc", [CannotConnect("x"), AuthFailed("x")])
async def test_pre_send_ble_failure_uses_cloud_fallback(hass, cloud, exc):
    fake = FakeClient()
    fake.exc = exc
    await setup_entry(hass, fake, options={"fallback_lock": CLOUD})
    await call(hass, "unlock")
    assert cloud == [("unlock", {"entity_id": CLOUD})]
    assert hass.states.get(lock_id(hass)).state == "unlocked"


@pytest.mark.parametrize("status", ["unknown", "rejected"])
async def test_unconfirmed_command_is_never_rerouted_or_retried(hass, cloud, status):
    fake = FakeClient()
    fake.results = [{"status": status, "detail": "no ack"}]
    await setup_entry(hass, fake, options={"fallback_lock": CLOUD})
    with pytest.raises(HomeAssistantError, match="not retried"):
        await call(hass, "lock")
    assert fake.sent == ["lock"] and cloud == []
    assert hass.states.get(lock_id(hass)).state == "unknown"


async def test_busy_refuses_and_does_not_fall_back(hass, cloud):
    fake = FakeClient()
    fake.exc = Busy("another Bluetooth command is in progress")
    await setup_entry(hass, fake, options={"fallback_lock": CLOUD})
    with pytest.raises(HomeAssistantError, match="Not sent"):
        await call(hass, "lock")
    assert cloud == []


@pytest.mark.parametrize(
    "byte,doors",
    [(0xC1, ["front_driver"]), (0xC2, ["front_passenger"]), (0xC4, ["back_passenger"]),
     (0xC8, ["back_driver"]), (0xD0, ["boot"]), (0xCA, ["front_passenger", "back_driver"])],
)
async def test_lock_with_door_open_is_not_locked_yet(hass, byte, doors):
    fake = FakeClient()
    fake.results = [{"status": "ok", "door_state": byte, "result": 1}]
    await setup_entry(hass, fake)
    await call(hass, "lock")
    st = hass.states.get(lock_id(hass))
    assert st.state == "unlocked"
    assert st.attributes["lock_pending"] is True and st.attributes["open_doors"] == doors


@pytest.mark.parametrize("byte", [0xFF, 0x00, 0xE0, 0x12])
async def test_unrecognised_lock_byte_is_unknown(hass, byte):
    fake = FakeClient()
    fake.results = [{"status": "ok", "door_state": byte, "result": 1}]
    await setup_entry(hass, fake)
    await call(hass, "lock")
    st = hass.states.get(lock_id(hass))
    assert st.state == "unknown" and st.attributes["door_state_note"] == "unrecognised"


async def test_unlock_clears_open_door_attributes(hass):
    fake = FakeClient()
    fake.results = [{"status": "ok", "door_state": 0xC8, "result": 1}, {"status": "ok", "door_state": 0xFF, "result": 1}]
    await setup_entry(hass, fake)
    await call(hass, "lock")
    await call(hass, "unlock")
    st = hass.states.get(lock_id(hass))
    assert st.state == "unlocked" and "open_doors" not in st.attributes


# ---- cloud state mirroring ----
from datetime import timedelta  # noqa: E402


async def test_startup_takes_cloud_state(hass):
    hass.states.async_set(CLOUD, "locked")
    await setup_entry(hass, FakeClient(), options={"fallback_lock": CLOUD})
    st = hass.states.get(lock_id(hass))
    assert st.state == "locked" and "assumed_state" not in st.attributes


async def test_no_cloud_lock_keeps_assumed_state(hass):
    await setup_entry(hass, FakeClient())
    assert hass.states.get(lock_id(hass)).attributes["assumed_state"] is True


async def test_cloud_change_right_after_command_is_ignored(hass):
    fake = FakeClient()
    fake.results = [{"status": "ok", "door_state": 0xFF, "result": 1}]
    hass.states.async_set(CLOUD, "locked")
    await setup_entry(hass, fake, options={"fallback_lock": CLOUD})
    await call(hass, "unlock")
    hass.states.async_set(CLOUD, "unlocked")
    hass.states.async_set(CLOUD, "locked")  # stale reading inside the grace window
    await hass.async_block_till_done()
    assert hass.states.get(lock_id(hass)).state == "unlocked"


async def test_cloud_change_after_grace_updates_state(hass, freezer):
    fake = FakeClient()
    fake.results = [{"status": "ok", "door_state": 0xFF, "result": 1}]
    hass.states.async_set(CLOUD, "unlocked")
    await setup_entry(hass, fake, options={"fallback_lock": CLOUD})
    await call(hass, "unlock")
    assert hass.states.get(lock_id(hass)).state == "unlocked"
    freezer.tick(timedelta(seconds=121))
    hass.states.async_set(CLOUD, "locked")  # e.g. locked by watch
    await hass.async_block_till_done()
    st = hass.states.get(lock_id(hass))
    assert st.state == "locked" and st.attributes["state_source"] == "cloud"


@pytest.mark.parametrize("junk", ["unavailable", "unknown"])
async def test_cloud_unavailable_is_ignored(hass, freezer, junk):
    hass.states.async_set(CLOUD, "locked")
    await setup_entry(hass, FakeClient(), options={"fallback_lock": CLOUD})
    freezer.tick(timedelta(seconds=300))
    hass.states.async_set(CLOUD, junk)
    await hass.async_block_till_done()
    assert hass.states.get(lock_id(hass)).state == "locked"


async def test_cloud_locked_clears_pending_door_state(hass, freezer):
    fake = FakeClient()
    fake.results = [{"status": "ok", "door_state": 0xC8, "result": 1}]
    hass.states.async_set(CLOUD, "unlocked")
    await setup_entry(hass, fake, options={"fallback_lock": CLOUD})
    await call(hass, "lock")
    st = hass.states.get(lock_id(hass))
    assert st.state == "unlocked" and st.attributes["open_doors"] == ["back_driver"]
    freezer.tick(timedelta(seconds=121))
    hass.states.async_set(CLOUD, "locked")  # door closed, car locked itself
    await hass.async_block_till_done()
    st = hass.states.get(lock_id(hass))
    assert st.state == "locked" and "open_doors" not in st.attributes and "lock_pending" not in st.attributes


# ---- optional code ----
from homeassistant.exceptions import ServiceValidationError  # noqa: E402

from custom_components.byd_ble.client import KeyRejected  # noqa: E402

OK_UNLOCK = {"status": "ok", "door_state": 0xFF, "result": 1}
OK_LOCK = {"status": "ok", "door_state": 0xC0, "result": 1}


async def call_code(hass, service, **data):
    await hass.services.async_call("lock", service, {"entity_id": lock_id(hass), **data}, blocking=True)


async def test_no_code_configured_means_no_code_needed(hass):
    fake = FakeClient()
    fake.results = [OK_UNLOCK]
    await setup_entry(hass, fake)
    assert "code_format" not in hass.states.get(lock_id(hass)).attributes or not hass.states.get(
        lock_id(hass)
    ).attributes["code_format"]
    await call(hass, "unlock")
    assert fake.sent == ["unlock"]


async def test_with_a_code_set_a_caller_that_sends_none_is_refused_and_nothing_is_sent(hass):
    """This is the voice assistant case: Assist's on/off intents pass no code."""
    fake = FakeClient()
    await setup_entry(hass, fake, options={"unlock_code": "123456"})
    assert hass.states.get(lock_id(hass)).attributes["code_format"] == r"^\d{4,12}$"
    with pytest.raises(ServiceValidationError):
        await call(hass, "unlock")
    with pytest.raises(ServiceValidationError):
        await call(hass, "lock")
    assert fake.sent == []


async def test_wrong_code_is_refused_and_nothing_is_sent(hass):
    fake = FakeClient()
    await setup_entry(hass, fake, options={"unlock_code": "123456"})
    with pytest.raises(ServiceValidationError, match="Incorrect code"):
        await call_code(hass, "unlock", code="654321")
    assert fake.sent == []


async def test_right_code_works_for_lock_and_unlock(hass):
    fake = FakeClient()
    fake.results = [OK_UNLOCK, OK_LOCK]
    await setup_entry(hass, fake, options={"unlock_code": "123456"})
    await call_code(hass, "unlock", code="123456")
    assert hass.states.get(lock_id(hass)).state == "unlocked"
    await call_code(hass, "lock", code="123456")
    assert hass.states.get(lock_id(hass)).state == "locked"
    assert fake.sent == ["unlock", "lock"]


async def test_a_wrong_code_does_not_fall_back_to_the_cloud_either(hass, cloud):
    fake = FakeClient(in_range=False)
    await setup_entry(hass, fake, options={"unlock_code": "123456", "fallback_lock": CLOUD})
    with pytest.raises(ServiceValidationError):
        await call_code(hass, "unlock", code="000000")
    assert cloud == [] and fake.sent == []


# ---- a key the car refuses ----
def reauth_flows(hass):
    return [
        f for f in hass.config_entries.flow.async_progress_by_handler(DOMAIN) if f["context"]["source"] == "reauth"
    ]


async def test_key_refused_by_the_car_starts_reauth(hass):
    fake = FakeClient()
    fake.exc = KeyRejected("car rejected the dkey (result=0)")
    await setup_entry(hass, fake)
    with pytest.raises(HomeAssistantError, match="over Bluetooth"):
        await call(hass, "unlock")
    await hass.async_block_till_done()
    assert len(reauth_flows(hass)) == 1


async def test_repeated_refusals_do_not_pile_up_reauth_flows(hass):
    fake = FakeClient()
    fake.exc = KeyRejected("no")
    await setup_entry(hass, fake)
    for _ in range(3):
        with pytest.raises(HomeAssistantError):
            await call(hass, "unlock")
        await hass.async_block_till_done()
    assert len(reauth_flows(hass)) == 1


async def test_key_refused_still_uses_the_cloud_fallback_and_asks_for_a_new_key(hass, cloud):
    fake = FakeClient()
    fake.exc = KeyRejected("no")
    await setup_entry(hass, fake, options={"fallback_lock": CLOUD})
    await call(hass, "unlock")
    await hass.async_block_till_done()
    assert cloud == [("unlock", {"entity_id": CLOUD})]
    assert len(reauth_flows(hass)) == 1


@pytest.mark.parametrize("exc", [CannotConnect("x"), AuthFailed("no response"), Busy("busy")])
async def test_transient_failures_never_start_reauth(hass, exc):
    fake = FakeClient()
    fake.exc = exc
    await setup_entry(hass, fake)
    with pytest.raises(HomeAssistantError):
        await call(hass, "unlock")
    await hass.async_block_till_done()
    assert reauth_flows(hass) == []
