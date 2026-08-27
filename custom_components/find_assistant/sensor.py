"""
Sensor entities per configured device, all grouped under that device's own
HA Device entry (so they show as siblings on the device's page, e.g.
Settings -> Devices & Services -> Find Assistant -> "Mail Key"):

  - sensor.<name>_room        primary entity, displayed as "<name> Location": current room, or "not_home"
  - sensor.<name>_rssi        diagnostic: RSSI at the currently-winning proxy
  - sensor.<name>_last_room   diagnostic, displayed as "<name> Last Location": most recent known room (persists through "not_home")
  - sensor.<name>_current_mac diagnostic: device's current advertising address
  - sensor.<name>_last_seen   diagnostic, displayed as "<name> Last Location Change": timestamp
                                      of the last time this device's room actually changed
                                      (not every sighting -- see DeviceLastSeenSensor)
  - sensor.<name>_irk         diagnostic: the configured IRK, IRK-kind devices only

Entity_id/unique_id suffixes ("_room"/"_last_room"/"_last_seen") are unchanged from before this
was renamed -- only the displayed friendly name changed ("Room" -> "Location", "Last Seen" ->
"Last Location Change"). HA generates
entity_id from the friendly name only once, at first creation; it's frozen in the entity
registry after that and never silently changes even when the underlying `_attr_name` this
code reports does (confirmed against HA's own entity_registry behavior) -- so this rename is
safe for existing installs: dashboards/automations referencing sensor.<name>_room keep
working, only the label shown in the UI updates (on next reload/restart).

Identity note: entities are keyed off each device's derived *id* (see
identity.py), not its display name -- two tracked devices can share a name
(e.g. two physical tags both labeled "OTAG"), and each still gets its own
independent set of entities/Device-registry entry. `unique_id` and
`DeviceInfo.identifiers` use the id directly (no slugify needed -- it's
already a short alphanumeric token); `DeviceInfo.name`/entity `_attr_name`
use the device's display name for what you actually see, which HA does not
require to be unique across Devices.

Note on the IRK sensor: its value is the plaintext secret you configured for
that device. It's genuinely useful for confirming which IRK is tied to which
device, but it's still a real entity state -- it'll show up in Developer
Tools/History like any other sensor, and in Recorder's database unless you
explicitly exclude it there. If that's a concern, exclude
`sensor.<name>_irk` via Settings -> System -> ... -> Recorder, or the
`recorder: exclude:` YAML config.

RSSI/last-room/current-MAC used to be attributes bundled on the Location sensor
alone -- promoted to their own entities so they're individually usable in
dashboards/automations/history graphs instead of needing templates to reach
them, and so they visibly belong to the *device* rather than being hidden
inside one entity's state.

Only slow-changing metadata (`kind`, `area_id`) stays as attributes on the
Location sensor -- see its extra_state_attributes for why anything that
changes per-advertisement must NOT live there. Per-proxy `candidates` is an
attribute of the RSSI diagnostic sensor instead.

Entities are created once at setup for every configured device (FMDN, IRK,
or static MAC) -- state starts as "unknown" until the first sighting.
"""
from datetime import datetime, timezone

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import SIGNAL_STRENGTH_DECIBELS_MILLIWATT, EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, KIND_IRK
from .presence import RoomPresenceTracker


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback):
    tracker: RoomPresenceTracker = hass.data[DOMAIN][entry.entry_id]

    entities = []
    for device_id, device in tracker.devices.items():
        entities.append(DevicePresenceSensor(tracker, device_id))
        entities.append(DeviceRssiSensor(tracker, device_id))
        entities.append(DeviceLastRoomSensor(tracker, device_id))
        entities.append(DeviceCurrentMacSensor(tracker, device_id))
        entities.append(DeviceLastSeenSensor(tracker, device_id))
        if device.kind == KIND_IRK:
            irk = tracker.resolver.irk_for(device_id)
            if irk is not None:
                entities.append(DeviceIrkSensor(tracker, device_id, irk))
    async_add_entities(entities)


class _BaseDeviceSensor(SensorEntity):
    """Shared device-linking boilerplate -- every subclass's entity is
    grouped under the same per-tracked-device HA Device via identical
    device_info, so they all show up together on that device's page."""

    _attr_should_poll = False
    _primary = False  # overridden True on DevicePresenceSensor -- see register_entity()

    def __init__(self, tracker: RoomPresenceTracker, device_id: str, suffix: str, display_suffix: str):
        self._tracker = tracker
        self._device_id = device_id
        device = tracker.devices[device_id]
        display_name = device.name
        self._attr_name = f"{display_name} {display_suffix}"
        self._attr_unique_id = f"{DOMAIN}_{device_id}_{suffix}"
        self._attr_device_info = DeviceInfo(
            identifiers=tracker.device_identifiers(device_id),
            name=display_name,
            manufacturer=device.manufacturer,
            model=device.model,
        )

    @property
    def _device(self):
        return self._tracker.devices[self._device_id]

    async def async_added_to_hass(self) -> None:
        # Every sensor for this device registers itself -- presence.py writes
        # state to *all* of a device's registered entities on each change,
        # not just one (see RoomPresenceTracker.register_entity). primary=True
        # (Room only) additionally tags fired enter/leave events with this
        # entity_id so HA's Logbook can associate them with it.
        self._tracker.register_entity(self._device_id, self, primary=self._primary)

    async def async_will_remove_from_hass(self) -> None:
        # Mirror of the registration above -- without this, an entity HA
        # removes mid-lifetime would linger in the tracker's write-state
        # fan-out list as a stale reference.
        self._tracker.unregister_entity(self._device_id, self)


