"""A simulated BYD (vehicle side of the protocol) and a fake bleak client wired to it."""
import hashlib
import os

from bleak.exc import BleakError
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from custom_components.byd_ble import codec as c

DKEY = "00112233445566778899AABBCCDDEEFF"


def notif(prefix: bytes, header=b"\xa5\x5a") -> bytes:
    p = prefix + b"\xff" * (15 - len(prefix))
    f = bytearray(header + p[:15] + b"\x00\xf5\xfa")
    f[17] = c.crc8(bytes(f), 0, 16)
    return bytes(f)


class FakeCar:
    def __init__(self, key_state=1, auth_ok=True, ack=True, silent=False, drop_on_control=False):
        self.key_state, self.auth_ok, self.ack = key_state, auth_ok, ack
        self.silent, self.drop_on_control = silent, drop_on_control
        self.vrand = os.urandom(8)
        self.deliver = None
        self.got = []
        self.last_seq = 0
        self._aes = None

    def _send(self, data):
        if not self.silent:
            self.deliver(None, data)

    async def write(self, data: bytes):
        if data[:3] == b"\x5a\xa5\xd5":
            self.got.append("wake")
        elif data[:3] == b"\x5a\xa5\xd6":
            self.iv = self.vrand + data[3:11]
            self.got.append("random")
            self._send(notif(bytes([0x2A, 0xD6, self.key_state]) + self.vrand + b"\x01"))
        elif data[:2] == b"\x5b\xb5":
            if self._aes is None:
                d = hashlib.sha256(bytes.fromhex(DKEY) + self.iv).digest()
                self._aes, self._cmac = d[:16], d[16:]
            dec = Cipher(algorithms.AES(self._aes), modes.CBC(self.iv)).decryptor()
            plain = dec.update(data[2:18]) + dec.finalize()
            assert c.aes_cmac(self._cmac, plain[:12])[:4] == plain[12:16], "bad CMAC"
            if plain[0] == 0xD8:
                self.got.append("auth")
                self._send(notif(bytes([0x2B, 0xD8, 1 if self.auth_ok else 0])))
            elif plain[0] == 0xE5:
                self.got.append(("ctl", plain[1]))
                if self.drop_on_control:
                    raise BleakError("link dropped")
                if self.ack:
                    self._send(notif(bytes([0x24, 0xE5, plain[1], 0x01, 0xC0]), header=b"\xb5\x5b"))


class _Char:
    properties = ["write"]


class _Services:
    def get_characteristic(self, uuid):
        return _Char()


class FakeBleak:
    """Stands in for BleakClientWithServiceCache."""

    def __init__(self, car):
        self.car = car
        self.services = _Services()
        self.disconnected = False

    async def start_notify(self, uuid, cb):
        self.car.deliver = cb

    async def write_gatt_char(self, char, data, response=True):
        await self.car.write(bytes(data))

    async def disconnect(self):
        self.disconnected = True
