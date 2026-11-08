<p align="center">
  <img src="https://raw.githubusercontent.com/mjens118/hass-byd-ble/main/custom_components/byd_ble/brand/icon.png" alt="BYD Bluetooth Key" width="128">
</p>

# BYD Bluetooth Key for Home Assistant

Lock, unlock, flash the lights and open the boot of a BYD over Bluetooth LE, straight from Home Assistant.
It talks to the car locally, so it is fast and works without the BYD cloud.

> **Unofficial and experimental.** Not affiliated with, endorsed by, or supported by BYD. It uses private,
> undocumented interfaces that BYD can change or block at any time. Do not rely on it as your only key,
> keep your physical key with you, and test while the car is parked. Treat the key it stores like a car key.
> You use it at your own risk, with your own car and account. The MIT licence gives you no rights to BYD's
> trademarks, services, accounts or firmware.

Verified by hand on an AU BYD Sealion 8 (setup, auth, flash, lock, unlock, open boot). Other models and regions
are untested, so please [report what you find](#does-it-work-on-your-car).

It is separate from the cloud-based [hass-byd-vehicle](https://github.com/jkaberg/hass-byd-vehicle)
integration (battery, climate, location and so on). The two work well together: you can pick your cloud lock
as a fallback for when the car is out of Bluetooth range.

## What you need
- A BYD with Bluetooth digital key support.
- Home Assistant 2025.1 or newer, with a Bluetooth adapter or an ESPHome Bluetooth proxy within range of
  where the car is parked. (Tested on a Raspberry Pi's built-in adapter. Proxies should work but are not verified.)

## Install
**HACS (recommended):** HACS > three-dot menu > Custom repositories > add
`https://github.com/mjens118/hass-byd-ble` as an **Integration** > download "BYD Bluetooth Key" > restart
Home Assistant.

**Manual:** copy `custom_components/byd_ble` into `/config/custom_components/` and restart.

## Setup
**If Home Assistant will use a separate or shared BYD account, do this first.** A separate account lets your phone
or watch stay on another one, but BYD only issues a Bluetooth key to an account whose authorisation includes it.
In the BYD app, signed in to the car's main account, open **Authorisation details**, find that account, and
check **Bluetooth key** is listed. If it is not, edit the authorisation and select it. On a real car it was
off, and BYD approved the QR code but sent back an empty key until it was switched on. Menu names may differ
between app versions.

1. Settings > Devices & services. Accept the discovered "BYD BLE3" card, or Add integration >
   "BYD Bluetooth Key".
2. Choose **Set up with a QR code**. Read the notice, tick the box, and Home Assistant shows a QR code.
   You can also pick your cloud lock as the fallback here.
3. In the official BYD app, signed in to the account Home Assistant should use, scan the code the way you
   would to approve a smartwatch. Then select **Submit**.
4. Home Assistant checks the key against the car before saving anything. The car must be awake and in range.
   If it does not answer, the key is kept and you can retry without a new code.

**Prefer to paste a key?** Choose **Paste a credentials.json (advanced)** and paste the contents of a file made
by the separate provisioning tool. Use it for a region the QR setup does not offer, or if BYD changes its servers
and the QR setup stops working.

### What the QR setup does
It is the only part of this integration that contacts BYD's servers, and only while you set up or replace a
key. It asks BYD for a key while **presenting itself as a Samsung smartwatch**, which is how the
[upstream project](https://github.com/PoorGrammerA/BydBleAutoLock) works too. BYD may regard that as outside its
terms of use and could restrict the key or the account, and a new key may replace an earlier smartwatch key.
Only do it with your own account and car. No BYD password is entered in Home Assistant. Only the key, its number,
its expiry date, the car's address and a random "watch" identity are kept. After that, the integration talks to
your car over Bluetooth only and never contacts BYD again.

Australia is the only region tried on a real car. New Zealand shares its server and South Korea comes from the
upstream project, and both are untested.

## Limits
- The car accepts one Bluetooth connection at a time. This integration connects only for the second or two a
  command takes, so a phone or watch can still use the car in between. If one is connected at that moment, the
  attempt fails before anything is sent, and the cloud fallback (if set) is used.
- Lock state comes from commands (or from a cloud lock, if you set one). If the car is locked by phone, watch or
  fob, Home Assistant does not know unless a cloud lock is set.
- Climate control and engine start are not available over Bluetooth. Use the cloud integration for those.
- Unverified: horn on a real car, long-term reliability, and setup on any model or region other than an Australian
  Sealion 8. Boot open works; there is no verified boot close.
- The integration's icon shows in Home Assistant 2026.3 or newer. Older versions work the same, without it.

## Does it work on your car?
Only an AU Sealion 8 is verified. If you try another model or region, please open a
[model and region report](https://github.com/mjens118/hass-byd-ble/issues/new?template=model-report.yml), whether
it worked or not. Attach the integration's diagnostics (three-dot menu > Download diagnostics): they contain
what the car advertises and the result of the last command, and not your key or address.

## Security
- The key is stored in Home Assistant's config entry storage, so it is in backups. Treat it like a car key.
- Diagnostics redact the key, the address and the code. Nothing logs the key.
- Bluetooth signals can be relayed, so being in range does not prove someone is next to the car.
- To report a security problem, see [SECURITY.md](SECURITY.md).

## More detail

<details>
<summary><b>Entities</b></summary>

- `lock.byd_bluetooth_key`: lock / unlock. State comes from the last acknowledged command. If you picked a
  cloud lock in the options, its state is mirrored in too; without one, state is assumed.
  Attributes: `last_command`, `last_result`, `last_route` (bluetooth or cloud), `door_state_raw`.
  After a lock, the car's door byte is decoded: all closed = locked. A door or the boot open = shown as
  unlocked with `lock_pending: true` and `open_doors` (the car beeps and locks once it is closed; Home
  Assistant does not see that happen). An unrecognised byte = unknown.
- `button`: Flash lights (enabled). Flash lights and horn, Open boot (disabled by default; enable in the
  entity settings).
- `binary_sensor`: In Bluetooth range (with RSSI).
- `sensor`: Key expires (diagnostic, only if the key has an expiry date).

</details>

<details>
<summary><b>Key expiry, re-authentication and replacing the key</b></summary>

If the key has a "key valid to" date, the integration keeps it:
- A diagnostic sensor, **Key expires**, shows the date.
- Home Assistant raises a Repairs warning 14 days before it, and an error once it has passed.
- If the car ever answers that the key is not usable, Home Assistant asks you for a new key (Settings >
  Devices & services shows "Re-authenticate"). Silence from the car, being out of range or another device
  holding the connection never triggers this, only an explicit refusal.

To replace the key at any time: Settings > Devices & services > BYD Bluetooth Key > three-dot menu >
**Reconfigure**, then use a QR code again or paste a new `credentials.json`. The new key is checked against the
car before it replaces the old one. If the old key had an expiry date and the new one has none, the old date
is dropped.

</details>

<details>
<summary><b>Voice assistants and unlocking</b></summary>

Unlocking a car by voice is a different risk from turning on a light. What Home Assistant does with a lock by
default (checked against its source):
- Locks are not exposed to voice assistants by default. You have to expose the lock yourself.
- **Assist** understands "turn off" a lock as unlock, and sends no code.
- **Google Assistant** refuses to unlock unless you have set a secure devices PIN, and asks for it.
- **Alexa**: Home Assistant passes the unlock through with no code of its own. Any voice PIN is on Amazon's side.
- Bluetooth signals can be relayed, and the car is only as safe as whatever can reach Home Assistant.

If you want a guard, set a **code** in the integration options (Configure > "Lock and unlock code", 4 to 12
digits). With a code set, Home Assistant asks for it on every lock and unlock, and anything that does not supply
it, such as Assist or an automation without the code, is refused before anything is sent to the car.
- It cannot protect unlock alone. Home Assistant requires the code for lock as well.
- Automations must include it: `code: "123456"` in the `lock.lock` and `lock.unlock` calls.
- **Do not store it as the lock's "default code"** in the entity settings. Home Assistant would then supply it
  for every caller that sends none, voice assistants included, which defeats the guard.
- Apple Home (HomeKit Bridge) only works with a code-protected lock if you put the code in that bridge's
  `entity_config` for the lock, which keeps the code in your YAML.
- The code is stored in plain text in the config entry, the same as the key, and is redacted from diagnostics.
  It is a guard against accidents and voice, not strong security.

The **Open boot** button is disabled by default and has no code. Think before exposing it to a voice assistant.

</details>

<details>
<summary><b>Cloud state mirroring</b></summary>

If a cloud lock is set in the options, it is used as a state source as well as the fallback:
- At startup, and whenever the cloud lock changes to `locked` or `unlocked`, this lock follows it. So a lock or
  unlock done by phone, watch or fob shows up once the cloud next reports it. How quickly depends on the cloud
  integration's polling interval.
- Cloud changes are ignored for 2 minutes after this integration sends a command, because the cloud can lag
  and report the state from before.
- `unavailable` and `unknown` from the cloud lock are ignored.
- A cloud `locked` also clears a pending "door open" state (the car locks itself once the door is closed).
- Attributes `state_source` (bluetooth, cloud) and `state_updated` show where the current state came from.
- With no cloud lock set, nothing changes: the state is assumed from the last command.

</details>

<details>
<summary><b>How commands behave, and technical notes</b></summary>

- Connects on demand, one command per connection, then disconnects.
- If Bluetooth fails before any command was sent (out of range, connect or auth failure), it uses the cloud
  lock fallback if you set one.
- Once a command has been sent, a timeout, drop or rejection is reported as unknown/failed. It is never
  retried and never re-routed to the cloud. The lock then shows `unknown`.
- If a Bluetooth command is already running, a second one is refused, not queued.
- Only the five commands confirmed on a real car are sent: lock, unlock, open boot, flash lights, and flash
  lights with horn. There is no probing of other commands.
- Door byte after a lock (car off): 0xC0 closed, +0x01 front driver, +0x02 front passenger, +0x04 back
  passenger, +0x08 back driver, +0x10 boot (all observed). With the car on, readings were unreliable.
- Probing the climate command on an AU Sealion 8 got no response from the car, and no start command is known.

</details>

## Development
```
pip install pytest-homeassistant-custom-component
python scripts/install_ha_requirements.py
pytest
```
The tests use a simulated car and need no Bluetooth hardware. The second command installs the extra packages
Home Assistant's Bluetooth component needs, read from the installed Home Assistant so they stay current.

## Credits and licence
The protocol work builds on [BydBleAutoLock](https://github.com/PoorGrammerA/BydBleAutoLock) (MIT). See
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md). This project is MIT licensed, see [LICENSE](LICENSE).
