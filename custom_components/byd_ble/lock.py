"""Lock entity: Bluetooth first, optional cloud lock as fallback when out of range."""
from __future__ import annotations

import hmac
import logging
from datetime import timedelta
from typing import Any

from homeassistant.components.lock import DOMAIN as LOCK_DOMAIN, LockEntity
from homeassistant.const import ATTR_ENTITY_ID, SERVICE_LOCK, SERVICE_UNLOCK
from homeassistant.core import Event, EventStateChangedData, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.restore_state import RestoreEntity
from homeassistant.util import dt as dt_util

from . import BydBleConfigEntry
from .client import Busy, BydBleError
from .const import CODE_PATTERN, CONF_FALLBACK_LOCK, CONF_UNLOCK_CODE
from .door_state import decode_lock_result
from .entity import BydBleEntity

_LOGGER = logging.getLogger(__name__)

# Commands go out one at a time and the client refuses a second one while a session is running,
# so Home Assistant must not queue service calls for us (that would turn "refused" into "sent twice").
PARALLEL_UPDATES = 0

# After we send a command, ignore cloud lock changes for this long: the cloud can lag the car and
# report the state from before the command.
CLOUD_GRACE = timedelta(seconds=120)
CLOUD_STATES = {"locked": True, "unlocked": False}


async def async_setup_entry(
    hass: HomeAssistant, entry: BydBleConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    async_add_entities([BydBleLock(entry)])


class BydBleLock(BydBleEntity, LockEntity, RestoreEntity):
    """Central locking over Bluetooth.

    State comes from the last acknowledged command. If a cloud lock is configured, its state is
    mirrored in as well, so a lock or unlock done by phone, watch or fob is picked up when the cloud
    next reports it.
    """

    _attr_name = None
    _attr_should_poll = False

    def __init__(self, entry: BydBleConfigEntry) -> None:
        super().__init__(entry)
        self._attr_unique_id = f"{entry.unique_id}_lock"
        self._last: dict[str, Any] = {}
        self._cloud = entry.options.get(CONF_FALLBACK_LOCK) or None
        self._command_at = None  # when we last sent a command (UTC)
        # Optional code. Home Assistant requires it on lock and unlock alike (it cannot be unlock-only),
        # shows a keypad for it, and blocks callers that supply none, such as a voice assistant.
        self._code: str | None = entry.options.get(CONF_UNLOCK_CODE) or None
        if self._code:
            self._attr_code_format = CODE_PATTERN

    @property
    def assumed_state(self) -> bool:
        # Without a cloud lock to cross-check against, state can only be assumed.
        return not self._cloud

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if (last := await self.async_get_last_state()) is not None:
            if last.state == "locked":
                self._attr_is_locked = True
            elif last.state == "unlocked":
                self._attr_is_locked = False
        if self._cloud:
            # The cloud's current reading is fresher than whatever was restored.
            if (cur := self.hass.states.get(self._cloud)) is not None and cur.state in CLOUD_STATES:
                self._attr_is_locked = CLOUD_STATES[cur.state]
            self.async_on_remove(
                async_track_state_change_event(self.hass, [self._cloud], self._cloud_changed)
            )

    @callback
    def _cloud_changed(self, event: Event[EventStateChangedData]) -> None:
        new = event.data["new_state"]
        if new is None or new.state not in CLOUD_STATES:
            return  # unavailable / unknown: keep what we have
        if self._command_at and dt_util.utcnow() - self._command_at < CLOUD_GRACE:
            return  # too soon after our own command; the cloud may be stale
        locked = CLOUD_STATES[new.state]
        if locked == self._attr_is_locked:
            return
        self._attr_is_locked = locked
        for key in ("open_doors", "lock_pending", "door_state_note"):
            self._last.pop(key, None)
        self._last["state_source"] = "cloud"
        self._last["state_updated"] = dt_util.utcnow().isoformat()
        self.async_write_ha_state()

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return dict(self._last)

    def _check_code(self, kwargs: dict[str, Any]) -> None:
        """Refuse the command unless the right code came with it (only when a code is configured)."""
        if not self._code:
            return
        supplied = str(kwargs.get("code") or "")
        if not hmac.compare_digest(supplied.encode(), self._code.encode()):
            raise ServiceValidationError("Incorrect code. Nothing was sent to the car.")

    async def async_lock(self, **kwargs: Any) -> None:
        self._check_code(kwargs)
        await self._run("lock", SERVICE_LOCK, True)

    async def async_unlock(self, **kwargs: Any) -> None:
        self._check_code(kwargs)
        await self._run("unlock", SERVICE_UNLOCK, False)

    async def _run(self, action: str, service: str, locked_after: bool) -> None:
        fallback = self._cloud
        try:
            if not self.client.in_range:
                raise BydBleError("car is out of Bluetooth range")
            result = await self.client.send(action)
            self._command_at = dt_util.utcnow()
        except Busy as err:
            # A Bluetooth command is already running: refuse rather than double-send via the cloud.
            raise HomeAssistantError(f"Not sent: {err}") from err
        except BydBleError as err:
            # Nothing was sent over Bluetooth, so falling back to the cloud cannot double-act.
            self.note_ble_error(err)
            if not fallback:
                self._record(action, "failed", route="bluetooth", detail=str(err))
                raise HomeAssistantError(f"Could not send '{action}' over Bluetooth: {err}") from err
            _LOGGER.info("Bluetooth unavailable (%s); using cloud lock %s", err, fallback)
            await self.hass.services.async_call(
                LOCK_DOMAIN, service, {ATTR_ENTITY_ID: fallback}, blocking=True
            )
            self._command_at = dt_util.utcnow()
            self._attr_is_locked = locked_after
            self._record(action, "ok", route="cloud", detail=str(err))
            return

        status = result["status"]
        if status == "ok":
            door_state = result.get("door_state")
            if action == "lock":
                open_doors = decode_lock_result(door_state)
                if open_doors is None:
                    # Unrecognised byte: the car accepted the command but we can't confirm it locked.
                    self._attr_is_locked = None
                    self._record(action, "ok", route="bluetooth", door_state=door_state, door_state_note="unrecognised")
                elif open_doors:
                    # Accepted, but the car will not lock around an open door. Not locked yet.
                    self._attr_is_locked = False
                    self._record(
                        action, "ok", route="bluetooth", door_state=door_state,
                        lock_pending=True, open_doors=open_doors,
                    )
                else:
                    self._attr_is_locked = True
                    self._record(action, "ok", route="bluetooth", door_state=door_state)
                return
            self._attr_is_locked = locked_after
            self._record(action, "ok", route="bluetooth", door_state=door_state)
            return
        # Sent but not confirmed, or refused: never retry or re-route automatically.
        self._attr_is_locked = None
        self._record(action, status, route="bluetooth", detail=result.get("detail"))
        raise HomeAssistantError(
            f"'{action}' was {'rejected by the car' if status == 'rejected' else 'not confirmed'}"
            f" ({result.get('detail', status)}). Check the car; it was not retried."
        )

    def _record(self, action: str, status: str, *, route: str, **extra: Any) -> None:
        door = extra.pop("door_state", None)
        self._last = {
            "last_command": action,
            "last_result": status,
            "last_route": route,
            "state_source": route,
            "state_updated": dt_util.utcnow().isoformat(),
            **({"door_state_raw": f"0x{door:02x}"} if door is not None else {}),
            **{k: v for k, v in extra.items() if v},
        }
        self.async_write_ha_state()
