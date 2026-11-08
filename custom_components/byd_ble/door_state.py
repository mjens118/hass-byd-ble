"""Decode the door byte the car returns after a LOCK command.

Observed on an AU Sealion 8, car off, lock command, one thing open at a time:
  nothing open     0xC0
  front driver     0xC1
  front passenger  0xC2
  back driver      0xC8
  boot             0xD0
  back passenger   0xC4

Other commands (unlock, flash) return 0xFF, which carries no door information. With the car on, one
reading (driver door open) came back as 0xD0, so door readings taken with the car on are unreliable.
"""
from __future__ import annotations

LOCK_BASE = 0xC0
BASE_MASK = 0xC0
DOOR_BITS: dict[int, str] = {
    0x01: "front_driver",
    0x02: "front_passenger",
    0x04: "back_passenger",
    0x08: "back_driver",
    0x10: "boot",
}
_KNOWN_MASK = BASE_MASK | sum(DOOR_BITS)


def decode_lock_result(value: int | None) -> list[str] | None:
    """Return the list of open doors, or None if the byte is not recognised.

    An empty list means the car reported everything closed. None means "don't trust this".
    """
    if value is None or value == 0xFF:
        return None
    if value & BASE_MASK != BASE_MASK:
        return None
    if value & ~_KNOWN_MASK & 0xFF:
        return None  # a bit we have never seen
    return [name for bit, name in DOOR_BITS.items() if value & bit]
