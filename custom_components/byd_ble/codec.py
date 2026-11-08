"""Pure-Python port of the BYD dkey BLE frame codec.

Ported from PoorGrammerA/BydBleAutoLock (BydBleCodec.java, BydBleCrypto.java),
Copyright (c) 2026 BLE AutoLock contributors, MIT License. See THIRD_PARTY_NOTICES.md.
No Bluetooth or network access in this module, so it can be unit tested offline.

Verified upstream only on a Korean ATTO 3. Treat everything else as experimental.
"""
from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, field

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.cmac import CMAC

SERVICE_UUID = "42594420-4155-544F-E0A9-E50E24DCCA9E"
WRITE_UUID = "42590002-4155-544F-E0A9-E50E24DCCA9E"
NOTIFY_UUID = "42590003-4155-544F-E0A9-E50E24DCCA9E"

RESP_CONTROL = 0x24
RESP_RANDOM = 0x2A
RESP_AUTH = 0x2B
CONTROL_TYPE = 0xE5

# functionId -> control code (only commands the upstream project marks verified)
VERIFIED_COMMANDS = {
    "unlock": 0x05,
    "lock": 0x07,
    "open_boot": 0x06,
    "flash_lights": 0x16,
    "flash_and_horn": 0x0A,
}


def crc8(data: bytes, start: int, end_inclusive: int) -> int:
    crc = 0
    for b in data[start : end_inclusive + 1]:
        crc ^= b
        for _ in range(8):
            crc = ((crc << 1) ^ 0x07) & 0xFF if crc & 0x80 else (crc << 1) & 0xFF
    return crc


def aes_cmac(key: bytes, data: bytes) -> bytes:
    c = CMAC(algorithms.AES(key))
    c.update(data)
    return c.finalize()


def aes_cbc_encrypt_nopad(data: bytes, key: bytes, iv: bytes) -> bytes:
    enc = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
    return enc.update(data) + enc.finalize()


def wake_frame() -> bytes:
    return bytes([0x5A, 0xA5, 0xD5, 0x00, 0x00, 0xF5, 0xFA])


def _finish_crc_frame(frame: bytearray) -> bytes:
    frame[17] = crc8(bytes(frame), 0, 16)
    frame[18] = 0xF5
    frame[19] = 0xFA
    return bytes(frame)


def split_response_frame(frame: bytes) -> bytes | None:
    """Validate a 20-byte notification frame and return its 16-byte payload."""
    if len(frame) != 20:
        return None
    if frame[:2] not in (b"\xa5\x5a", b"\xb5\x5b"):
        return None
    if frame[18:20] != b"\xf5\xfa":
        return None
    if crc8(frame, 0, 16) != frame[17]:
        return None
    return bytes(frame[2:18])


@dataclass
class Session:
    """One BLE session: wake -> random exchange -> auth -> control frames."""

    dkey_hex: str
    key_number: int
    app_random: bytes = field(default_factory=lambda: os.urandom(8))
    session_iv: bytes = b"\x00" * 16
    aes_key: bytes = b"\x00" * 16
    cmac_key: bytes = b"\x00" * 16
    seq: int = 0

    def __post_init__(self) -> None:
        d = self.dkey_hex.strip()
        if len(d) % 2 or len(d) > 64:
            raise ValueError("dkey must be an even number of hex characters (max 64)")
        self._dkey = bytes.fromhex(d)
        if not 0 <= self.key_number <= 255:
            raise ValueError("key_number out of range")

    def random_exchange_frame(self) -> bytes:
        if len(self.app_random) != 8:
            raise ValueError("app random must be 8 bytes")
        f = bytearray(20)
        f[0], f[1], f[2] = 0x5A, 0xA5, 0xD6
        f[3:11] = self.app_random
        f[11] = self.key_number
        f[12:17] = b"\xff" * 5
        return _finish_crc_frame(f)

    def parse_random_response(self, payload: bytes) -> dict | None:
        if len(payload) < 12 or payload[0] != RESP_RANDOM or payload[1] != 0xD6:
            return None
        vehicle_random = payload[3:11]
        self.session_iv = vehicle_random + self.app_random
        return {
            "key_state": payload[2],
            "vehicle_random": vehicle_random,
            "crc_ok": payload[11],
        }

    def _wrap(self, plain: bytes) -> bytes:
        enc = aes_cbc_encrypt_nopad(plain, self.aes_key, self.session_iv)
        return b"\x5b\xb5" + enc + b"\xf5\xfa"

    def auth_frame(self) -> bytes:
        digest = hashlib.sha256(self._dkey + self.session_iv).digest()
        self.aes_key, self.cmac_key = digest[:16], digest[16:]
        plain = bytearray(16)
        plain[0] = 0xD8
        plain[1:12] = b"\xff" * 11
        plain[12:16] = aes_cmac(self.cmac_key, bytes(plain[:12]))[:4]
        return self._wrap(bytes(plain))

    @staticmethod
    def parse_auth_result(payload: bytes) -> int:
        if len(payload) >= 3 and payload[0] == RESP_AUTH and payload[1] == 0xD8:
            return payload[2]
        return 0xFF

    def control_frame(self, control_code: int) -> bytes:
        self.seq = 1 if self.seq >= 0xFFFF else self.seq + 1
        plain = bytearray(16)
        plain[0] = CONTROL_TYPE
        plain[1] = control_code
        plain[2] = (self.seq >> 8) & 0xFF
        plain[3] = self.seq & 0xFF
        plain[4:12] = b"\xff" * 8
        if control_code == 0x03:
            plain[4] = 0x00
        plain[12:16] = aes_cmac(self.cmac_key, bytes(plain[:12]))[:4]
        return self._wrap(bytes(plain))

    @staticmethod
    def parse_control_response(payload: bytes) -> dict | None:
        if len(payload) < 5 or payload[0] != RESP_CONTROL or payload[1] != CONTROL_TYPE:
            return None
        return {
            "control_code": payload[2],
            "success": payload[3] == 0x01,
            "result": payload[3],
            "door_state": payload[4],
        }
