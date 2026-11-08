"""Sensor: when the Bluetooth key expires (only if the server told us)."""
from __future__ import annotations

from datetime import UTC, datetime

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import BydBleConfigEntry
from .const import CONF_KEY_VALID_TO
from .entity import BydBleEntity

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant, entry: BydBleConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    if entry.data.get(CONF_KEY_VALID_TO):
        async_add_entities([BydBleKeyExpiry(entry)])


class BydBleKeyExpiry(BydBleEntity, SensorEntity):
    _attr_translation_key = "key_expires"
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_should_poll = False

    def __init__(self, entry: BydBleConfigEntry) -> None:
        super().__init__(entry)
        self._attr_unique_id = f"{entry.unique_id}_key_expires"
        self._attr_native_value = datetime.fromtimestamp(entry.data[CONF_KEY_VALID_TO], UTC)
