"""Buttons: flash lights (on by default), horn and boot (off by default)."""
from __future__ import annotations

from dataclasses import dataclass

from homeassistant.components.button import ButtonEntity, ButtonEntityDescription
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import BydBleConfigEntry
from .entity import BydBleEntity

# One command per press, and the client refuses a second while one is running, so no queueing here.
PARALLEL_UPDATES = 0


@dataclass(frozen=True, kw_only=True)
class BydBleButtonDescription(ButtonEntityDescription):
    """A button that sends one command."""

    action: str


BUTTONS = (
    BydBleButtonDescription(key="flash", action="flash", translation_key="flash_lights"),
    BydBleButtonDescription(
        key="horn", action="horn", translation_key="flash_lights_and_horn", entity_registry_enabled_default=False
    ),
    BydBleButtonDescription(
        key="boot", action="boot", translation_key="open_boot", entity_registry_enabled_default=False
    ),
)


async def async_setup_entry(
    hass: HomeAssistant, entry: BydBleConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    async_add_entities(BydBleButton(entry, description) for description in BUTTONS)


class BydBleButton(BydBleEntity, ButtonEntity):
    entity_description: BydBleButtonDescription

    def __init__(self, entry: BydBleConfigEntry, description: BydBleButtonDescription) -> None:
        super().__init__(entry)
        self.entity_description = description
        # Unique IDs are part of existing installs: keep the {address}_{key} form.
        self._attr_unique_id = f"{entry.unique_id}_{description.key}"

    async def async_press(self) -> None:
        await self.send_or_raise(self.entity_description.action)
