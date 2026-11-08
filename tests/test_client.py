"""Session logic of BydBleClient against a simulated car (no radio, no Home Assistant instance)."""
import functools

import pytest
from bleak.exc import BleakError

from custom_components.byd_ble import client as cl
from .fakecar import DKEY, FakeBleak, FakeCar


_DEVICE = object()  # stands in for a BLEDevice; any non-None value means "the car is in range"


@pytest.fixture
def patch_ble(monkeypatch):
    """Returns a function that wires a FakeCar in and gives back the FakeBleak."""
    holder = {}

    def wire(car, device=_DEVICE, connect_error=None):
        fb = FakeBleak(car)
        holder["fb"] = fb

        async def fake_establish(*a, **k):
            if connect_error:
                raise connect_error
            return fb

        monkeypatch.setattr(cl, "establish_connection", fake_establish)
        monkeypatch.setattr(cl.bluetooth, "async_ble_device_from_address", lambda *a, **k: device)
        monkeypatch.setattr(cl, "Runner", functools.partial(cl.Runner, step_timeout=0.3, control_timeout=0.3))
        monkeypatch.setattr(cl, "WAKE_TO_RANDOM_DELAY", 0.0)
        monkeypatch.setattr(cl, "RANDOM_TO_AUTH_DELAY", 0.0)
        return holder

    return wire


def make_client():
    return cl.BydBleClient(None, "AA:BB:CC:DD:EE:FF", DKEY, 3, "test")


async def test_lock_ok_and_disconnects(patch_ble):
    car = FakeCar()
    holder = patch_ble(car)
    res = await make_client().send("lock")
    assert res == {"status": "ok", "door_state": 0xC0, "result": 1}
    assert car.got == ["wake", "random", "auth", ("ctl", 0x07)]
    assert holder["fb"].disconnected


async def test_check_auth_sends_no_control_frame(patch_ble):
    car = FakeCar()
    patch_ble(car)
    await make_client().check_auth()
    assert car.got == ["wake", "random", "auth"]


async def test_not_in_range_sends_nothing(patch_ble):
    car = FakeCar()
    patch_ble(car, device=None)
    with pytest.raises(cl.CarNotInRange):
        await make_client().send("lock")
    assert car.got == []


async def test_connect_failure_is_pre_send(patch_ble):
    patch_ble(FakeCar(), connect_error=BleakError("nope"))
    with pytest.raises(cl.CannotConnect):
        await make_client().send("unlock")


async def test_auth_failures_are_pre_send(patch_ble):
    for car, text in [(FakeCar(key_state=0), "not active"), (FakeCar(auth_ok=False), "rejected"),
                      (FakeCar(silent=True), "no random")]:
        patch_ble(car)
        with pytest.raises(cl.AuthFailed, match=text):
            await make_client().send("lock")
        assert not any(isinstance(g, tuple) for g in car.got)


async def test_no_ack_is_unknown_and_sent_once(patch_ble):
    car = FakeCar(ack=False)
    patch_ble(car)
    res = await make_client().send("unlock")
    assert res["status"] == "unknown"
    assert sum(isinstance(g, tuple) for g in car.got) == 1


async def test_link_drop_after_send_is_unknown_not_an_error(patch_ble):
    car = FakeCar(drop_on_control=True)
    patch_ble(car)
    res = await make_client().send("lock")
    assert res["status"] == "unknown" and "link failed" in res["detail"]


async def test_busy_is_refused_not_queued(patch_ble):
    car = FakeCar()
    patch_ble(car)
    client = make_client()
    await client._lock.acquire()
    with pytest.raises(cl.Busy):
        await client.send("lock")
    client._lock.release()


async def test_unknown_command_name_is_a_keyerror_not_a_send(patch_ble):
    car = FakeCar()
    patch_ble(car)
    with pytest.raises(KeyError):
        await make_client().send("self_destruct")


async def test_explicit_key_refusal_is_distinguished_from_silence(patch_ble):
    """Only an answer from the car means "get a new key". Silence is just a bad moment."""
    for car, refused in [(FakeCar(key_state=0), True), (FakeCar(auth_ok=False), True), (FakeCar(silent=True), False)]:
        patch_ble(car)
        with pytest.raises(cl.AuthFailed) as info:
            await make_client().send("lock")
        assert isinstance(info.value, cl.KeyRejected) is refused


async def test_missing_characteristic_is_a_pre_send_connection_error(patch_ble, monkeypatch):
    car = FakeCar()
    holder = patch_ble(car)
    monkeypatch.setattr(holder["fb"].services, "get_characteristic", lambda uuid: None)
    with pytest.raises(cl.CannotConnect, match="characteristic"):
        await make_client().send("lock")
    assert car.got == [] and holder["fb"].disconnected
