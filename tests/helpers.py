from unittest.mock import patch

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.byd_ble.const import DOMAIN

ADDRESS = "AA:BB:CC:DD:EE:FF"


class FakeClient:
    """Stands in for BydBleClient at the entity level."""

    def __init__(self, in_range=True):
        self.in_range = in_range
        self.address = ADDRESS
        self.sent = []
        self.results = []
        self.exc = None

    async def send(self, action):
        self.sent.append(action)
        if self.exc:
            raise self.exc
        return self.results.pop(0)

    async def check_auth(self):
        return None


async def setup_entry(hass, fake, options=None, data=None, expect_loaded=True, **entry_kwargs):
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=ADDRESS,
        title="BYD Bluetooth key",
        data={"address": ADDRESS, "dkey": "00112233445566778899AABBCCDDEEFF", "key_number": 0, **(data or {})},
        options=options or {},
        **entry_kwargs,
    )
    entry.add_to_hass(hass)
    with patch("custom_components.byd_ble.BydBleClient", return_value=fake):
        assert await hass.config_entries.async_setup(entry.entry_id) is expect_loaded
        await hass.async_block_till_done()
    return entry
