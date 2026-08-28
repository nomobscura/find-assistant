DOMAIN = "find_assistant"

# --- Config entry data keys ---
# Each is a list of device dicts, discriminated by kind:
#   fmdn:       {"name": str, "identity_key": <64-hex>, "pair_date": int}
#   irk:        {"name": str, "irk": <32-hex>}
#   static_mac: {"name": str, "mac": "AA:BB:CC:DD:EE:FF"}
CONF_FMDN_DEVICES = "fmdn_devices"
CONF_IRK_DEVICES = "irk_devices"
CONF_STATIC_MAC_DEVICES = "static_mac_devices"
#   smarttag:   {"name": str, "encryption_key": <base64>, "privacy_id_seed": <base64>,
#                "pool_size": int, "iv": <base64>} -- see smarttag/privacy_id.py.
# No cloud sync path for these yet (unlike fmdn's Google account linking) --
# imported once via config_flow.py's import_smarttag step. See
# smarttag/__init__.py's module docstring for why.
CONF_SMARTTAG_DEVICES = "smarttag_devices"

# List of {"source": "AA:BB:CC:DD:EE:FF", "room": "Living Room"} -- lets you give a
# proxy's *actual room* as its display name instead of whatever HA's automatic
# Area lookup or the proxy's own device name resolves to (which can be unreliable
# or just technical-looking, e.g. "fmd-scanner-living_room (AA:BB:CC:DD:EE:FF)").
# Takes priority over both the automatic Area lookup and the raw scanner name
# (see presence._describe_source, which layers this on top of the detected
# cache below).
CONF_PROXY_ROOMS = "proxy_rooms"

# Same shape, but written by the integration itself rather than the user: a
# last-known-good cache of each proxy's auto-detected HA Area name, so that
# survives the Area lookup being briefly unavailable (e.g. right after a
# restart, before the proxy's own Device re-registers). Kept strictly
# separate from CONF_PROXY_ROOMS: when these shared one key, auto-detection
# overwrote deliberate user overrides on the very next sighting.
CONF_DETECTED_PROXY_ROOMS = "detected_proxy_rooms"

# List of {"source": "AA:BB:CC:DD:EE:FF", "offset": -8} -- a per-proxy dBm
# adjustment applied to every RSSI reading from that proxy before it's stored/
# compared in recompute()'s "strongest RSSI wins" room picking. Different
# proxy hardware has genuinely different radio sensitivity (e.g. a Shelly's
# antenna vs an ESP32's), so two proxies equidistant from the same device can
# report meaningfully different raw RSSI -- biasing room selection toward
# whichever proxy's radio just happens to run "hotter", not whichever one is
# actually closer. A positive offset makes that proxy's readings win more
# often; negative makes them win less. 0/absent means no adjustment.
CONF_PROXY_RSSI_OFFSETS = "proxy_rssi_offsets"

# Opt-in: when True, also assign each tracked device's own HA Device-registry
# entry to the Area of whichever proxy is currently winning. Off by default --
# this is the one mutation that actually *relocates* the device, so it stays
# opt-in.
#
# NOT covered by this flag (both always run regardless):
#   - the tag's CONNECTION_BLUETOOTH, kept in sync with current_mac -- it's
#     the same address the current_mac sensor already always shows.
#   - the tag's via_device_id link to the winning proxy's own Device (shows
#     as "Connected devices" in the UI) -- it's just a link, doesn't itself
#     move the tag between Areas.
# Gating either of those behind this flag added friction without protecting
# against anything meaningful.
CONF_UPDATE_LOCATION = "update_location"

# Google Find My account auto-sync (alternative to one-time devices.json
# upload -- see config_flow.py's sync_google_account/sync_google_now steps
# and google_findmy/ for the vendored account/API client).
#
# CONF_GOOGLE_SECRETS: the uploaded secrets.json, stored verbatim. Treated
# as read-only/opaque here -- nothing in this integration mutates it, it's
# only ever handed to google_findmy.GoogleFindMySession.
CONF_GOOGLE_SECRETS = "google_secrets"
# Hours between automatic resyncs; 0 disables the periodic timer (manual
# "Sync Google account now" still works either way).
CONF_GOOGLE_SYNC_INTERVAL_HOURS = "google_sync_interval_hours"
DEFAULT_GOOGLE_SYNC_INTERVAL_HOURS = 12

