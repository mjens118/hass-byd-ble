"""Constants for the BYD Bluetooth Key integration."""
from __future__ import annotations

DOMAIN = "byd_ble"
DOCS_URL = "https://github.com/mjens118/hass-byd-ble"

CONF_ADDRESS = "address"
CONF_DKEY = "dkey"
CONF_KEY_NUMBER = "key_number"
CONF_FALLBACK_LOCK = "fallback_lock"
CONF_UNLOCK_CODE = "unlock_code"
CONF_KEY_VALID_TO = "key_valid_to"  # epoch seconds (UTC), optional
CONF_WATCH_IMEI = "watch_imei"  # the made-up "watch" identity this key was issued to, optional
CONF_COUNTRY = "country"  # BYD server region the key was issued from, optional
CONF_UNDERSTOOD = "understood"  # consent tick in the QR setup flow, never stored

CODE_PATTERN = r"^\d{4,12}$"  # optional lock code: digits only, so Home Assistant shows a numeric keypad

# Warn this many days before the Bluetooth key expires.
KEY_EXPIRY_WARNING_DAYS = 14

SERVICE_UUID = "42594420-4155-544f-e0a9-e50e24dcca9e"
WRITE_UUID = "42590002-4155-544F-E0A9-E50E24DCCA9E"
NOTIFY_UUID = "42590003-4155-544F-E0A9-E50E24DCCA9E"

# Seconds
SESSION_TIMEOUT = 25.0
STEP_TIMEOUT = 5.0
CONTROL_TIMEOUT = 3.0
WAKE_TO_RANDOM_DELAY = 0.20
RANDOM_TO_AUTH_DELAY = 0.18

# Command name -> control code (only the commands upstream marks as verified)
COMMANDS = {
    "flash": 0x16,
    "horn": 0x0A,
    "lock": 0x07,
    "unlock": 0x05,
    "boot": 0x06,
}
