"""
Button entities per configured device -- currently just Ring/Stop Ringing,
grouped under the same per-device HA Device as sensor.py's entities.

Only created for KIND_FMDN devices: ringing (see ring.py) needs the tag's
identity_key to authenticate the command, which only FMDN-kind devices have
on file (IRK/static-MAC devices were never linked to a Google account).
"""
import logging

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_call_later

from .const import DOMAIN, KIND_FMDN
from .presence import RoomPresenceTracker
from .ring import RING_TIMEOUT_SECONDS, ring_device, stop_ring_device

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback):
    tracker: RoomPresenceTracker = hass.data[DOMAIN][entry.entry_id]
    entities = [
        DeviceRingButton(tracker, device_id)
        for device_id, device in tracker.devices.items()
        if device.kind == KIND_FMDN
    ]
    async_add_entities(entities)


class DeviceRingButton(ButtonEntity):
    """A single button that toggles between "Ring" and "Stop Ringing".

    is_ringing is purely local UI state on this entity -- nothing else in
    the integration needs to know a tag is ringing, so it isn't threaded
    through DevicePresence/RoomPresenceTracker. It's best-effort, not a live
    guarantee: ring.py's ring/stop calls return a *confirmed* ringing state
    when the tag supports the FMDN status notification (see ring.py's module
    docstring), but even then, nothing keeps watching after this entity's
    own connection to the tag closes. If the tag stops on its own (its own
    RING_TIMEOUT_SECONDS timeout, its own physical button, another owner's
    GATT request) without this entity being told to stop it, this button
    would otherwise be stuck showing "Stop Ringing" forever -- the
    self-cancelling timer below (set to the same timeout we ask the tag to
    use) is what keeps that from happening.
    """

    _attr_should_poll = False
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, tracker: RoomPresenceTracker, device_id: str):
        self._tracker = tracker
        self._device_id = device_id
        self._is_ringing = False
        self._remove_auto_reset = None
        device = tracker.devices[device_id]
        display_name = device.name
        self._display_name = display_name
        self._attr_unique_id = f"{DOMAIN}_{device_id}_ring"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, device_id)},
            name=display_name,
            manufacturer=device.manufacturer,
            model=device.model,
        )

    @property
    def _device(self):
        return self._tracker.devices[self._device_id]

    @property
    def name(self) -> str:
        return f"{self._display_name} {'Stop Ringing' if self._is_ringing else 'Ring'}"

    @property
    def icon(self) -> str:
        return "mdi:bell-off-outline" if self._is_ringing else "mdi:bell-ring-outline"

    @property
    def available(self) -> bool:
        # Ringing needs a live GATT connection to the tag's *current*
        # advertising address -- with no known current_mac at all there's
        # nothing to connect to, so don't offer a button that can only ever
        # fail. (Still offered while the device shows "not_home" -- a stale
        # room/rssi doesn't necessarily mean current_mac is stale too, and
        # even if the actual connect attempt fails, that's a clearer signal
        # than a button that's unavailable for a reason that isn't obvious.)
        return bool(self._device.current_mac)

    async def async_added_to_hass(self) -> None:
        # Register with the tracker like sensor.py's entities do, so this
        # button's state gets rewritten whenever the device is seen. Without
        # it, `available`/`name`/`icon` above are only ever evaluated at
        # creation time and on a local press: a tag that wasn't in range at
        # setup would show permanently greyed-out even once it appeared,
        # since nothing would ever re-read current_mac.
        #
        # primary=False -- the Location sensor stays the entity that fired
        # enter/leave events are attributed to.
        self._tracker.register_entity(self._device_id, self)

    async def async_will_remove_from_hass(self) -> None:
        self._tracker.unregister_entity(self._device_id, self)
        self._cancel_auto_reset()

    def _cancel_auto_reset(self) -> None:
        if self._remove_auto_reset is not None:
            self._remove_auto_reset()
            self._remove_auto_reset = None

    def _set_ringing(self, is_ringing: bool) -> None:
        self._cancel_auto_reset()
        self._is_ringing = is_ringing
        if is_ringing:
            self._remove_auto_reset = async_call_later(self.hass, RING_TIMEOUT_SECONDS, self._auto_reset)
        self.async_write_ha_state()

    @callback
    def _auto_reset(self, _now) -> None:
        # @callback is REQUIRED, not cosmetic: async_call_later inspects its
        # target, and a plain sync function gets dispatched to an executor
        # thread instead of the event loop -- where async_write_ha_state()
        # raises "calls async_write_ha_state from a thread other than the
        # event loop" (the identical bug, with the same symptom, is
        # documented on presence.py's sweep_stale). Without the decorator
        # this timer -- the thing that stops the button getting stuck on
        # "Stop Ringing" -- silently failed every time it fired.
        self._remove_auto_reset = None
        self._is_ringing = False
        self.async_write_ha_state()

    async def async_press(self) -> None:
        device = self._device
        if not device.current_mac:
            raise HomeAssistantError(f"No known current address for '{device.name}' -- it hasn't been seen recently.")

        identity_key_hex = self._tracker.resolver.identity_key_for(self._device_id)
        if identity_key_hex is None:
            raise HomeAssistantError(f"No identity_key on file for '{device.name}'.")
        identity_key = bytes.fromhex(identity_key_hex)

        if self._is_ringing:
            try:
                await stop_ring_device(self._tracker.hass, device.current_mac, identity_key)
            except Exception as err:
                _LOGGER.exception("Failed to stop ringing '%s'", device.name)
                raise HomeAssistantError(f"Failed to stop ringing '{device.name}': {err}") from err
            self._set_ringing(False)
        else:
            try:
                await ring_device(self._tracker.hass, device.current_mac, identity_key)
            except Exception as err:
                _LOGGER.exception("Failed to ring '%s'", device.name)
                raise HomeAssistantError(f"Failed to ring '{device.name}': {err}") from err
            self._set_ringing(True)
