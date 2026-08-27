# Find Assistant — independent multi-strategy BLE room presence

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="custom_components/find_assistant/dark_logo@2x.png 2x, custom_components/find_assistant/dark_logo.png 1x">
  <img alt="Find Assistant logo" src="custom_components/find_assistant/logo.png" srcset="custom_components/find_assistant/logo@2x.png 2x, custom_components/find_assistant/logo.png 1x" width="128">
</picture>

> Note: Home Assistant's own UI (Settings → Devices & Services, the
> config-flow header, device pages) doesn't read these files -- it only
> shows integration branding it pulls from the external
> [`home-assistant/brands`](https://github.com/home-assistant/brands) repo.
> These are kept here purely as project assets for this README/repo
> (`logo.png`/`logo@2x.png` light, `dark_logo.png`/`dark_logo@2x.png` dark).

A native Home Assistant integration for room-level BLE presence that does
**not** depend on Bermuda at all. Tracks devices identified three
different ways, all feeding one shared engine:

- **FMDN** — Google Find My Device Network EID matching (`identity_key` +
  `pair_date` from `device_lister/list_devices.py`'s `devices.json`), the
  same crypto used throughout this repo.
- **IRK** — standard Bluetooth Core Spec resolvable-private-address
  resolution, for devices that rotate their MAC using a real Identity
  Resolving Key (phones via `private_ble_device`-style IRKs, or
  potentially FMDN LE Audio devices via `account_key` — see
  [`../room_presence/BERMUDA.md`](../room_presence/BERMUDA.md) for why
  that's plausible for LE Audio devices specifically but not locator tags).
- **static MAC** — devices that never rotate their address at all.

Each resolved device gets a `sensor.<name>_room` entity showing whichever
proxy last heard it with the strongest signal (mapped to that proxy's HA
Area where possible), with `rssi`/`candidates`/`kind` as attributes.

## Why build this instead of using `../ha_integration/`

`../ha_integration/` (`fmdn_bermuda_bridge`) works, but getting there
required threading data through Bermuda's undocumented internal
assumptions (its `coordinator.py` discovery sweep, its MAC-casing
handling, its scanner/source model) on top of HA's own Bluetooth API —
real, hard-won fragility documented in that project's README. This project
trades Bermuda's more sophisticated distance/smoothing math for a much
simpler, fully self-contained, fully observable system: no synthetic
advertisement injection, no dependency on a third-party integration's
internals, entities visible immediately in Developer Tools → States.

See the "pros and cons" discussion earlier in this project's history for
the full trade-off reasoning — in short: worth it if you don't need
Bermuda's calibrated distance estimation or a unified dashboard alongside
other Bermuda-tracked things (phones, etc.), since IRK/static-MAC devices
already have perfectly good native HA support (Private BLE Device,
Bermuda's own discovery) that this project doesn't try to replace, only
consolidate under one roof if you want everything in one place.

## Upgrading from "BLE Room Presence" (pre-0.5.0)

As of 0.5.0-beta this integration's domain changed from `ble_room_presence`
to `find_assistant` (folder renamed to match). If you already have this
integration set up and don't run the migration below, Home Assistant will
show it as missing on next restart and every entity/device tied to it will
orphan -- you'd have to remove and re-add it from scratch.

1. **Stop Home Assistant.**
2. Run [`migrate_domain_rename.py`](migrate_domain_rename.py) against your
   config directory (the one containing `.storage/`):
   ```
   python migrate_domain_rename.py --config-dir /path/to/homeassistant/config --dry-run
   python migrate_domain_rename.py --config-dir /path/to/homeassistant/config
   ```
   It rewrites the domain in `core.config_entries`, `core.entity_registry`,
   and `core.device_registry`, backing up each file first
   (`*.pre_find_assistant_migration.bak`). `entity_id`s are left untouched,
   so dashboards, automations, and recorder history keep working.
3. Replace `custom_components/ble_room_presence/` with
   `custom_components/find_assistant/` (delete the old folder, copy in the
   new one -- see Installation below).
4. Restart Home Assistant and confirm devices/entities show correctly
   under the "Find Assistant" integration.

If something looks wrong, restore the three `.bak` files (drop the
`.pre_find_assistant_migration.bak` suffix) and restart again.

## Installation

1. Copy `custom_components/find_assistant/` into your Home Assistant
   config directory (`config/custom_components/find_assistant/`).
2. Restart Home Assistant.
3. **Settings → Devices & Services → Add Integration → "BLE Room
   Presence"** — click Submit (nothing to configure yet).
4. **Settings → Devices & Services → Find Assistant → Configure** to
   add devices:
   - **Import/update FMDN devices.json** — upload or paste the file from
     `device_lister/list_devices.py`.
   - **Add a device by IRK** — name + 32-hex-char (16-byte) IRK.
   - **Add a device by static MAC** — name + `AA:BB:CC:DD:EE:FF`.
   - **Remove a device** — multi-select removal across all kinds.

   Re-open Configure any time to add more devices or update the FMDN list
   (re-importing replaces the FMDN list only; IRK/static-MAC devices are
   unaffected).

No ESPHome changes needed beyond what [`../esp_scanner/`](../esp_scanner/)
already provides — plain `bluetooth_proxy:`, nothing FMDN-specific
required on the device side, since all matching happens here in Python.

## Verified vs. not yet tested

**Verified without a live HA instance** (as much as possible standalone):
- All three resolution strategies (`resolver.py`) tested against real
  FMDN data plus synthetic IRK and static-MAC devices, including negative
  controls (wrong IRK, garbage EID correctly rejected).
- The IRK round-trip specifically: implemented the standard Bluetooth
  `ah()` function independently to generate a known-good resolvable
  address, confirmed `bluetooth-data-tools`'s `resolve_private_address`
  accepts it and rejects a wrong IRK — both the byte-ordering convention
  and the library's correctness are confirmed, not assumed.
- The presence engine (`presence.py`): strongest-RSSI-wins room picking
  across multiple simulated proxies, and staleness-based "away" detection,
  both tested directly.
- `bluetooth.async_register_callback`'s connectable-default and
  replay-cache gotchas are already accounted for (same fixes as
  `../ha_integration/`, confirmed there against live HA).

**Not yet tested against live HA** (this is all new, unlike
`../ha_integration/`'s core matching/injection logic which is
live-confirmed):
- The config flow and options flow in full (menu navigation, file
  upload, entry reload after each change) — built following documented,
  verified HA APIs, but never run end-to-end in a real instance.
- `presence.py`'s Area lookup (`device_registry.async_get_device(connections={(CONNECTION_BLUETOOTH, source)})`)
  — the `CONNECTION_BLUETOOTH` constant and `connections` parameter shape
  are confirmed correct from HA's source, but whether ESPHome's device
  registry entry actually registers a Bluetooth connection keyed by the
  *scanner's* address (as opposed to its WiFi MAC) specifically is
  unconfirmed. If it doesn't resolve, the code degrades gracefully —
  `room` falls back to showing the raw proxy source string instead of a
  pretty Area name, rather than breaking.
- The `sensor.py` entity platform itself (device registry linkage,
  `unique_id` stability, state updates via `async_write_ha_state()`).

If the Area lookup doesn't pan out on your HA version, that's the first
thing to investigate — everything else has a much more direct path to
diagnosing (the same debug-logging approach used throughout this session
works identically here).
