"""Diagnostics with the key material redacted.

Includes enough to help with a "works on my model" report: the advertised name and service UUIDs, signal
strength and the result of the last command (including the raw door byte), but never the key, the code or
the address, including the copies of it that Home Assistant keeps in discovery keys and advertisement data.
"""
from __future__ import annotations

from typing import Any

from homeassistant.components import bluetooth
from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from . import BydBleConfigEntry
from .const import CONF_ADDRESS, CONF_DKEY, CONF_UNLOCK_CODE, CONF_WATCH_IMEI, DOMAIN

# "discovery_keys" holds the car's address inside a repr string, so it has to go as well.
# The watch identity is what BYD ties the key to, so it is treated like the key.
TO_REDACT = {CONF_DKEY, CONF_ADDRESS, CONF_UNLOCK_CODE, CONF_WATCH_IMEI, "unique_id", "discovery_keys"}


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry: BydBleConfigEntry) -> dict[str, Any]:
    client = entry.runtime_data.client

    advertisement: dict[str, Any] | None = None
    if info := bluetooth.async_last_service_info(hass, entry.data[CONF_ADDRESS], connectable=True):
        advertisement = {
            "name": info.name,
            "rssi": info.rssi,
            "service_uuids": list(info.service_uuids),
            # Not the IDs themselves: on a real car the "company ID" is two bytes of its own address.
            "has_manufacturer_data": bool(info.manufacturer_data),
        }

    lock: dict[str, Any] | None = None
    registry = er.async_get(hass)
    if lock_id := registry.async_get_entity_id("lock", DOMAIN, f"{entry.unique_id}_lock"):
        if state := hass.states.get(lock_id):
            lock = {"state": state.state, "attributes": dict(state.attributes)}

    return {
        "entry": async_redact_data(entry.as_dict(), TO_REDACT),
        "in_range": client.in_range,
        "advertisement": advertisement,
        "lock": lock,
    }
