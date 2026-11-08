"""Bluetooth client for the BYD dkey flow, built on Home Assistant's Bluetooth stack.

Rules baked in (same as the standalone test kit):
  * connect on demand, one command per connection, then disconnect;
  * once a control frame has been written, any failure or timeout is reported as
    UNKNOWN and is never retried or re-routed automatically;
  * only one session at a time per car; a second request is refused, not queued.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from bleak import BleakClient
from bleak.exc import BleakError
from bleak_retry_connector import BleakClientWithServiceCache, establish_connection

from homeassistant.components import bluetooth
from homeassistant.core import HomeAssistant

from . import codec as c
from .const import (
    COMMANDS,
    CONTROL_TIMEOUT,
    NOTIFY_UUID,
    RANDOM_TO_AUTH_DELAY,
    SESSION_TIMEOUT,
    STEP_TIMEOUT,
    WAKE_TO_RANDOM_DELAY,
    WRITE_UUID,
)

_LOGGER = logging.getLogger(__name__)


class BydBleError(Exception):
    """Base error. Raised only when NO control frame was sent."""


class CarNotInRange(BydBleError):
    """The car is not currently seen by any Bluetooth adapter or proxy."""


class CannotConnect(BydBleError):
    """Connecting to the car failed."""


class AuthFailed(BydBleError):
    """Authentication did not complete (no response, bad frame, key inactive, or rejected)."""


class KeyRejected(AuthFailed):
    """The car answered and said the key is not usable (inactive or refused).

    Unlike a plain AuthFailed (silence, a corrupt frame), this is an explicit answer from the car, so
    it is the one case where asking the user to provision a new key makes sense.
    """


class Busy(BydBleError):
    """Another Bluetooth session is already in progress."""


class FrameAssembler:
    """Reassembles 20-byte notification frames from arbitrary notification chunks."""

    def __init__(self) -> None:
        self.buf = bytearray()

    def feed(self, chunk: bytes) -> list[bytes]:
        self.buf += chunk
        out: list[bytes] = []
        while len(self.buf) >= 20:
            if bytes(self.buf[:2]) not in (b"\xa5\x5a", b"\xb5\x5b"):
                del self.buf[0]
                continue
            payload = c.split_response_frame(bytes(self.buf[:20]))
            if payload is None:
                del self.buf[0]
                continue
            out.append(payload)
            del self.buf[:20]
        return out


class Runner:
    """Drives wake -> random exchange -> auth -> optional command over any write function."""

    def __init__(
        self,
        write: Callable[[bytes], Awaitable[None]],
        dkey: str,
        key_number: int,
        *,
        step_timeout: float = STEP_TIMEOUT,
        control_timeout: float = CONTROL_TIMEOUT,
    ) -> None:
        self._write = write
        self.session = c.Session(dkey, int(key_number))
        self.asm = FrameAssembler()
        self.q: asyncio.Queue[bytes] = asyncio.Queue()
        self.command_sent = False
        self._step_timeout = step_timeout
        self._control_timeout = control_timeout

    def on_notify(self, data: bytes) -> None:
        for payload in self.asm.feed(bytes(data)):
            self.q.put_nowait(payload)

    async def _expect(self, resp_type: int, timeout: float) -> bytes:
        async with asyncio.timeout(timeout):
            while True:
                payload = await self.q.get()
                if payload[0] == resp_type:
                    return payload

    async def authenticate(self) -> None:
        await self._write(c.wake_frame())
        await asyncio.sleep(WAKE_TO_RANDOM_DELAY)
        await self._write(self.session.random_exchange_frame())
        try:
            payload = await self._expect(c.RESP_RANDOM, self._step_timeout)
        except TimeoutError as err:
            raise AuthFailed("no random-exchange response") from err
        r = self.session.parse_random_response(payload)
        if not r or r["crc_ok"] != 1:
            raise AuthFailed("random-exchange response invalid")
        if r["key_state"] != 1:
            raise KeyRejected(f"key not active (keyState={r['key_state']})")
        await asyncio.sleep(RANDOM_TO_AUTH_DELAY)
        await self._write(self.session.auth_frame())
        try:
            payload = await self._expect(c.RESP_AUTH, self._step_timeout)
        except TimeoutError as err:
            raise AuthFailed("no authentication response") from err
        result = self.session.parse_auth_result(payload)
        if result != 1:
            raise KeyRejected(f"car rejected the dkey (result={result})")

    async def command(self, name: str) -> dict[str, Any]:
        code = COMMANDS[name]
        frame = self.session.control_frame(code)
        self.command_sent = True  # from here on, failure means UNKNOWN, never "not sent"
        await self._write(frame)
        try:
            payload = await self._expect(c.RESP_CONTROL, self._control_timeout)
        except TimeoutError:
            return {"status": "unknown", "detail": "no acknowledgement within 3 seconds"}
        r = c.Session.parse_control_response(payload)
        if not r or r["control_code"] != code:
            return {"status": "unknown", "detail": f"unexpected response {payload.hex()}"}
        return {
            "status": "ok" if r["success"] else "rejected",
            "door_state": r["door_state"],
            "result": r["result"],
        }


class BydBleClient:
    """Per-car client. Safe to call from entities."""

    def __init__(self, hass: HomeAssistant, address: str, dkey: str, key_number: int, name: str) -> None:
        self.hass = hass
        self.address = address
        self._dkey = dkey
        self._key_number = key_number
        self._name = name
        self._lock = asyncio.Lock()

    def _ble_device(self):
        return bluetooth.async_ble_device_from_address(self.hass, self.address, connectable=True)

    @property
    def in_range(self) -> bool:
        return bluetooth.async_address_present(self.hass, self.address, connectable=True)

    async def _session(self, action: str | None) -> dict[str, Any] | None:
        """Run one full session. Raises BydBleError subclasses only if nothing was sent."""
        if self._lock.locked():
            raise Busy("another Bluetooth command is in progress")
        async with self._lock:
            device = self._ble_device()
            if device is None:
                raise CarNotInRange("car not seen by any Bluetooth adapter or proxy")
            runner: Runner | None = None
            client: BleakClient | None = None
            try:
                async with asyncio.timeout(SESSION_TIMEOUT):
                    try:
                        client = await establish_connection(
                            BleakClientWithServiceCache,
                            device,
                            self._name,
                            max_attempts=3,
                            ble_device_callback=self._ble_device,
                        )
                    except (BleakError, TimeoutError, OSError) as err:
                        raise CannotConnect(str(err) or type(err).__name__) from err

                    char = client.services.get_characteristic(WRITE_UUID)
                    if char is None:
                        # Nothing has been sent yet, so this is a connection problem, not an unknown outcome.
                        raise CannotConnect("the car does not expose the expected Bluetooth characteristic")
                    with_response = "write" in char.properties

                    async def write(data: bytes) -> None:
                        await client.write_gatt_char(char, data, response=with_response)

                    runner = Runner(write, self._dkey, self._key_number)
                    await client.start_notify(NOTIFY_UUID, lambda _h, data: runner.on_notify(bytes(data)))
                    await runner.authenticate()
                    if action is None:
                        return None
                    return await runner.command(action)
            except BydBleError:
                raise
            except (BleakError, TimeoutError, OSError) as err:
                if runner is not None and runner.command_sent:
                    _LOGGER.warning("Link failed after the %s command was sent: %s", action, err)
                    return {"status": "unknown", "detail": f"link failed after sending: {err}"}
                raise CannotConnect(str(err) or type(err).__name__) from err
            finally:
                if client is not None:
                    try:
                        await client.disconnect()
                    except (BleakError, TimeoutError, OSError):
                        pass

    async def check_auth(self) -> None:
        """Wake, random exchange and dkey auth only. Sends no car command."""
        await self._session(None)

    async def send(self, action: str) -> dict[str, Any]:
        """Send exactly one command. Returns a result dict; never retries."""
        result = await self._session(action)
        assert result is not None
        return result