class DevicePresenceSensor(_BaseDeviceSensor):
    _attr_icon = "mdi:map-marker-radius"
    _primary = True

    def __init__(self, tracker: RoomPresenceTracker, device_id: str):
        # "room" (2nd arg) feeds unique_id/entity_id -- unchanged so existing
        # entity_ids/automations keep working. "Location" (3rd arg) is just
        # the displayed name.
        super().__init__(tracker, device_id, "room", "Location")

    @property
    def native_value(self):
        return self._device.room or "not_home"

    @property
    def extra_state_attributes(self):
        device = self._device
        # Deliberately only slow-changing values here. `candidates` (per-proxy
        # RSSI) and `updated_at` (stamped by every recompute(), i.e. every
        # advertisement AND every 5s sweep tick) used to live on this entity
        # too -- which meant this sensor's attributes differed on essentially
        # every advertisement, so HA could never take its cheap
        # state-reported path and instead emitted a full state_changed: a new
        # State object, a websocket push to every open frontend, and a
        # recorder states+state_attributes row. At ~10 tags x 3 proxies that
        # was ~30 events/sec and millions of recorder rows a day, on the one
        # entity users actually put on dashboards -- and it made this
        # sensor's own History view useless, since every point was a
        # "change". `candidates` now lives on the RSSI diagnostic sensor
        # (where per-advertisement churn is expected and wanted) and
        # last-seen has its own dedicated timestamp entity.
        return {
            "kind": device.kind,
            "area_id": device.area_id,  # set only when a real HA Area was resolved (see presence.py)
        }


class DeviceRssiSensor(_BaseDeviceSensor):
    _attr_icon = "mdi:signal"
    _attr_device_class = SensorDeviceClass.SIGNAL_STRENGTH
    _attr_native_unit_of_measurement = SIGNAL_STRENGTH_DECIBELS_MILLIWATT
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, tracker: RoomPresenceTracker, device_id: str):
        super().__init__(tracker, device_id, "rssi", "RSSI")

    @property
    def native_value(self):
        return self._device.rssi  # None while away, matching the Location sensor's "not_home"

    @property
    def extra_state_attributes(self):
        # Per-proxy RSSI lives here rather than on the Location sensor: this
        # entity's own state already changes on every advertisement, so the
        # extra churn is free, whereas on Location it forced a recorder write
        # per advertisement (see DevicePresenceSensor.extra_state_attributes).
        return {"candidates": self._device.candidates}


class DeviceLastRoomSensor(_BaseDeviceSensor):
    _attr_icon = "mdi:history"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, tracker: RoomPresenceTracker, device_id: str):
        super().__init__(tracker, device_id, "last_room", "Last Location")

    @property
    def native_value(self):
        return self._device.last_room  # most recent non-None room; persists through "not_home"


class DeviceLastSeenSensor(_BaseDeviceSensor):
    """The last time this device's current room actually changed -- NOT
    every time it was heard from (a device sitting still in range still
    gets sighted repeatedly; this only ticks on an actual move between
    rooms, or the very first time it's ever placed in one).

    Distinct from DevicePresence.updated_at, which is stamped by every
    recompute() tick (every 5s regardless of sightings), and from
    last_seen_at, which IS "heard from at all" (still used internally by
    sweep_stale()'s staleness/recovery logic, just not shown here anymore
    -- see DevicePresence.last_room_change_at). TIMESTAMP device class
    gets HA's automatic "5 minutes ago"-style relative rendering for
    free, and is usable directly in automations/history unlike a buried
    attribute.

    Entity_id/unique_id suffix ("_last_seen") is unchanged despite the
    renamed/repurposed display -- same reasoning as the "Room" ->
    "Location" rename: HA freezes entity_id from the name only at first
    creation, so changing it here doesn't break existing dashboards/
    automations, only the label shown in the UI updates."""

    _attr_icon = "mdi:clock-outline"
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, tracker: RoomPresenceTracker, device_id: str):
        super().__init__(tracker, device_id, "last_seen", "Last Location Change")

    @property
    def native_value(self):
        ts = self._device.last_room_change_at
        return datetime.fromtimestamp(ts, tz=timezone.utc) if ts is not None else None


class DeviceCurrentMacSensor(_BaseDeviceSensor):
    _attr_icon = "mdi:bluetooth"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, tracker: RoomPresenceTracker, device_id: str):
        super().__init__(tracker, device_id, "current_mac", "Current MAC")

    @property
    def native_value(self):
        return self._device.current_mac


class DeviceIrkSensor(_BaseDeviceSensor):
    """Only created for KIND_IRK devices (see async_setup_entry above). The
    IRK is configured, not derived -- it never changes at runtime, so this
    just echoes back the value resolver.py resolved this device's cipher
    from, for easy visual confirmation of which IRK is tied to which device."""

    _attr_icon = "mdi:key"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, tracker: RoomPresenceTracker, device_id: str, irk: str):
        super().__init__(tracker, device_id, "irk", "IRK")
        self._irk = irk

    @property
    def native_value(self):
        return self._irk
