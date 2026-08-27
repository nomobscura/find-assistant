"""
Tracks per-proxy RSSI for each resolved device and decides which room
("winning" proxy = strongest recent RSSI) it's currently in -- independent
of Bermuda entirely. Native HA entities (sensor.py) subscribe to updates
here directly.

Identity note: every dict/cache here is keyed by a device's derived *id*
(see identity.py), not its display name -- two tracked devices can
legitimately share a name (confirmed live: two real physical tags both
named "UGREEN Finder Pro", and separately two "OTAG" entries in one user's
devices.json), and keying by name alone silently merged them into one
tracked device. `device.name` is still carried on DevicePresence purely for
display (event data, log messages, entity naming).
"""
import logging
import time

from bluetooth_data_tools import monotonic_time_coarse

from homeassistant.components import bluetooth
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr

from .const import (
    CONF_DETECTED_PROXY_ROOMS,
    CONF_PROXY_ROOMS,
    CONF_UPDATE_LOCATION,
    DEFAULT_STALE_SECONDS,
    DOMAIN,
    EVENT_TAG_ENTERED_ROOM,
    EVENT_TAG_LEFT_ROOM,
    EVENT_TAG_SPOTTED_BY_PROXY,
    FULL_SCAN_INTERVAL_SECONDS,
    PRESENCE_UPDATE_INTERVAL_SECONDS,
    SOURCE_DESCRIBE_TTL_SECONDS,
)
from .resolver import IdentityResolver

_LOGGER = logging.getLogger(__name__)

# Sightings from proxies not seen in this long are dropped entirely (memory /
# `candidates` hygiene for proxies that go away permanently). Distinct from
# stale_seconds, which only excludes them from room-picking.
_SIGHTING_PRUNE_FACTOR = 10

# How far past stale_seconds a device still counts as "recently seen" for the
# purpose of escalating the full-cache scan (see sweep_stale's _needs_recovery).
# Generous enough to cover a genuinely-present device whose push callbacks have
# stalled, without keeping the escalation on for devices that have actually left.
_RECOVERY_GRACE_FACTOR = 3


