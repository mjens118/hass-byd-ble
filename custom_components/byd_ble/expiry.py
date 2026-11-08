"""Bluetooth key expiry: parse the server's valid-to value and raise Repairs issues before it lapses."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir
from homeassistant.util import dt as dt_util

from .const import CONF_KEY_VALID_TO, DOCS_URL, DOMAIN, KEY_EXPIRY_WARNING_DAYS

ISSUE_EXPIRING = "key_expiring"
ISSUE_EXPIRED = "key_expired"

# Anything outside this range is treated as "not a date we understand".
_MIN_YEAR, _MAX_YEAR = 2000, 2200
# Epoch values above this are milliseconds (1e11 s is the year 5138; 1e11 ms is 1973).
_MS_THRESHOLD = 1e11


def parse_valid_to(value: Any) -> int | None:
    """Return the expiry as epoch seconds (UTC), or None if it cannot be understood.

    Accepts epoch milliseconds or seconds (as a number or digit string), or an ISO 8601 string
    (a string with no time zone is taken as UTC). Never raises: a key whose expiry we cannot read
    still works, it just cannot be warned about.
    """
    if value is None or isinstance(value, bool):
        return None
    seconds: float | None = None
    try:
        if isinstance(value, (int, float)):
            seconds = float(value)
        elif isinstance(value, str):
            text = value.strip()
            if text.replace(".", "", 1).isdigit():
                seconds = float(text)
            elif (parsed := dt_util.parse_datetime(text)) is not None:
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=UTC)
                seconds = parsed.timestamp()
        if seconds is None:
            return None
        if seconds > _MS_THRESHOLD:
            seconds /= 1000
        year = datetime.fromtimestamp(seconds, UTC).year
    except (ValueError, OverflowError, OSError):
        return None
    if not _MIN_YEAR <= year <= _MAX_YEAR:
        return None
    return int(seconds)


def _issue_ids(entry_id: str) -> tuple[str, str]:
    return f"{ISSUE_EXPIRING}_{entry_id}", f"{ISSUE_EXPIRED}_{entry_id}"


@callback
def async_check_expiry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Create or clear the Repairs issues for this entry's key. Safe to call repeatedly."""
    expiring_id, expired_id = _issue_ids(entry.entry_id)
    valid_to = entry.data.get(CONF_KEY_VALID_TO)
    if not valid_to:
        ir.async_delete_issue(hass, DOMAIN, expiring_id)
        ir.async_delete_issue(hass, DOMAIN, expired_id)
        return

    expires = datetime.fromtimestamp(valid_to, UTC)
    remaining = expires - dt_util.utcnow()
    placeholders = {"date": dt_util.as_local(expires).strftime("%d %B %Y"), "name": entry.title}
    common = {
        "is_fixable": False,
        "learn_more_url": f"{DOCS_URL}#key-expiry",
        "translation_placeholders": placeholders,
    }
    if remaining <= timedelta(0):
        ir.async_delete_issue(hass, DOMAIN, expiring_id)
        ir.async_create_issue(
            hass, DOMAIN, expired_id, severity=ir.IssueSeverity.ERROR, translation_key=ISSUE_EXPIRED, **common
        )
    elif remaining <= timedelta(days=KEY_EXPIRY_WARNING_DAYS):
        ir.async_delete_issue(hass, DOMAIN, expired_id)
        ir.async_create_issue(
            hass, DOMAIN, expiring_id, severity=ir.IssueSeverity.WARNING, translation_key=ISSUE_EXPIRING, **common
        )
    else:
        ir.async_delete_issue(hass, DOMAIN, expiring_id)
        ir.async_delete_issue(hass, DOMAIN, expired_id)


@callback
def async_clear_issues(hass: HomeAssistant, entry_id: str) -> None:
    for issue_id in _issue_ids(entry_id):
        ir.async_delete_issue(hass, DOMAIN, issue_id)
