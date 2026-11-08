"""BYD Bluetooth Key: lock, unlock and flash a BYD over Bluetooth LE (experimental)."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.event import async_track_time_interval

from . import codec
from .client import BydBleClient
from .const import CONF_ADDRESS, CONF_DKEY, CONF_KEY_NUMBER
from .expiry import async_check_expiry, async_clear_issues

PLATFORMS: list[Platform] = [Platform.LOCK, Platform.BUTTON, Platform.BINARY_SENSOR, Platform.SENSOR]

# How often to re-evaluate the key expiry warning. Cheap, and a long-running instance would otherwise
# only notice at the next restart.
EXPIRY_CHECK_INTERVAL = timedelta(hours=12)


@dataclass
class BydBleData:
    """Runtime data for one config entry."""

    client: BydBleClient
    options: dict[str, Any]  # snapshot taken at setup, to tell option changes from other updates


type BydBleConfigEntry = ConfigEntry[BydBleData]


async def async_setup_entry(hass: HomeAssistant, entry: BydBleConfigEntry) -> bool:
    dkey = entry.data[CONF_DKEY]
    key_number = int(entry.data[CONF_KEY_NUMBER])
    try:
        codec.Session(dkey, key_number)  # validates the stored key without touching Bluetooth
    except ValueError as err:
        raise ConfigEntryAuthFailed("The stored Bluetooth key is not valid") from err

    client = BydBleClient(hass, entry.data[CONF_ADDRESS], dkey, key_number, entry.title)
    entry.runtime_data = BydBleData(client=client, options=dict(entry.options))

    @callback
    def _check_expiry(_now: datetime | None = None) -> None:
        async_check_expiry(hass, entry)

    _check_expiry()
    entry.async_on_unload(async_track_time_interval(hass, _check_expiry, EXPIRY_CHECK_INTERVAL))

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    if not hasattr(config_entries, "OptionsFlowWithReload"):
        # Home Assistant before 2025.8 has no reloading options flow, so reload from a listener instead.
        # Newer versions reload from the options flow itself, and a listener there is deprecated.
        entry.async_on_unload(entry.add_update_listener(_async_options_updated))
    return True


async def _async_options_updated(hass: HomeAssistant, entry: BydBleConfigEntry) -> None:
    """Reload when the options change (Home Assistant before 2025.8 only).

    Data updates (a new key from reauth or reconfigure) reload through their own flow, so reacting to
    them here as well would reload twice.
    """
    if dict(entry.options) != entry.runtime_data.options:
        await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: BydBleConfigEntry) -> bool:
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_remove_entry(hass: HomeAssistant, entry: BydBleConfigEntry) -> None:
    async_clear_issues(hass, entry.entry_id)
