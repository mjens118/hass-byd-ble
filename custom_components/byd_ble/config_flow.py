"""Config flow: get a key (QR code or pasted credentials.json), verify it against the car, then save.

Two ways to get the key, offered as a menu:
- QR code: Home Assistant asks BYD's servers for a QR code, you approve it in the official BYD app, and
  Home Assistant collects the key. Behind an explicit "I understand" step, because it contacts BYD as a
  Samsung smartwatch (see provisioning.py).
- Paste: the contents of a credentials.json made by the separate provisioning tool.

Either way the key is checked against the car over Bluetooth before anything is saved. Reauth (the car
refused the key) and reconfigure (replace the key any time) offer the same two ways.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections.abc import Mapping
from typing import Any

import voluptuous as vol

from homeassistant.components import bluetooth
from homeassistant.components.bluetooth import BluetoothServiceInfoBleak
from homeassistant.config_entries import (
    SOURCE_REAUTH,
    SOURCE_RECONFIGURE,
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .client import AuthFailed, BydBleClient, BydBleError
from .const import (
    CODE_PATTERN,
    CONF_ADDRESS,
    CONF_COUNTRY,
    CONF_DKEY,
    CONF_FALLBACK_LOCK,
    CONF_KEY_NUMBER,
    CONF_KEY_VALID_TO,
    CONF_UNDERSTOOD,
    CONF_UNLOCK_CODE,
    CONF_WATCH_IMEI,
    DOMAIN,
    SERVICE_UUID,
)
from .expiry import parse_valid_to
from .provisioning import (
    APPROVED,
    DEFAULT_COUNTRY,
    PENDING,
    QR_TIMEOUT_SECONDS,
    SERVERS,
    IssuedKey,
    ProvisionError,
    ServerUnreachable,
    WatchProvisioner,
)

_LOGGER = logging.getLogger(__name__)

CONF_CREDENTIALS = "credentials"
DEFAULT_TITLE = "BYD Bluetooth key"

# After "Submit" the QR may have been approved a moment ago. Look a few times before saying "not yet".
APPROVAL_CHECKS = 4
APPROVAL_CHECK_DELAY = 3.0

# Everything that belongs to one issued key. A replacement key replaces all of it, never part of it.
KEY_DATA_KEYS = (CONF_DKEY, CONF_KEY_NUMBER, CONF_KEY_VALID_TO, CONF_WATCH_IMEI, CONF_COUNTRY)

_IMEI = re.compile(r"[0-9A-Fa-f]{32}")


def _fallback_field() -> dict:
    return {vol.Optional(CONF_FALLBACK_LOCK): selector.EntitySelector(selector.EntitySelectorConfig(domain="lock"))}


def _credentials_field() -> dict:
    return {vol.Required(CONF_CREDENTIALS): selector.TextSelector(selector.TextSelectorConfig(multiline=True))}


PASTE_SCHEMA = vol.Schema({**_credentials_field(), **_fallback_field()})
CREDENTIALS_SCHEMA = vol.Schema(_credentials_field())
OPTIONS_SCHEMA = vol.Schema(
    {
        **_fallback_field(),
        vol.Optional(CONF_UNLOCK_CODE): selector.TextSelector(
            selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
        ),
    }
)


def parse_credentials(text: str) -> dict[str, Any] | None:
    """Parse the JSON written by byd_provision.py. Returns None if unusable. Never logs the key."""
    try:
        data = json.loads(text)
        dkey = str(data["dkey"]).strip()
        bytes.fromhex(dkey)
        if not 2 <= len(dkey) <= 64:
            return None
        imei = str(data.get("watch_imei") or "")
        country = str(data.get("country") or "").upper()
        return {
            CONF_DKEY: dkey,
            CONF_KEY_NUMBER: int(data.get("key_number", 0)),
            "mac": (str(data.get("mac") or "").upper() or None),
            CONF_KEY_VALID_TO: parse_valid_to(data.get("key_valid_to")),
            CONF_WATCH_IMEI: imei.upper() if _IMEI.fullmatch(imei) else None,
            CONF_COUNTRY: country if country in SERVERS else None,
        }
    except (ValueError, KeyError, TypeError, AttributeError):
        return None


def credentials_from_issued_key(key: IssuedKey, watch_imei: str, country: str) -> dict[str, Any] | None:
    """Run what BYD returned through the same checks as a pasted file."""
    return parse_credentials(
        json.dumps(
            {
                "dkey": key.dkey,
                "key_number": key.key_number,
                "mac": key.mac,
                "key_valid_to": key.key_valid_to,
                "watch_imei": watch_imei,
                "country": country,
            }
        )
    )


class BydBleConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self) -> None:
        self._discovery: BluetoothServiceInfoBleak | None = None
        self._creds: dict[str, Any] | None = None  # a key waiting to be checked against the car
        self._address: str | None = None
        self._fallback: str | None = None
        self._country = DEFAULT_COUNTRY
        self._watch_imei: str | None = None
        self._provisioner: WatchProvisioner | None = None
        self._qr_uuid: str | None = None
        self._qr_content: str | None = None
        self._qr_started = 0.0

    # ---- helpers ----

    @property
    def _target_entry(self) -> ConfigEntry | None:
        """The entry whose key is being replaced, or None when setting up a new one."""
        if self.source == SOURCE_REAUTH:
            return self._get_reauth_entry()
        if self.source == SOURCE_RECONFIGURE:
            return self._get_reconfigure_entry()
        return None

    def _resolve_address(self, mac: str | None) -> str | None:
        """Prefer what Bluetooth actually sees; fall back to the MAC the server gave us."""
        if self._discovery is not None:
            return self._discovery.address.upper()
        seen = [
            i.address.upper()
            for i in bluetooth.async_discovered_service_info(self.hass, connectable=True)
            if SERVICE_UUID in [u.lower() for u in i.service_uuids]
        ]
        if mac and mac in seen:
            return mac
        if len(seen) == 1:
            return seen[0]
        return mac

    async def _async_check_key(self, creds: dict[str, Any], address: str) -> str | None:
        """Authenticate against the car without sending any command. Returns an error key, or None."""
        client = BydBleClient(self.hass, address, creds[CONF_DKEY], creds[CONF_KEY_NUMBER], DEFAULT_TITLE)
        try:
            await client.check_auth()
        except AuthFailed as err:
            _LOGGER.warning("Car refused authentication: %s", err)
            return "auth_failed"
        except BydBleError:
            return "cannot_connect"
        return None

    async def _async_resolve_and_check(self, creds: dict[str, Any]) -> str | None:
        """Find the car, make sure it is not already set up, and test the key on it. Error key or None."""
        if (entry := self._target_entry) is not None:
            address = entry.data[CONF_ADDRESS]
        else:
            if (address := self._resolve_address(creds["mac"])) is None:
                return "no_car"
            await self.async_set_unique_id(address)
            self._abort_if_unique_id_configured()
        if error := await self._async_check_key(creds, address):
            return error
        self._address = address
        return None

    @staticmethod
    def _key_data(creds: dict[str, Any]) -> dict[str, Any]:
        data = {CONF_DKEY: creds[CONF_DKEY], CONF_KEY_NUMBER: creds[CONF_KEY_NUMBER]}
        for key in (CONF_KEY_VALID_TO, CONF_WATCH_IMEI, CONF_COUNTRY):
            if creds.get(key):
                data[key] = creds[key]
        return data

    def _async_finish(self) -> ConfigFlowResult:
        """The key works on the car: save it (new entry) or swap it in (reauth, reconfigure)."""
        assert self._creds is not None and self._address is not None
        if (entry := self._target_entry) is not None:
            # Replace rather than merge, so an expiry date from the old key cannot outlive it.
            data = {k: v for k, v in entry.data.items() if k not in KEY_DATA_KEYS}
            data.update(self._key_data(self._creds))
            return self.async_update_reload_and_abort(entry, data=data)
        options = {CONF_FALLBACK_LOCK: self._fallback} if self._fallback else {}
        return self.async_create_entry(
            title=DEFAULT_TITLE,
            data={CONF_ADDRESS: self._address, **self._key_data(self._creds)},
            options=options,
        )

    # ---- choose how to get the key ----

    async def async_step_bluetooth(self, discovery_info: BluetoothServiceInfoBleak) -> ConfigFlowResult:
        await self.async_set_unique_id(discovery_info.address.upper())
        self._abort_if_unique_id_configured()
        self._discovery = discovery_info
        self.context["title_placeholders"] = {"name": discovery_info.name or "BYD"}
        return await self.async_step_user()

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return self.async_show_menu(step_id="user", menu_options=["qr", "paste"])

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        """The car refused the stored key."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return self.async_show_menu(
            step_id="reauth_confirm",
            menu_options=["qr", "paste"],
            description_placeholders={"name": self._get_reauth_entry().title},
        )

    async def async_step_reconfigure(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Replace the key at any time, for example when the expiry warning appears."""
        return self.async_show_menu(
            step_id="reconfigure",
            menu_options=["qr", "paste"],
            description_placeholders={"name": self._get_reconfigure_entry().title},
        )

    # ---- paste credentials.json ----

    async def async_step_paste(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        entry = self._target_entry
        schema = CREDENTIALS_SCHEMA if entry else PASTE_SCHEMA
        errors: dict[str, str] = {}
        if user_input is not None:
            creds = parse_credentials(user_input[CONF_CREDENTIALS])
            if creds is None:
                errors[CONF_CREDENTIALS] = "invalid_credentials"
            elif error := await self._async_resolve_and_check(creds):
                errors["base"] = error
            else:
                self._creds = creds
                self._fallback = user_input.get(CONF_FALLBACK_LOCK) if entry is None else None
                return self._async_finish()
        return self.async_show_form(
            step_id="paste",
            data_schema=self.add_suggested_values_to_schema(schema, user_input),
            errors=errors,
            description_placeholders={"name": entry.title if entry else "your car"},
        )

    # ---- QR code ----

    def _qr_consent_schema(self) -> vol.Schema:
        default_country = DEFAULT_COUNTRY
        if (entry := self._target_entry) is not None:
            default_country = entry.data.get(CONF_COUNTRY) or DEFAULT_COUNTRY
        fields: dict[Any, Any] = {
            # Lowercase because Home Assistant requires translation keys to be. Converted back on submit.
            vol.Required(CONF_COUNTRY, default=default_country.lower()): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=[c.lower() for c in SERVERS],
                    mode=selector.SelectSelectorMode.DROPDOWN,
                    translation_key="country",
                )
            )
        }
        if entry is None:
            fields.update(_fallback_field())
        fields[vol.Required(CONF_UNDERSTOOD, default=False)] = selector.BooleanSelector()
        return vol.Schema(fields)

    async def async_step_qr(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Explain what is about to happen and ask for a clear yes before contacting BYD."""
        entry = self._target_entry
        if entry is None and (address := self._resolve_address(None)):
            # Stop before touching BYD's servers if this car is already set up.
            await self.async_set_unique_id(address)
            self._abort_if_unique_id_configured()
        errors: dict[str, str] = {}
        if user_input is not None:
            if not user_input.get(CONF_UNDERSTOOD):
                errors[CONF_UNDERSTOOD] = "must_understand"
            else:
                self._country = user_input[CONF_COUNTRY].upper()
                self._fallback = user_input.get(CONF_FALLBACK_LOCK) if entry is None else None
                self._watch_imei = (entry.data.get(CONF_WATCH_IMEI) if entry else None) or self._watch_imei
                if error := await self._async_new_qr():
                    errors["base"] = error
                else:
                    return await self.async_step_qr_scan()
        return self.async_show_form(
            step_id="qr",
            data_schema=self.add_suggested_values_to_schema(self._qr_consent_schema(), user_input),
            errors=errors,
            description_placeholders={"name": entry.title if entry else "your car"},
        )

    async def _async_new_qr(self) -> str | None:
        """Ask BYD for a fresh QR code. Returns an error key, or None."""
        self._provisioner = WatchProvisioner(async_get_clientsession(self.hass), self._country, self._watch_imei)
        self._watch_imei = self._provisioner.watch_imei  # same "watch" for every QR in this flow
        try:
            self._qr_uuid, self._qr_content = await self._provisioner.async_create_qr()
        except ServerUnreachable:
            return "cannot_reach_byd"
        except ProvisionError as err:
            _LOGGER.warning("BYD would not issue a QR code: %s", err)
            return "byd_refused"
        self._qr_started = time.monotonic()
        return None

    def _show_qr(self, errors: dict[str, str]) -> ConfigFlowResult:
        assert self._qr_content is not None
        return self.async_show_form(
            step_id="qr_scan",
            data_schema=vol.Schema(
                {
                    vol.Optional("qr_code"): selector.QrCodeSelector(
                        selector.QrCodeSelectorConfig(
                            data=self._qr_content,
                            scale=6,
                            error_correction_level=selector.QrErrorCorrectionLevel.QUARTILE,
                        )
                    )
                }
            ),
            errors=errors,
        )

    async def async_step_qr_scan(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Show the QR code. On Submit, see whether it was approved and collect the key."""
        errors: dict[str, str] = {}
        if user_input is not None:
            outcome = await self._async_collect_key()
            if outcome is None:
                return await self._async_verify_and_finish()
            errors["base"] = outcome
        return self._show_qr(errors)

    async def _async_collect_key(self) -> str | None:
        """Returns None once the key is in self._creds, otherwise an error key for the form.

        A code that was refused, expired or already used is replaced with a fresh one, so the form never
        shows a QR code that cannot work.
        """
        assert self._provisioner is not None and self._qr_uuid is not None
        status = PENDING
        try:
            for attempt in range(APPROVAL_CHECKS):
                status = await self._provisioner.async_check_qr(self._qr_uuid)
                if status != PENDING:
                    break
                if attempt < APPROVAL_CHECKS - 1:
                    await asyncio.sleep(APPROVAL_CHECK_DELAY)
        except ServerUnreachable:
            return "cannot_reach_byd"
        except ProvisionError as err:
            _LOGGER.warning("BYD would not check the QR code: %s", err)
            return "byd_refused"

        if status == PENDING:
            if time.monotonic() - self._qr_started <= QR_TIMEOUT_SECONDS:
                return "not_approved_yet"
            return await self._async_new_qr() or "qr_expired"
        if status != APPROVED:
            return await self._async_new_qr() or "qr_expired"

        try:
            issued = await self._provisioner.async_fetch_key(self._qr_uuid)
        except ServerUnreachable:
            return "cannot_reach_byd"  # keep this code: it was approved, and Submit can try again
        except ProvisionError as err:
            _LOGGER.warning("BYD approved the code but gave no usable key: %s", err)
            return await self._async_new_qr() or "no_key"
        creds = credentials_from_issued_key(issued, self._provisioner.watch_imei, self._country)
        if creds is None:
            return await self._async_new_qr() or "no_key"
        self._creds = creds
        return None

    async def _async_verify_and_finish(self) -> ConfigFlowResult:
        """Test the new key on the car. If the car is not answering, keep the key and let the user retry."""
        assert self._creds is not None
        if error := await self._async_resolve_and_check(self._creds):
            return self.async_show_form(
                step_id="qr_verify", data_schema=vol.Schema({}), errors={"base": error}
            )
        return self._async_finish()

    async def async_step_qr_verify(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """The key was issued but the car did not answer. Retry the Bluetooth check without a new QR code."""
        if user_input is None:
            return self.async_show_form(step_id="qr_verify", data_schema=vol.Schema({}))
        return await self._async_verify_and_finish()

    # ---- options ----

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return BydBleOptionsFlow()


# Reloads the entry when the options change, on Home Assistant 2025.8 and later. Older versions get the same
# behaviour from an update listener in __init__.py.
_OptionsFlowBase = getattr(config_entries, "OptionsFlowWithReload", OptionsFlow)


class BydBleOptionsFlow(_OptionsFlowBase):
    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            code = (user_input.get(CONF_UNLOCK_CODE) or "").strip()
            if code and not re.fullmatch(CODE_PATTERN, code):
                errors[CONF_UNLOCK_CODE] = "invalid_code"
            else:
                options = {k: v for k, v in user_input.items() if k != CONF_UNLOCK_CODE and v}
                if code:
                    options[CONF_UNLOCK_CODE] = code
                return self.async_create_entry(data=options)
        return self.async_show_form(
            step_id="init",
            data_schema=self.add_suggested_values_to_schema(OPTIONS_SCHEMA, user_input or self.config_entry.options),
            errors=errors,
        )
