# Changelog

All notable changes to this project are listed here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/). While the version is below 1.0, anything can change.

## [0.3.0] - 2026-11-08

First public release.

### Added
- **Set up with a QR code, inside Home Assistant.** Home Assistant asks BYD's servers for a key (presenting itself
  as a Samsung smartwatch, as the upstream project does), shows a QR code, and you approve it in the official BYD
  app. It sits behind an explicit "I understand" step, works for new setups, re-authentication and
  Reconfigure, keeps the key and lets you retry if the car does not answer, and saves nothing until the key has
  worked on the car. Pasting a `credentials.json` stays available as an advanced option. Australia is the only
  region tried on a real car; New Zealand and South Korea are listed as untested.
- Key expiry: the "key valid to" date from provisioning is stored, shown in a diagnostic **Key expires**
  sensor, and raises a Repairs warning 14 days before and an error after it passes.
- Re-authentication: if the car explicitly refuses the key, Home Assistant asks for a new one. Silence,
  out of range or a busy car never triggers this.
- **Reconfigure** option to replace the key at any time, checked against the car before it is saved.
- Optional lock and unlock code, to refuse callers that send none (voice assistants, careless automations).
- Diagnostics now include what the car advertises and the result of the last command, to help with
  "does it work on my model" reports. The key, the code and the address stay redacted.
- Known-answer tests for the frame codec (RFC 4493 and upstream vectors), and a test suite that runs on
  Home Assistant 2025.1 and the latest release.
- Model and region report issue template.

### Fixed
- When BYD approves a QR code but sends no key, the message now points at the likely cause: the account's
  authorisation does not include Bluetooth key. On a real car that gave an empty answer, and adding it under
  Authorisation details in the main account fixed it with no other change. The setup notice and README say so
  up front.
- Diagnostics no longer contain the car's address. A downloaded file from a real car showed it in two places
  that the first redaction missed: Home Assistant's discovery keys, and the advertised manufacturer ID (the
  car uses two bytes of its own address for it). Both are now removed, and a test covers each.

### Changed
- Options changes reload through the options flow on Home Assistant 2025.8 and later. The update listener
  it replaces is deprecated and stops working in Home Assistant 2026.12 (older versions keep the listener).
- Entity names and icons use translation keys. Existing entity IDs and unique IDs are unchanged.
- Reloading on an options change now ignores other config entry updates, so replacing the key reloads once.
- A corrupt stored key now asks for a new one instead of failing on the first command.
- A car that does not expose the expected Bluetooth characteristic is reported as a connection error
  before anything is sent.

### Fixed
- CI installed too few packages for Home Assistant's Bluetooth component, so the test workflow could not
  have passed. It now reads them from the installed Home Assistant.
- `THIRD_PARTY_NOTICES.md` now carries the upstream copyright line and licence text, and `codec.py`
  names its source.

## [0.2.0]

Internal, not released. Lock, unlock, flash, horn and open boot over Bluetooth; assumed state with
optional cloud lock mirroring and fallback; in-range sensor; diagnostics with the key redacted.
