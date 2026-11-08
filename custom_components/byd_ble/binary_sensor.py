"""Binary sensor: is the car currently seen over Bluetooth?"""
from __future__ import annotations

from homeassistant.components import bluetooth
from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import BydBleConfigEntry
from .const import CONF_ADDRESS
from .entity import BydBleEntity

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant, entry: BydBleConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    async_add_entities([BydBleInRange(entry)])


class BydBleInRange(BydBleEntity, BinarySensorEntity):
    _attr_translation_key = "in_range"
    _attr_should_poll = False

    def __init__(self, entry: BydBleConfigEntry) -> None:
        super().__init__(entry)
        self._attr_unique_id = f"{entry.unique_id}_in_range"
        self._rssi: int | None = None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        address = self._entry.data[CONF_ADDRESS]
        self._attr_is_on = self.client.in_range
        self.async_on_remove(
            bluetooth.async_register_callback(
                self.hass,
                self._seen,
                bluetooth.BluetoothCallbackMatcher(address=address, connectable=True),
                bluetooth.BluetoothScanningMode.ACTIVE,
            )
        )
        self.async_on_remove(
            bluetooth.async_track_unavailable(self.hass, self._gone, address, connectable=True)
        )

    @property
    def extra_state_attributes(self) -> dict[str, int | None]:
        return {"rssi": self._rssi}

    @callback
    def _seen(self, service_info: bluetooth.BluetoothServiceInfoBleak, change: object) -> None:
        self._attr_is_on = True
        self._rssi = service_info.rssi
        self.async_write_ha_state()

    @callback
    def _gone(self, service_info: bluetooth.BluetoothServiceInfoBleak) -> None:
        self._attr_is_on = False
        self.async_write_ha_state()
