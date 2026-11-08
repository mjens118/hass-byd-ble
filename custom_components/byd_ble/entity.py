"""Shared entity base and command helper."""
from __future__ import annotations

from typing import Any

from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import CONNECTION_BLUETOOTH, DeviceInfo
from homeassistant.helpers.entity import Entity

from . import BydBleConfigEntry
from .client import BydBleClient, BydBleError, KeyRejected
from .const import CONF_ADDRESS, DOMAIN


class BydBleEntity(Entity):
    """Base entity tied to one car."""

    _attr_has_entity_name = True

    def __init__(self, entry: BydBleConfigEntry) -> None:
        self._entry = entry
        self.client: BydBleClient = entry.runtime_data.client
        address = entry.data[CONF_ADDRESS]
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, address)},
            connections={(CONNECTION_BLUETOOTH, address)},
            manufacturer="BYD",
            name=entry.title,
        )

    def note_ble_error(self, err: BydBleError) -> None:
        """React to a failure that happened before anything was sent.

        If the car explicitly refused the key, ask the user for a new one. That is a notification, not
        an action: nothing is deleted and the integration keeps working for everything else.
        """
        if isinstance(err, KeyRejected):
            self._entry.async_start_reauth(self.hass)

    async def send_or_raise(self, action: str) -> dict[str, Any]:
        """Send one command, turning every non-success outcome into a HomeAssistantError."""
        try:
            result = await self.client.send(action)
        except BydBleError as err:
            self.note_ble_error(err)
            raise HomeAssistantError(f"Could not send '{action}' over Bluetooth: {err}") from err
        if result["status"] == "rejected":
            raise HomeAssistantError(f"The car rejected '{action}'")
        if result["status"] == "unknown":
            raise HomeAssistantError(
                f"No confirmation for '{action}' ({result.get('detail')}). Check the car; it was not retried."
            )
        return result