def _describe_source(
    hass: HomeAssistant, source: str, proxy_rooms: dict
) -> tuple[str | None, str | None, str | None, str, str | None]:
    """
    Best-effort description of the real proxy that reported a sighting.
    Returns (room_name, area_id, proxy_device_id, friendly_name, area_name).

    `proxy_rooms` is the caller's already-merged view of room labels for this
    source -- RoomPresenceTracker layers its manual CONF_PROXY_ROOMS
    overrides on top of the auto-detected CONF_DETECTED_PROXY_ROOMS cache
    before calling, so a user-typed override wins over a detected Area name.

    Fields are resolved as follows:

      1. A real HA Area, resolved via one of three Device-registry lookups,
         tried in order (this is HA's own live source of truth for where a
         proxy physically is, and is what populates area_id/area_name):
           a. CONNECTION_BLUETOOTH keyed by the scanner's exact source
              address -- this is HA's *own* "Bluetooth" integration's device
              for the scanner (confirmed by reading
              homeassistant/components/bluetooth/__init__.py's
              async_update_device(): every external scanner gets registered
              this way via the SOURCE_INTEGRATION_DISCOVERY flow, and it even
              inherits its Area from the owning integration's main device via
              via_device_id if it doesn't have one of its own). Only exists
              once that discovery flow has been accepted in Settings ->
              Devices & Services, but when it has, this is the most direct,
              always-correct match -- no MAC-offset guessing needed.
           b. CONNECTION_NETWORK_MAC keyed by the same address -- the owning
              integration's (e.g. ESPHome's) own main device. Only matches
              when that integration doesn't report a *separate* address for
              Bluetooth specifically (verified in ESPHome's case via
              bleak_esphome/connect.py: `source = device_info.bluetooth_mac_address
              or device_info.mac_address`) -- so this resolves some of the
              time, not always.
           c. Name-based match (see below) as a last resort.
      2. proxy_rooms[source] -- the merged label described above. Takes
         priority over #1's Area name for room_name purposes (but not for
         area_id, which is always a real Area id or None).
      3. friendly_name is *always* populated as a last resort, via the
         source's already-registered HA Bluetooth scanner object -- an
         ESPHome proxy's scanner is constructed as
         `BaseHaScanner(source=mac, adapter=device_name)`, so `scanner.name`
         is a human-friendly string built from the ESPHome device's own
         configured name (e.g. "fmd-scanner-living_room (28:84:...)"),
         rather than a bare MAC.

    room_name is (mapped room) or (area name) or (friendly name), in that
    order. area_id is only ever a *real* HA Area id, and proxy_device_id is
    the proxy's own Device-registry entry id. area_name is the *raw*
    freshly-resolved Area name (None if #1 didn't resolve this time),
    returned separately so callers can decide whether to cache it into
    CONF_DETECTED_PROXY_ROOMS.

    NOTE: this hits the scanner/device/area registries every call -- hot-path
    callers must go through RoomPresenceTracker._describe_source_cached(),
    which TTL-caches per source. Only the config flow (rare, interactive)
    calls this directly.
    """
    friendly = source
    scanner_adapter = None
    try:
        scanner = bluetooth.async_scanner_by_source(hass, source)
        if scanner is not None:
            if scanner.name:
                friendly = scanner.name
            scanner_adapter = scanner.adapter  # device_info.name, per bleak_esphome's ESPHomeScanner construction
    except Exception:
        _LOGGER.debug("Could not look up scanner name for source %s", source, exc_info=True)

    area_name = None
    area_id = None
    proxy_device_id = None
    match_method = "none"
    try:
        device_registry = dr.async_get(hass)
        # IMPORTANT: HA's own device_registry._normalize_connections() only lowercases
        # CONNECTION_NETWORK_MAC values -- CONNECTION_BLUETOOTH is stored completely
        # as-is (confirmed by reading device_registry.py's source directly). ESPHome's
        # scanner.source is uppercase, so the "bluetooth" integration's own device for
        # it is registered under the *uppercase* address, unnormalized. Querying with
        # dr.format_mac() (which lowercases) here silently never matched -- confirmed
        # live via debug logging (match_method was "none" despite the target Device
        # genuinely existing with an Area set). Try the address as given, then both
        # cases, since we can't assume every scanner reports the same convention.
        device = None
        for candidate in dict.fromkeys((source, source.upper(), source.lower())):
            device = device_registry.async_get_device(connections={(dr.CONNECTION_BLUETOOTH, candidate)})
            if device is not None:
                match_method = "connection_bluetooth"
                break
        if device is None:
            # CONNECTION_NETWORK_MAC genuinely IS normalized to lowercase by HA, so
            # format_mac() is correct here.
            normalized = dr.format_mac(source)
            device = device_registry.async_get_device(connections={(dr.CONNECTION_NETWORK_MAC, normalized)})
            if device is not None:
                match_method = "connection_network_mac"
        if device is None and scanner_adapter and scanner_adapter != source:
            # Fallback: the CONNECTION_NETWORK_MAC lookup above only works when the
            # ESP32 doesn't report a *separate* bluetooth_mac_address (confirmed --
            # when it does, HA registers the device under its WiFi MAC, which we
            # have no way to derive from the BLE scanner's source address alone).
            # But that same device's registry entry's name is set from the exact
            # same device_info.name that scanner.adapter holds (confirmed from
            # esphome/manager.py's `name=entry_data.friendly_name or entry_data.name`
            # vs. bleak_esphome/connect.py's `name = device_info.name`) -- so match
            # by name instead as a second attempt. This scans the whole registry,
            # which is exactly why hot-path callers must use the TTL cache.
            device = next(
                (d for d in device_registry.devices.values() if d.name == scanner_adapter),
                None,
            )
            if device is not None:
                match_method = "name_match"
        if device is not None:
            proxy_device_id = device.id
            if device.area_id is not None:
                area_registry = ar.async_get(hass)
                area = area_registry.async_get_area(device.area_id)
                if area is not None:
                    area_name, area_id = area.name, device.area_id
    except Exception:
        _LOGGER.exception("Failed to resolve area for proxy source %s", source)

    _LOGGER.debug(
        "_describe_source(%s): match_method=%s proxy_device_id=%s area_id=%s area_name=%s "
        "scanner_adapter=%s friendly=%s",
        source, match_method, proxy_device_id, area_id, area_name, scanner_adapter, friendly,
    )

    # A manual override / cached label WINS over the auto-detected Area name.
    # This matches what the option is called ("Override a proxy's room") and
    # what const.CONF_PROXY_ROOMS documents. It used to be `area_name or
    # mapped_room or friendly`, i.e. Area-first -- which made the override
    # silently do nothing for any proxy that had an HA Area, and (combined
    # with _maybe_persist_detected_room, which then overwrote the stored
    # value) meant a typed-in override was both ignored and erased.
    mapped_room = proxy_rooms.get(source.upper())
    if mapped_room is not None:
        # area_id must agree with the room the caller is actually being told
        # about, since update_location relocates the tag's Device into it.
        # Re-resolve it from the label, whether or not a Device-registry Area
        # was detected above: leaving the detected area_id in place while
        # returning an overridden room_name meant the Location sensor read
        # "Kitchen" while the Device was physically moved into "Utility Room".
        # If the label doesn't name a real Area, area_id becomes None -- the
        # label is then a free-text room with no HA Area to relocate into,
        # which is correct and better than relocating somewhere contradictory.
        override_area_id = None
        try:
            area_registry = ar.async_get(hass)
            for area in area_registry.async_list_areas():
                if area.name.casefold() == mapped_room.casefold():
                    override_area_id = area.id
                    break
        except Exception:
            _LOGGER.debug("Could not match proxy_rooms label '%s' to a real Area", mapped_room, exc_info=True)
        area_id = override_area_id

    room_name = mapped_room or area_name or friendly
    return room_name, area_id, proxy_device_id, friendly, area_name