KIND_FMDN = "fmdn"
KIND_IRK = "irk"
KIND_STATIC_MAC = "static_mac"
KIND_SMARTTAG = "smarttag"

# Fired on hass.bus whenever a tracked device's *winning proxy* changes --
# i.e. it's now considered to be near a different proxy than before (ENTERED),
# or it's gone stale/away entirely (LEFT, fired for the proxy it was last at).
# Consumed by logbook.py so these show up as readable "<tag> entered/left
# <room>" activity in that tag's own Device Logbook, not just as a generic
# sensor-state-changed record.
EVENT_TAG_ENTERED_ROOM = f"{DOMAIN}_entered_room"
EVENT_TAG_LEFT_ROOM = f"{DOMAIN}_left_room"

# Fired on hass.bus the first time (per HA restart) a given proxy reports a
# sighting of a given tag -- carries the *proxy's* device_id (not the tag's),
# so it shows up as "<tag> was first spotted nearby" activity on the PROXY's
# own Device Logbook, confirmed against HA's actual logbook query source
# (homeassistant/components/logbook/queries/devices.py filters purely by a
# `device_id` key in the raw event data -- one id per event, hence a
# separate event type from EVENT_TAG_ENTERED_ROOM/LEFT_ROOM above, which
# target the tag's device_id instead).
EVENT_TAG_SPOTTED_BY_PROXY = f"{DOMAIN}_tag_spotted"

# Same UUIDs used throughout HA-FindMy.
FMDN_SERVICE_UUID = "0000fcaf-0000-1000-8000-00805f9b34fb"
EDDYSTONE_SERVICE_UUID = "0000feaa-0000-1000-8000-00805f9b34fb"

# Samsung SmartTag control-service UUID -- carries the 20-byte service data
# payload whose bytes 4:12 are the rotating Privacy ID (see
# smarttag/privacy_id.py and https://github.com/KieronQuinn/uTag/wiki/BLE-Service-Data).
SMARTTAG_SERVICE_UUID = "0000fd5a-0000-1000-8000-00805f9b34fb"

# How often to recompute FMDN EID windows (clock advances, so the window
# needs to be periodically re-centered on "now"). Keep in sync with
# ble_scanner/scan_devices.py's REFRESH_SECONDS.
EID_REFRESH_SECONDS = 300

# +/- windows (~2 hours) of clock-skew tolerance for FMDN EID matching.
EID_WINDOWS = 7

# How often to recompute each device's winning room and check for staleness.
PRESENCE_UPDATE_INTERVAL_SECONDS = 5

# The staleness sweep only re-scans HA's FULL discovered-advertisements cache
# (scan_all_known -- every BLE address in range, not just tracked ones) this
# often in steady state; the 5s tick otherwise just recomputes staleness from
# already-collected sightings, which is nearly free. Exception: while any
# tracked device currently has no winning proxy at all, the full scan runs
# every tick so recovery from a stalled push callback stays fast.
FULL_SCAN_INTERVAL_SECONDS = 30

# How long a proxy's resolved description (Area/device-id/name) is cached
# before the device/area registries are consulted again. Proxy->Area mappings
# change on a human timescale (someone edits it in the UI), so 60s staleness
# is imperceptible, while the cache removes several registry lookups -- worst
# case a linear scan of the whole device registry (the name-match fallback) --
# from EVERY processed advertisement.
SOURCE_DESCRIBE_TTL_SECONDS = 60

# How long a sighting stays valid for room-picking before being treated as stale.
# FMDN tags don't advertise continuously as far as HA is concerned -- confirmed
# against a live ESPHome fmd_validate log where the same tag's confirmed sightings
# were naturally 15-90+ seconds apart (BLE advertising interval aside, esp_scanner's
# own validation logging and/or the proxy's forwarding cadence coalesce repeats).
# The original 30s default was shorter than routinely-observed gaps, so devices
# were flipping to "not_home" for several seconds between completely normal
# sightings. 150s gives real margin above the largest observed gap (~90s) while
# still going "away" reasonably promptly if a tag is actually no longer in range.
DEFAULT_STALE_SECONDS = 150
