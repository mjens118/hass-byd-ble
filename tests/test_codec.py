"""Known-answer tests for the frame codec.

The other tests build both the car side and the integration side with this same codec, so a mistake
in the codec itself would still pass them. These vectors come from outside it: RFC 4493 (AES-CMAC) and
the upstream project's Java unit tests (BydBleAutoLock), so they catch a codec that is self-consistent
but wrong.
"""
from custom_components.byd_ble import codec as c
from custom_components.byd_ble.client import FrameAssembler
from custom_components.byd_ble.const import COMMANDS

H = bytes.fromhex


def test_cmac_rfc4493_empty():
    key = H("2B7E151628AED2A6ABF7158809CF4F3C")
    assert c.aes_cmac(key, b"") == H("BB1D6929E95937287FA37D129B756746")


def test_cmac_rfc4493_other_lengths():
    key = H("2B7E151628AED2A6ABF7158809CF4F3C")
    assert c.aes_cmac(key, H("6BC1BEE22E409F96E93D7E117393172A")) == H("070A16B46B4D4144F79BDD9DD04A287C")
    msg40 = H("6BC1BEE22E409F96E93D7E117393172AAE2D8A571E03AC9C9EB76FAC45AF8E5130C81C46A35CE411")
    assert c.aes_cmac(key, msg40) == H("DFA66747DE9AE63030CA32611497C827")


def test_wake_frame():
    assert c.wake_frame() == H("5AA5D50000F5FA")


def test_crc8_vector():
    frame = H("5AA5D6010203040506070805FFFFFFFFFF6AF5FA")
    assert c.crc8(frame, 0, 16) == 0x6A == frame[17]


def test_random_frame_and_auth_vector():
    s = c.Session("00112233445566778899AABBCCDDEEFF", 0, app_random=H("1020304050607080"))
    assert s.random_exchange_frame() == H("5AA5D6102030405060708000FFFFFFFFFF3CF5FA")
    resp = s.parse_random_response(H("2AD601112233445566778801FFFFFFFF"))
    assert resp and resp["key_state"] == 1 and resp["crc_ok"] == 1
    assert s.auth_frame() == H("5BB50396C4727C202D50B06A0477CF412BEBF5FA")


def test_parse_random_response_shape():
    s = c.Session("0123456789abcdef0123456789abcdef", 3, app_random=H("1020304050607080"))
    r = s.parse_random_response(H("2AD601102030405060708001000000000000"[:32]))
    assert r["vehicle_random"] == H("1020304050607080")


def test_control_frame_envelope_and_sequence():
    s = c.Session("0123456789abcdef0123456789abcdef", 3, app_random=H("1020304050607080"))
    s.parse_random_response(H("2AD601112233445566778801FFFFFFFF"))
    a = s.auth_frame()
    f1 = s.control_frame(0x05)
    f2 = s.control_frame(0x05)
    for f in (a, f1, f2):
        assert len(f) == 20 and f[:2] == b"\x5b\xb5" and f[18:] == b"\xf5\xfa"
    assert f1 != f2 and f1 != a  # sequence counter changes the ciphertext


def test_control_response_parse():
    payload = bytearray(b"\xff" * 16)
    payload[0:5] = bytes([0x24, 0xE5, 0x07, 0x01, 0xC0])
    r = c.Session.parse_control_response(bytes(payload))
    assert r == {"control_code": 7, "success": True, "result": 1, "door_state": 0xC0}


def test_split_response_frame():
    body = bytearray(20)
    body[0:2] = b"\xa5\x5a"
    body[2:7] = bytes([0x24, 0xE5, 0x07, 0x01, 0xC0])
    body[18:20] = b"\xf5\xfa"
    body[17] = c.crc8(bytes(body), 0, 16)
    assert c.split_response_frame(bytes(body)) is not None
    body[3] ^= 1  # corrupt
    assert c.split_response_frame(bytes(body)) is None


def test_the_five_verified_command_codes():
    """The only codes confirmed on a real car. Changing one of these is a behaviour change."""
    assert COMMANDS == {"flash": 0x16, "horn": 0x0A, "lock": 0x07, "unlock": 0x05, "boot": 0x06}
    assert c.VERIFIED_COMMANDS == {
        "flash_lights": 0x16, "flash_and_horn": 0x0A, "lock": 0x07, "unlock": 0x05, "open_boot": 0x06,
    }
    assert sorted(COMMANDS.values()) == sorted(c.VERIFIED_COMMANDS.values())


def test_frame_assembler_handles_split_and_noisy_notifications():
    body = bytearray(20)
    body[0:2] = b"\xa5\x5a"
    body[2:7] = bytes([0x24, 0xE5, 0x07, 0x01, 0xC0])
    body[18:20] = b"\xf5\xfa"
    body[17] = c.crc8(bytes(body), 0, 16)
    frame = bytes(body)

    asm = FrameAssembler()
    assert asm.feed(frame[:7]) == []  # arrives in pieces
    out = asm.feed(frame[7:])
    assert len(out) == 1 and out[0][:5] == bytes([0x24, 0xE5, 0x07, 0x01, 0xC0])

    asm = FrameAssembler()
    out = asm.feed(b"\x00\x01\x02" + frame + frame)  # junk first, then two back-to-back frames
    assert len(out) == 2