class DevicePresence:
    """Tracks one device's sightings across all proxies and its current best room."""

    def __init__(self, device_id: str, name: str, kind: str, manufacturer: str | None = None, model: str | None = None):
        self.id = device_id  # unique -- see identity.py. Used for every registry/dict key.
        self.name = name  # display only -- NOT guaranteed unique across devices.
        self.kind = kind
        self.manufacturer = manufacturer  # Google's own label, FMDN-kind only -- see resolver.manufacturer_for
        self.model = model  # Google's own label, FMDN-kind only -- see resolver.model_for
        # real_source -> {"rssi": int, "ts": float, "room_name": str, "area_id": str|None, "proxy_device_id": str|None}
        self.sightings = {}
        self.room = None
        self.last_room = None  # most recent non-None room; persists after the device goes stale/away
        self.area_id = None  # only set when `room` came from a real resolved HA Area, else None
        self.via_device_id = None  # the winning proxy's own Device-registry id, for via_device linking
        self.rssi = None
        self.current_mac = None  # the device's own current advertising address (rotates for fmdn/irk kinds)
        self.synced_mac = None  # last MAC successfully written to the Device registry (see _sync_device_connection)
        self.updated_at = None  # last recompute() tick -- NOT "last seen"; recompute runs every sweep tick
        # regardless of whether this device was actually heard from. last_seen_at (below) is the real
        # "last seen" answer -- only ever set from an actual sighting, in note_sighting() itself.
        self.last_seen_at = None
        # Set only when `room` actually changes value in recompute() (below) -- unlike last_seen_at,
        # which ticks on every genuine sighting even if the device hasn't moved. Kept as a separate
        # field rather than repurposing last_seen_at itself, since sweep_stale()'s staleness/recovery
        # logic genuinely needs "when was this last heard from at all", not "when did it last move".
        self.last_room_change_at = None
        self.winning_source = None  # raw proxy source currently winning, or None -- drives enter/leave events
        self.entities = []  # every sensor.py entity for this device (room, rssi, last_room, current_mac)
        self.primary_entity = None  # the Location sensor specifically, for tagging fired events with an entity_id

    def note_sighting(
        self, real_source: str, rssi: int, address: str,
        room_name: str | None, area_id: str | None, proxy_device_id: str | None,
    ):
        self.last_seen_at = time.time()
        self.sightings[real_source] = {
            # Normalized, never None: recompute() compares these with `>`, and
            # HA's BluetoothServiceInfoBleak.rssi is genuinely Optional (see
            # config_flow.py's MAC picker, which defends against exactly this).
            # A single None-RSSI sighting would otherwise raise TypeError
            # inside recompute() -- swallowed on the advertisement path, but
            # on the sweep tick it aborted the whole pass, starving every
            # device after this one for as long as the sighting was retained.
            # -127 dBm is the floor of the BT spec's reportable range, so an
            # unknown-strength sighting loses to any real reading.
            "rssi": rssi if rssi is not None else -127,
            "ts": self.last_seen_at,
            "room_name": room_name,
            "area_id": area_id,
            "proxy_device_id": proxy_device_id,
        }
        # The advertising address itself doesn't depend on which proxy heard it --
        # track it independent of the room/RSSI winner logic below.
        self.current_mac = address.upper()

    def recompute(self, stale_seconds: int) -> bool:
        """Recompute the winning room from fresh sightings. Returns True if anything changed."""
        now = time.time()

        # Hygiene: drop sightings from proxies not heard from in a long time,
        # so `candidates` doesn't accumulate dead proxies forever.
        prune_cutoff = now - stale_seconds * _SIGHTING_PRUNE_FACTOR
        for src in [s for s, v in self.sightings.items() if v["ts"] < prune_cutoff]:
            del self.sightings[src]

        best_src, best = None, None
        for src, v in self.sightings.items():
            if now - v["ts"] > stale_seconds:
                continue
            if best is None or v["rssi"] > best["rssi"]:
                best_src, best = src, v

        new_room = best["room_name"] if best else None
        new_area_id = best["area_id"] if best else None
        new_via_device_id = best["proxy_device_id"] if best else None
        new_rssi = best["rssi"] if best else None

        room_changed = new_room != self.room
        changed = room_changed or new_rssi != self.rssi
        if room_changed:
            self.last_room_change_at = now
        self.room = new_room
        self.area_id = new_area_id
        self.via_device_id = new_via_device_id
        self.rssi = new_rssi
        self.winning_source = best_src
        self.updated_at = now
        if new_room is not None:
            self.last_room = new_room
        return changed

    @property
    def candidates(self) -> dict:
        return {src: v["rssi"] for src, v in self.sightings.items()}


