# Find Assistant — Home Assistant integration for BLE room presence

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="custom_components/find_assistant/brand/dark_logo@2x.png 2x, custom_components/find_assistant/brand/dark_logo.png 1x">
  <img alt="Find Assistant logo" src="custom_components/find_assistant/brand/logo.png" srcset="custom_components/find_assistant/brand/logo@2x.png 2x, custom_components/find_assistant/brand/logo.png 1x" width="128">
</picture>

A custom Home Assistant integration for room-level Bluetooth presence
tracking — no Bermuda dependency, no third-party app, no cloud polling
beyond an occasional Google account sync. Works with any number of
ESPHome `bluetooth_proxy` devices, one per room.

Tracks devices identified three different ways, all feeding one shared
engine:

- **FMDN** — Google Find My Device Network EID matching, for Google
  Find My-compatible tags (Pebblebee, Chipolo, etc.).
- **IRK** — standard Bluetooth Core Spec resolvable-private-address
  resolution, for devices with a known Identity Resolving Key (e.g.
  phones).
- **Static MAC** — devices that never rotate their Bluetooth address.

## Capabilities

- **Room-level presence** — each device gets a `sensor.<name>_room`
  entity (displayed as "*Location*") showing whichever proxy last heard
  it with the strongest signal, mapped to that proxy's HA Area where
  possible.
- **Google Find My account sync** — link an account once and FMDN
  devices stay in sync automatically (interval configurable, manual
  resync always available). Devices are only ever added or updated by
  sync, never silently removed.
- **Manual device management** — add a device by IRK or static MAC,
  remove devices, all through the integration's Configure menu — no
  YAML editing required.
- **Ring the tag** — a per-device button that rings a physical FMDN tag
  over Bluetooth through your ESPHome proxy (requires
  `bluetooth_proxy: active: true` on that proxy, since ringing needs an
  active GATT connection, not just passive scanning).
- **Last known location** — a diagnostic sensor with a Google Maps link
  to the device's most recent location report, independent of its
  current room/away state.
- **Manufacturer/model** — automatically populated on the device page
  for devices synced from a Google account.
- **Per-proxy Area override** — rename what room a specific proxy
  reports without needing to rename its Home Assistant Area.
- **Auto-generating dashboard view** — a Lovelace strategy that groups
  trackers by current room automatically (no static YAML to keep in
  sync as devices come and go), with a separate "Away" section
  including a map. See [Dashboard](#dashboard) below. A static
  alternative, [`dashboards/tag_locations.yaml`](dashboards/tag_locations.yaml),
  is also included.

Diagnostic entities per device: last known room (persists through
"away"), signal strength (RSSI) and nearby-proxy candidates, current
advertising MAC address, last-seen timestamp, and the configured IRK
(IRK devices only).

## Installation

**Prerequisite for Google Find My account sync**: this integration only
*reads* an already-authenticated session — it can't perform Google's
interactive login itself. Before linking an account (or importing a
devices.json), run
[GoogleFindMyTools](https://github.com/leonboe1/GoogleFindMyTools) once
on a separate machine/environment to complete that login; it produces a
`secrets.json` containing the cached credentials (`aas_token`,
`owner_key`, etc.) that this integration's "Link/relink a Google Find
My account" step expects you to upload. Not needed if you're only using
IRK or static-MAC devices.

1. Copy `custom_components/find_assistant/` into your Home Assistant
   config directory (`config/custom_components/find_assistant/`).
2. Restart Home Assistant.
3. **Settings → Devices & Services → Add Integration → "Find
   Assistant"** — click Submit (nothing to configure yet).
4. **Settings → Devices & Services → Find Assistant → Configure** to
   add devices:
   - **Link/relink a Google Find My account** — sync FMDN devices
     automatically (recommended; needs `secrets.json` from
     GoogleFindMyTools, see prerequisite above).
   - **Import/update FMDN devices.json** — one-time/manual alternative
     if you'd rather not link an account. Generate it with
     [google-findmy-device-lister](https://github.com/nomobscura/google-findmy-device-lister),
     a small drop-in add-on for GoogleFindMyTools that exports
     `{name, identity_key, pair_date, account_key, manufacturer, model}`
     for every device on your account.
   - **Add a device by IRK** — name + 32-hex-char (16-byte) IRK.
   - **Add a device by static MAC** — name + `AA:BB:CC:DD:EE:FF`.
   - **Remove a device** — multi-select removal across all kinds.

No FMDN-specific firmware needed on the proxy side — plain ESPHome
`bluetooth_proxy:` is enough, since all matching happens here in
Python.

## Dashboard

The integration ships a Lovelace **view strategy** — a small JS module
that builds the dashboard dynamically from whatever trackers currently
exist, instead of a static YAML file you have to keep updating by hand.

It groups trackers by their current room. Trackers that have never
been seen at all are left out entirely. Each tracker shown gets a
**Last Seen** row and, if it has one, a **Ring** button. Trackers that
are currently away get their own section instead of a room, with an
additional **Last Known Location** row (a Google Maps link) and a
shared map plotting every away tracker that has coordinates.

**One-time setup:**

1. **Settings → Dashboards → ⋮ (top right) → Resources → Add Resource.**
   - URL: `/find_assistant_static/find-assistant-strategy.js`
   - Resource type: **JavaScript Module**
2. Open (or create) a dashboard, **Edit Dashboard → ⋮ → Edit in YAML**,
   and add a view using the strategy:
   ```yaml
   views:
     - strategy:
         type: custom:find-assistant-trackers
         title: Trackers        # optional, defaults to "Trackers"
         away_title: Away       # optional, defaults to "Away"
   ```

That's it — the view regenerates itself from live entities/devices
every time it's opened, so newly added or removed trackers just show
up correctly without touching the dashboard config again.

## Known Issues

- **Phones aren't synced automatically.** Google's account-sync API
  doesn't expose a usable key for phones, so they never show up via
  "Link/relink a Google Find My account." If you have the phone's own
  Bluetooth IRK, add it manually via "Add a device by IRK."
- **LE Audio devices (earbuds/headphones) aren't supported at all**,
  manually or otherwise. Google's FMDN spec has these rotate their
  address via ordinary Bluetooth pairing (SMP) with a phone rather than
  `account_key` — a secret Google's API never exposes, so there's no
  key this integration can use for them under any path.
- **Some third-party tags report an implausible pair date** (seen with
  OTAG-branded tags specifically, reporting mid-1970s), a firmware bug
  in that brand's Fast Pair implementation — this can degrade
  EID-matching reliability for those specific tags.
- **Google account re-linking may be needed occasionally.** The cached
  credential used for sync can't be refreshed headlessly from inside
  Home Assistant; if Google invalidates it, you'll need to redo the
  login flow externally and re-upload a fresh `secrets.json` via
  "Link/relink a Google Find My account."
- **Area mapping depends on your ESPHome proxy registering a Bluetooth
  connection under its scanner address.** If your setup doesn't expose
  that, a device's room falls back to showing the raw proxy identifier
  instead of a friendly Area name — tracking still works, it's just
  less pretty.

## Vibe-coded — how it was validated

This integration was built conversationally with Claude (Anthropic)
rather than hand-written line by line. Before anything was called done:
every change was tested with scenario-based unit tests against stubbed
Home Assistant internals (identity resolution, presence engine, config
flow), a dedicated review pass checked for data-loss, threading, and
credential-handling issues, and the result was confirmed working
against a real Home Assistant instance with real trackers. Still —
read the code before trusting it with your home.