class RoomPresenceTracker:
    def __init__(self, hass: HomeAssistant, resolver: IdentityResolver, entry, stale_seconds: int = DEFAULT_STALE_SECONDS):
        self.hass = hass
        self.resolver = resolver
        self.entry = entry  # kept for _maybe_persist_detected_room's config-entry updates
        self.stale_seconds = stale_seconds
        # normalize once to {SOURCE_UPPER: "Room Name"} for cheap lookups in _describe_source
        # Manual overrides from the options flow -- authoritative, never
        # written to by auto-detection (see _maybe_persist_detected_room).
        self.proxy_rooms = {m["source"].upper(): m["room"] for m in entry.data.get(CONF_PROXY_ROOMS, [])}
        # Last-known-good auto-detected Area names, cached separately so they
        # can serve as a fallback without clobbering the above.
        self.detected_proxy_rooms = {
            m["source"].upper(): m["room"] for m in entry.data.get(CONF_DETECTED_PROXY_ROOMS, [])
        }
        self.update_location = entry.data.get(CONF_UPDATE_LOCATION, False)  # gates the device-registry mutations
        self.devices = {
            device_id: DevicePresence(
                device_id, resolver.name_for(device_id), resolver.kind_for(device_id),
                manufacturer=resolver.manufacturer_for(device_id), model=resolver.model_for(device_id),
            )
            for device_id in resolver.device_ids
        }
        # source -> (describe tuple, monotonic expiry) -- see _describe_source_cached
        self._source_cache: dict[str, tuple[tuple, float]] = {}
        # tag id -> its own Device-registry entry id (stable for the entry's
        # lifetime; the entry *object* is replaced on every update, but the id
        # isn't) -- avoids an identifier-set lookup per registry sync.
        self._tag_device_registry_ids: dict[str, str] = {}
        self._last_full_scan = 0.0  # monotonic ts of last scan_all_known()

    def _describe_source_cached(self, source: str) -> tuple:
        """TTL-cached _describe_source -- proxy Area/name mappings change on a
        human timescale, so hot paths shouldn't pay registry lookups (worst
        case a full-registry name scan) per advertisement."""
        now = monotonic_time_coarse()
        cached = self._source_cache.get(source)
        if cached is not None and cached[1] > now:
            return cached[0]
        # Manual overrides layered on top of the auto-detected cache, so a
        # user-typed room wins and a detected one is used only as a fallback.
        described = _describe_source(
            self.hass, source, {**self.detected_proxy_rooms, **self.proxy_rooms}
        )
        self._source_cache[source] = (described, now + SOURCE_DESCRIBE_TTL_SECONDS)
        return described

    def catch_up(self) -> None:
        """
        Run once at startup, before sensor.py creates any entities. Without
        this, every device sits at its blank initial state ("not_home") until
        the first sweep tick or push callback -- even if it's demonstrably
        already visible on HA's own Bluetooth advertisement page right now
        (confirmed live: exactly this gap was reported after a restart). Also
        runs after every options reload (e.g. right after adding a device).

        Uses _recompute_and_fire() rather than a bare recompute() -- a real
        bug this exact scenario surfaced: a raw recompute() updates room/rssi
        silently but skips the enter/leave event firing, so a newly-added
        device's first-ever sighting (via catch-up, not a live advertisement)
        populated state correctly but never logged "entered <room>" activity.
        """
        self.scan_all_known()
        self._last_full_scan = monotonic_time_coarse()
        for device_id, device in self.devices.items():
            self._recompute_and_fire(device_id, device)

    def register_entity(self, device_id: str, entity, primary: bool = False):
        # Multiple sensor.py entities (room, rssi, last_room, current_mac) all
        # register against the same tracked device -- each needs its own
        # async_write_ha_state() call on change, since they're separate entities.
        # primary=True (the Location sensor) additionally tags fired enter/leave
        # events with an entity_id so HA's Logbook can link them to it.
        if device_id in self.devices:
            device = self.devices[device_id]
            device.entities.append(entity)
            if primary:
                device.primary_entity = entity

    def unregister_entity(self, device_id: str, entity):
        """Called from sensor.py's async_will_remove_from_hass so a removed
        entity can't linger in the write-state fan-out list."""
        device = self.devices.get(device_id)
        if device is None:
            return
        if entity in device.entities:
            device.entities.remove(entity)
        if device.primary_entity is entity:
            device.primary_entity = None

    def _tag_device_entry(self, device: DevicePresence):
        """The tag's own Device-registry entry, via cached registry id (with
        an identifier-lookup fallback that repopulates the cache)."""
        device_registry = dr.async_get(self.hass)
        cached_registry_id = self._tag_device_registry_ids.get(device.id)
        if cached_registry_id is not None:
            entry = device_registry.async_get(cached_registry_id)
            if entry is not None:
                return entry
            del self._tag_device_registry_ids[device.id]  # registry entry was deleted -- re-resolve
        entry = device_registry.async_get_device(identifiers={(DOMAIN, device.id)})
        if entry is not None:
            self._tag_device_registry_ids[device.id] = entry.id
        return entry

    def _note_sighting(self, device_id: str, device: DevicePresence, source: str, rssi: int, address: str) -> bool:
        """
        Shared by both the live push path (handle_advertisement) and the
        full-cache sweep (scan_all_known). Returns True if current_mac
        changed, so callers can refresh entities even on the rare tick where
        the MAC rotates but room/RSSI happen to come out identical
        (recompute()'s own changed-detection only compares room/RSSI).
        """
        prev_mac = device.current_mac
        is_first_from_this_proxy = source not in device.sightings
        room_name, area_id, proxy_device_id, _friendly, area_name = self._describe_source_cached(source)
        device.note_sighting(source, rssi, address, room_name, area_id, proxy_device_id)
        if area_name is not None:
            self._maybe_persist_detected_room(source, area_name)
        # Deliberately NOT gated by self.update_location -- that flag is about
        # the higher-impact Area relocation. Keeping the tag's own Device
        # Bluetooth connection in sync with its current_mac is a low-stakes,
        # always-useful mutation (same address the current_mac sensor already
        # shows), so it always runs. It gates itself on synced_mac internally,
        # so in steady state this is a single attribute comparison.
        self._sync_device_connection(device_id, device)
        if is_first_from_this_proxy:
            self._fire_proxy_spotted_event(device, source)
        return device.current_mac != prev_mac

    def _fire_proxy_spotted_event(self, device: DevicePresence, source: str) -> None:
        """
        Fires the first time (per HA restart) a given proxy reports seeing a
        given tag -- independent of whether that proxy ends up "winning" the
        room (a tag can be seen by several proxies at once). Targets the
        PROXY's own device_id (see const.EVENT_TAG_SPOTTED_BY_PROXY) so it
        shows up as "<tag> was first spotted nearby" activity on the PROXY's
        own Device Logbook, as distinct from the tag-centric enter/leave
        events.
        """
        proxy_device_id = device.sightings[source]["proxy_device_id"]
        if proxy_device_id is None:
            return  # no resolvable proxy Device entry -- nothing to attach the activity to
        self.hass.bus.async_fire(
            EVENT_TAG_SPOTTED_BY_PROXY,
            {
                "device_id": proxy_device_id,  # the PROXY's device_id -- this is what puts it on ITS Logbook
                "name": device.name,
                "tag_id": device.id,
                "kind": device.kind,
                "tag_entity_id": device.primary_entity.entity_id if device.primary_entity else None,
                "source": source,
            },
        )

    def _maybe_persist_detected_room(self, source: str, area_name: str) -> None:
        """
        Cache the last-known-good auto-detected Area name for a proxy, so it
        survives the Area lookup later being unavailable (e.g. the proxy's
        Device not yet re-registered after a restart).

        Stored under CONF_DETECTED_PROXY_ROOMS, NOT CONF_PROXY_ROOMS. Those
        two used to share one dict, which meant this method -- running on
        every sighting -- would overwrite whatever the user had deliberately
        typed into "Override a proxy's room" the moment a real Area resolved.
        The user's override is now never touched by auto-detection, and wins
        over it in _describe_source.

        Cheap and safe to call on every sighting: async_update_entry() is a
        synchronous @callback (confirmed from HA's own source) that doesn't
        trigger a reload by itself, and this only actually writes when the
        value has changed, so a steady-state device (the common case) causes
        no repeated writes.
        """
        key = source.upper()
        if self.detected_proxy_rooms.get(key) == area_name:
            return
        self.detected_proxy_rooms[key] = area_name
        self._source_cache.pop(source, None)  # description depends on this -- refresh next lookup
        new_list = [{"source": s, "room": r} for s, r in self.detected_proxy_rooms.items()]
        self.hass.config_entries.async_update_entry(
            self.entry, data={**self.entry.data, CONF_DETECTED_PROXY_ROOMS: new_list}
        )

    def handle_advertisement(self, service_info) -> None:
        try:
            self.resolver.maybe_refresh()
            service_data = service_info.service_data or {}
            device_id = self.resolver.resolve(service_info.address, service_data)
            if device_id is None:
                return

            device = self.devices.get(device_id)
            if device is None:
                return  # shouldn't happen -- resolver and self.devices are built from the same lists

            _LOGGER.debug(
                "Find Assistant: matched '%s' [%s] (%s) at address %s via proxy %s at %d dBm",
                device.name, device_id, device.kind,
                service_info.address, service_info.source, service_info.rssi,
            )
            mac_changed = self._note_sighting(
                device_id, device, service_info.source, service_info.rssi, service_info.address
            )
            room_or_rssi_changed = self._recompute_and_fire(device_id, device)
            if room_or_rssi_changed or mac_changed:
                for entity in device.entities:
                    entity.async_write_ha_state()
        except Exception:
            _LOGGER.exception("Error handling advertisement from %s", getattr(service_info, "source", "?"))

    @callback
    def sweep_stale(self, _now=None) -> None:
        """Periodic tick: recompute every device (handles the 'nothing seen in a while -> away' case,
        which handle_advertisement() alone can't catch since it only runs when something IS seen).

        Must be marked @callback: async_track_time_interval() inspects whether its target is a
        HA callback, and if not, dispatches it to a worker thread via async_add_executor_job()
        instead of running it on the event loop. Without this decorator, every tick crashed with
        'calls async_write_ha_state from a thread other than the event loop' the moment a device's
        state actually changed -- confirmed directly from a live HA traceback.

        The FULL cache scan (scan_all_known -- every BLE address HA knows about) only runs
        every FULL_SCAN_INTERVAL_SECONDS in steady state; it exists as a *recovery* mechanism
        for stalled push callbacks, not the primary data path. It also runs on a tick where
        some device plausibly needs rescuing -- see _needs_recovery below."""
        now = monotonic_time_coarse()
        now_wall = time.time()

        def _needs_recovery(d: DevicePresence) -> bool:
            """Whether the full-cache scan should be escalated to THIS tick for
            this device.

            Restricted to devices that were seen recently: the point of the
            escalation is to rescue a device whose push callbacks have stalled
            *while it's still physically around*, before recompute() marks it
            away and causes a spurious not_home blip. A device that's simply
            gone (tag left in a car, keys at the office) can't be rescued by
            re-reading the cache, and must NOT hold the escalation on -- an
            earlier version returned True for any device with no winning
            source at all, so one permanently-absent device pinned the scan
            to every 5s tick instead of every 30s, a sustained 6x cost that
            grows with how many BLE devices are in range.
            """
            if d.last_seen_at is None or now_wall - d.last_seen_at > self.stale_seconds * _RECOVERY_GRACE_FACTOR:
                return False  # long gone, or never seen -- nothing to rescue
            if d.winning_source is None:
                return True
            s = d.sightings.get(d.winning_source)
            # "about to" age out, not "already": gives the scan a chance to
            # refresh from HA's cache before recompute() below marks it away.
            return s is None or now_wall - s["ts"] > self.stale_seconds - PRESENCE_UPDATE_INTERVAL_SECONDS

        if (
            now - self._last_full_scan >= FULL_SCAN_INTERVAL_SECONDS
            or any(_needs_recovery(d) for d in self.devices.values())
        ):
            mac_changed_ids = self.scan_all_known()
            self._last_full_scan = now
        else:
            mac_changed_ids = set()
        for device_id, device in self.devices.items():
            # Per-device try/except: without it, one device raising here (e.g.
            # on malformed sighting data) propagated out of the whole tick,
            # silently starving every device after it in iteration order --
            # every 5s, for as long as the bad data was retained.
            try:
                room_or_rssi_changed = self._recompute_and_fire(device_id, device)
                if room_or_rssi_changed or device_id in mac_changed_ids:
                    for entity in device.entities:
                        entity.async_write_ha_state()
            except Exception:
                _LOGGER.exception("Error updating presence for '%s' [%s]", device.name, device_id)

    def _recompute_and_fire(self, device_id: str, device: DevicePresence) -> bool:
        """
        Shared by handle_advertisement, sweep_stale, and catch_up: recompute
        the winning room, sync the device registry, and fire an enter/leave
        event on hass.bus whenever the *winning proxy itself* changes (not
        just the room name -- two proxies could theoretically share an
        override label) -- see const.EVENT_TAG_ENTERED_ROOM /
        EVENT_TAG_LEFT_ROOM and logbook.py for how these surface as
        "<tag> entered/left <room>" activity on the tag's own Device page.
        """
        prev_source = device.winning_source
        prev_room = device.room
        changed = device.recompute(self.stale_seconds)
        if changed:
            # via_device_id always runs, same reasoning as _sync_device_connection:
            # it's just a "connected devices" link to whichever proxy Device is
            # currently seeing this tag (preferring the Bluetooth integration's own
            # scanner Device -- see _describe_source), not a "location" mutation --
            # setting it doesn't itself move the tag between Areas. Area assignment
            # is the one still gated, since that genuinely does relocate the device.
            self._sync_device_via(device)
            if self.update_location:
                self._sync_device_area(device)
        if device.winning_source != prev_source:
            self._fire_transition_events(device, prev_source, prev_room)
        return changed

    def _fire_transition_events(
        self, device: DevicePresence, prev_source: str | None, prev_room: str | None
    ) -> None:
        entity_id = device.primary_entity.entity_id if device.primary_entity else None
        device_id = None
        try:
            entry = self._tag_device_entry(device)
            if entry is not None:
                device_id = entry.id
        except Exception:
            _LOGGER.exception("Failed to look up device_id for '%s' while firing transition event", device.name)

        base_data = {"name": device.name, "tag_id": device.id, "kind": device.kind,
                      "entity_id": entity_id, "device_id": device_id}

        if prev_source is not None:
            self.hass.bus.async_fire(
                EVENT_TAG_LEFT_ROOM,
                {**base_data, "room": prev_room, "source": prev_source},
            )
        if device.winning_source is not None:
            self.hass.bus.async_fire(
                EVENT_TAG_ENTERED_ROOM,
                {**base_data, "room": device.room, "source": device.winning_source, "rssi": device.rssi},
            )

    def scan_all_known(self) -> set:
        """
        Sweep every advertisement currently in HA's Bluetooth manager cache
        (the same data backing HA's own "Bluetooth" integration's discovered-
        devices/advertisement page) through the resolver, treating any match
        as a fresh sighting. Returns the set of device ids whose current_mac
        changed as a result.

        Purpose: startup/reload catch-up, and recovery when push callbacks
        stall (confirmed live this session: a device's sightings went stale
        far longer than its real advertising interval while an ESPHome-side
        log showed continuous sightings -- our callback had stopped firing
        even though the device was in range). Discovers rotated addresses
        too, since it scans by content rather than by known address.

        A single connectable=False pass is a complete sweep: habluetooth's
        manager keeps `_all_history` (every advertisement) and
        `_connectable_history` (the connectable subset), and
        async_discovered_service_info(connectable=False) returns the former
        -- a strict superset of the connectable pass (confirmed from
        habluetooth's own source), so iterating both just processed every
        connectable device twice.

        `async_discovered_service_info()` returns HA's raw per-address
        history with no freshness filtering built in (entries are only
        pruned on a much longer, unrelated timeout), so age is checked here
        against our own stale_seconds using monotonic_time_coarse() -- the
        exact clock habluetooth stamps BluetoothServiceInfoBleak.time with,
        which isn't comparable to wall-clock time.time().
        """
        self.resolver.maybe_refresh()
        changed_ids = set()
        now = monotonic_time_coarse()
        try:
            infos = bluetooth.async_discovered_service_info(self.hass, False)
        except Exception:
            _LOGGER.exception("Failed to enumerate discovered Bluetooth service info")
            return changed_ids
        for info in infos:
            try:
                if now - info.time > self.stale_seconds:
                    continue
                device_id = self.resolver.resolve(info.address, info.service_data or {})
                if device_id is None:
                    continue
                device = self.devices.get(device_id)
                if device is None:
                    continue
                if self._note_sighting(device_id, device, info.source, info.rssi, info.address):
                    changed_ids.add(device_id)
            except Exception:
                _LOGGER.exception("Error scanning discovered service info for %s", getattr(info, "address", "?"))
        return changed_ids

    def _sync_device_area(self, device: DevicePresence) -> None:
        """
        Only called when self.update_location is True (see CONF_UPDATE_LOCATION).

        Keep the tracked device's own HA Device-registry Area in sync with
        wherever it was last actually located -- so it shows up in the right
        Area's dashboard/card, not just in our own sensor's state.

        Only acts when a *real* Area was resolved for the winning proxy
        (device.area_id is not None); intentionally never clears the area
        when the device goes stale/away, so its last known location sticks
        (matching last_room's "most recent known" semantics) instead of
        popping back to "no area" every time it's briefly out of range.
        """
        if device.area_id is None:
            return
        try:
            entry = self._tag_device_entry(device)
            if entry is not None and entry.area_id != device.area_id:
                dr.async_get(self.hass).async_update_device(entry.id, area_id=device.area_id)
        except Exception:
            _LOGGER.exception("Failed to sync device area for '%s'", device.name)

    def _sync_device_via(self, device: DevicePresence) -> None:
        """
        Always runs (not gated by self.update_location -- see
        CONF_UPDATE_LOCATION and _recompute_and_fire's comment on why).

        Link the tracked device to the proxy that's currently seeing it via
        HA's standard `via_device` relationship (the same mechanism used for
        e.g. Zigbee routers or ESPHome sub-devices) -- so in the HA UI, this
        device shows as "connected via <the winning proxy>" under "Connected
        devices" on its own Device page. Prefers the Bluetooth integration's
        own scanner Device when that resolves (see _describe_source's
        priority order).

        Only acts when a proxy Device-registry entry was actually found
        (device.via_device_id is not None); like the Area sync above, never
        clears it when the device goes stale/away, so the last known proxy
        sticks rather than popping back to "no via_device" every time it's
        briefly out of range.
        """
        if device.via_device_id is None:
            return
        try:
            entry = self._tag_device_entry(device)
            if entry is not None and entry.via_device_id != device.via_device_id:
                dr.async_get(self.hass).async_update_device(entry.id, via_device_id=device.via_device_id)
        except Exception:
            _LOGGER.exception("Failed to sync via_device for '%s'", device.name)

    def _sync_device_connection(self, device_id: str, device: DevicePresence) -> None:
        """
        Always runs on every sighting (not gated by self.update_location --
        keeping the tag's Bluetooth connection in sync with current_mac is a
        much lower-stakes mutation than moving it between Areas), but gates
        itself on device.synced_mac: registry work only happens when the MAC
        actually rotated (or after a previously failed sync, since synced_mac
        is only advanced on success). Tracking the last *successfully synced*
        MAC -- rather than "did it change in memory" -- is what preserves both
        earlier bug fixes (scan-only rediscovery and mid-session backfill)
        while keeping the steady-state cost to one attribute comparison.

        Registers the device's *current* advertising address as a real
        CONNECTION_BLUETOOTH connection on its HA Device-registry entry, so
        the tag cross-references HA's own Bluetooth tooling (diagnostics,
        the advertisement monitor, async_last_service_info, ...) by MAC.

        Replaces rather than accumulates: fmdn/irk addresses rotate every
        ~15 minutes, so merging forever would grow the connection set
        without bound. Only the single most-recent address is kept.
        """
        if device.current_mac is None or device.synced_mac == device.current_mac:
            return
        try:
            entry = self._tag_device_entry(device)
            if entry is None:
                _LOGGER.debug(
                    "_sync_device_connection('%s'): no Device-registry entry found for identifier (%s, %s) yet "
                    "-- it's created at setup (see __init__.py), so this should only happen transiently",
                    device.name, DOMAIN, device_id,
                )
                return  # synced_mac deliberately NOT set -- retry on the next sighting
            new_connection = (dr.CONNECTION_BLUETOOTH, dr.format_mac(device.current_mac))
            if new_connection not in entry.connections:
                kept = {c for c in entry.connections if c[0] != dr.CONNECTION_BLUETOOTH}
                dr.async_get(self.hass).async_update_device(entry.id, new_connections=kept | {new_connection})
                _LOGGER.debug(
                    "_sync_device_connection('%s'): updated CONNECTION_BLUETOOTH to %s", device.name, new_connection[1]
                )
            device.synced_mac = device.current_mac
        except Exception:
            _LOGGER.exception("Failed to sync current MAC connection for '%s'", device.name)
