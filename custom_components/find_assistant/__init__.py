"""
Find Assistant -- independent of Bermuda.

Tracks devices identified three different ways (FMDN EID, classic
Bluetooth IRK, or a fixed/static MAC) across multiple ESPHome
bluetooth_proxy-equipped rooms, and exposes a native sensor.<device>_room
entity per device showing whichever proxy last heard it strongest.

Unlike ../ha_integration/ (fmdn_bermuda_bridge), this does not depend on
Bermuda at all -- no synthetic advertisement injection, no reliance on
another integration's internal scanner/device model. The only externally
undocumented-behavior dependency is HA's own Bluetooth subscription API
(the connectable-default and replay-cache gotchas noted below, both
confirmed by testing this exact code shape in ../ha_integration/ against a
live instance this session).
"""
import json
import logging
import re
from datetime import timedelta
from pathlib import Path

from homeassistant.components.frontend import add_extra_js_url
from homeassistant.components.http import StaticPathConfig

try:
    from homeassistant.components.bluetooth import BluetoothScanningMode
except ImportError:  # moved to a separate package on some HA versions
    from habluetooth import BluetoothScanningMode

try:
    from homeassistant.components.bluetooth import BluetoothCallbackReplay
except ImportError:  # moved to a separate package on some HA versions
    from habluetooth import BluetoothCallbackReplay

from homeassistant.components import bluetooth
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.event import async_track_time_interval

from .const import (
    CONF_FMDN_DEVICES,
    CONF_GOOGLE_SECRETS,
    CONF_GOOGLE_SYNC_INTERVAL_HOURS,
    CONF_IRK_DEVICES,
    CONF_STATIC_MAC_DEVICES,
    DEFAULT_GOOGLE_SYNC_INTERVAL_HOURS,
    DOMAIN,
    PRESENCE_UPDATE_INTERVAL_SECONDS,
)
from .google_findmy import GoogleFindMySession
from .identity import merge_fmdn_devices, merge_irk_candidates
from .presence import RoomPresenceTracker
from .resolver import IdentityResolver

PLATFORMS = ["sensor", "button"]
_LOGGER = logging.getLogger(__name__)

# identity.compute_id's output shape: _ID_LENGTH (16) lowercase hex chars.
_CURRENT_SCHEME_ID_RE = re.compile(r"[0-9a-f]{16}\Z")  # \Z, not $: $ also matches before a trailing newline


def _is_current_scheme_id(identifier: str) -> bool:
    """True if this Device-registry identifier was minted by the current
    compute_id() scheme, rather than the retired slugified-display-name one
    (which produced human-readable values like "mail_key")."""
    return bool(_CURRENT_SCHEME_ID_RE.fullmatch(identifier))


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    fmdn_devices = entry.data.get(CONF_FMDN_DEVICES, [])
    irk_devices = entry.data.get(CONF_IRK_DEVICES, [])
    static_mac_devices = entry.data.get(CONF_STATIC_MAC_DEVICES, [])
    resolver = await hass.async_add_executor_job(
        IdentityResolver, fmdn_devices, irk_devices, static_mac_devices,
    )
    _LOGGER.debug(
        "IdentityResolver built from %d FMDN + %d IRK + %d static-MAC entries -> "
        "%d unique device ids",
        len(fmdn_devices), len(irk_devices), len(static_mac_devices), len(resolver.device_ids),
    )
    tracker = RoomPresenceTracker(hass, resolver, entry)
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = tracker

    # Serve this integration's optional Lovelace strategy JS (groups trackers
    # by room in a dashboard view, no static YAML to maintain by hand -- see
    # www/find-assistant-strategy.js), and register it as an extra frontend
    # module so it loads on every page automatically -- no manual "Add
    # Resource" step in Settings -> Dashboards -> Resources required.
    #
    # URL path is deliberately "/find_assistant/static/..." (a literal
    # "static" path SEGMENT), not "/find_assistant_static/..." -- confirmed
    # live (a real user hit this) that HA's frontend service worker has a
    # fast, dedicated CacheFirst route for any path containing a "/static/"
    # segment (see frontend's service-worker.ts), separate from the much
    # slower generic StaleWhileRevalidate catch-all every other path falls
    # into. "/find_assistant_static/" doesn't contain that segment (it's
    # "static" glued onto "find_assistant" as one path component, not a
    # separate one) and fell into the slow catch-all -- which on at least
    # one real setup measured minutes, not seconds, to resolve, blowing
    # right through Lovelace's fixed 5-second strategy-load timeout.
    #
    # add_extra_js_url() is idempotent (backed by a frozenset), but the
    # static-path registration below isn't -- aiohttp rejects registering the
    # same url_path twice -- so both are guarded with a hass.data flag since
    # async_setup_entry can re-run on reload (e.g. after a Google sync
    # detects device-list changes).
    if not hass.data.get(f"{DOMAIN}_www_registered"):
        await hass.http.async_register_static_paths(
            [StaticPathConfig(
                f"/{DOMAIN}/static",
                str(Path(__file__).parent / "www"),
                cache_headers=False,
            )]
        )
        add_extra_js_url(hass, f"/{DOMAIN}/static/find-assistant-strategy.js")
        hass.data[f"{DOMAIN}_www_registered"] = True

    # Pre-create each tracked device's HA Device-registry entry *before*
    # catch_up() below -- normally that entry only gets created by sensor.py's
    # DeviceInfo when entities are set up, which happens *after* catch_up()
    # (deliberately, so entities render with correct initial state). Without
    # this, a device's very first-ever sighting -- if it happens via catch_up()
    # rather than a later live advertisement, e.g. right after adding a brand
    # new device -- can't attach a device_id to its "entered <room>" activity
    # event, since the Device it'd point to doesn't exist yet. get_or_create()
    # is a no-op for devices that already exist from a prior load.
    #
    # Keyed by device.id (derived from identity_key/irk/mac -- see identity.py),
    # not display name, so two devices sharing a name each still get their own
    # Device-registry entry instead of colliding on one.
    device_registry = dr.async_get(hass)
    for device_id, device in tracker.devices.items():
        device_registry.async_get_or_create(
            config_entry_id=entry.entry_id,
            identifiers={(DOMAIN, device_id)},
            name=device.name,
            manufacturer=device.manufacturer,
            model=device.model,
        )

    # Remove Device-registry entries left over from the pre-refactor
    # identifier scheme (device ids used to be slugified display names;
    # they're now derived from identity_key/irk/mac -- see identity.py).
    # Migration was deliberately not attempted, so without this those old
    # entries -- and their sensor.* entities -- would linger forever as
    # orphans that reload() never revisits.
    #
    # Deliberately NARROW: only entries whose identifier doesn't even look
    # like a current-scheme id (16 lowercase hex chars) are removed. This
    # used to prune anything not in the current device list, which turned
    # any shrinking of that list into cascading deletion of real devices,
    # their entities, and their history -- including from an unattended
    # Google sync (see identity.merge_fmdn_devices for the other half of
    # that fix). Deleting a still-configured device now only ever happens
    # through the explicit "Remove a device" options flow, which removes
    # the registry entry itself.
    for stale_entry in dr.async_entries_for_config_entry(device_registry, entry.entry_id):
        our_ids = {ident[1] for ident in stale_entry.identifiers if ident[0] == DOMAIN}
        if our_ids and not any(_is_current_scheme_id(i) for i in our_ids):
            _LOGGER.info(
                "Removing device-registry entry %s (%s) -- uses the retired pre-refactor id scheme",
                stale_entry.id, stale_entry.name,
            )
            device_registry.async_remove_device(stale_entry.id)

    # Catch up immediately on anything HA already knows about (e.g. still
    # visible on HA's own Bluetooth advertisement page from before this
    # integration finished setting up) -- done *before* forwarding to
    # sensor.py below so entities are created with correct initial state
    # instead of sitting at "not_home" until the first sweep tick or push
    # callback happens to fire.
    tracker.catch_up()

    @callback
    def _handle_advertisement(service_info, change) -> None:
        tracker.handle_advertisement(service_info)

    # A single {"connectable": False} registration receives EVERY advertisement,
    # connectable or not -- confirmed from HA's own match.py, where the only
    # connectable filter is `if matcher.get(CONNECTABLE, True) and not
    # service_info.connectable: return False`, i.e. a False matcher never
    # rejects anything. (match_dict=None would default CONNECTABLE to True and
    # silently drop non-connectable beacons -- the original bug found by live
    # testing in ../ha_integration/. A second {"connectable": True} registration,
    # which we briefly carried defensively, was fully redundant and processed
    # every connectable advertisement twice.)
    #
    # replay=DISABLED: the default OLDEST_FIRST replays cached advertisement
    # history immediately at registration time, which can race a scanner's own
    # post-restart reconnection. We only want live sightings -- catch_up()
    # above already consumed the cache deliberately.
    entry.async_on_unload(
        bluetooth.async_register_callback(
            hass, _handle_advertisement, {"connectable": False}, BluetoothScanningMode.PASSIVE,
            replay=BluetoothCallbackReplay.DISABLED,
        )
    )

    # Periodic sweep so a device that's simply stopped being seen gets marked
    # "not_home" -- handle_advertisement() alone only fires when something IS seen.
    remove_interval = async_track_time_interval(
        hass, tracker.sweep_stale, timedelta(seconds=PRESENCE_UPDATE_INTERVAL_SECONDS)
    )
    entry.async_on_unload(remove_interval)

    # Optional: sync from a linked Google Find My account (see
    # config_flow.py's sync_google_account/sync_google_now steps, and
    # google_findmy/ for the vendored account/API client). Updates device
    # list (CONF_FMDN_DEVICES/CONF_IRK_DEVICES) -- only ever adds/updates,
    # never removes just because a sync returned a shorter list (deliberate
    # removal always stays a manual "Remove a device" action), and only
    # triggers a reload when something actually changed, so a healthy
    # account with nothing new to report doesn't churn every tracked
    # device's entities on every tick. Runs once immediately on every
    # setup/reload (as a background task, so a slow/failed network call
    # never blocks startup) rather than only ever firing on the timer.
    if entry.data.get(CONF_GOOGLE_SECRETS):

        async def _google_sync(_now=None) -> None:
            current_entry = hass.config_entries.async_get_entry(entry.entry_id)
            if current_entry is None:
                return
            secrets = current_entry.data.get(CONF_GOOGLE_SECRETS)
            if not secrets:
                return

            # Covers the merge/update work below as well as the fetch itself,
            # not just the network call: this runs as an unattended background
            # task, so anything escaping here would die silently with no
            # repair issue raised -- the one failure mode the user would have
            # no way to notice. The reachable case is a malformed entry in the
            # *synced* response (missing identity_key), which makes
            # merge_fmdn_devices raise KeyError.
            try:
                fmdn_devices, unmatched = await hass.async_add_executor_job(
                    GoogleFindMySession(secrets).list_devices
                )
                await _apply_google_sync(current_entry, fmdn_devices, unmatched)
            except Exception:
                _LOGGER.exception("Google account sync failed")
                ir.async_create_issue(
                    hass, DOMAIN, "google_sync_failed",
                    is_fixable=False, severity=ir.IssueSeverity.WARNING,
                    translation_key="google_sync_failed",
                )
                return

            ir.async_delete_issue(hass, DOMAIN, "google_sync_failed")

        async def _apply_google_sync(current_entry, fmdn_devices, unmatched) -> None:
            """Everything after a successful fetch: merge the device lists,
            and reload only if the stored lists actually changed. Split out
            from _google_sync purely so its caller's try/except covers this
            work too."""
            # Devices with no usable identity_key (typically phones) but a
            # usable account_key are auto-added/updated as IRK devices --
            # same merge helper and caveats as config_flow.py's interactive
            # sync steps (see identity.merge_irk_candidates and
            # room_presence/BERMUDA.md). LE Audio devices (headphones/
            # earbuds) are excluded from fmdn_devices entirely before this
            # point -- see google_findmy/session.py for why (confirmed live
            # that account_key doesn't work as their IRK either).
            new_irk_devices, added_irk_names = merge_irk_candidates(
                current_entry.data.get(CONF_IRK_DEVICES, []), unmatched
            )
            for name in added_irk_names:
                _LOGGER.info("Google account sync added/updated '%s' as an IRK device", name)

            # Add/update-only merge -- never drops a stored device just
            # because this response didn't include it. See
            # identity.merge_fmdn_devices for why that matters (silent
            # cascading deletion of devices/entities/history on a transient
            # partial API response).
            new_fmdn_devices, changed_fmdn_names = merge_fmdn_devices(
                current_entry.data.get(CONF_FMDN_DEVICES, []), fmdn_devices
            )
            for name in changed_fmdn_names:
                _LOGGER.info("Google account sync added/updated FMDN device '%s'", name)

            # Order-independent comparison -- Google doesn't guarantee
            # response ordering is stable call-to-call.
            def _signature(devices):
                return {json.dumps(d, sort_keys=True) for d in devices}

            unchanged = (
                _signature(current_entry.data.get(CONF_FMDN_DEVICES, [])) == _signature(new_fmdn_devices)
                and _signature(current_entry.data.get(CONF_IRK_DEVICES, [])) == _signature(new_irk_devices)
            )
            if unchanged:
                _LOGGER.debug("Google account sync: no device-list changes")
                return

            _LOGGER.info("Google account sync found device-list changes -- reloading")
            data = dict(current_entry.data)
            data[CONF_FMDN_DEVICES] = new_fmdn_devices
            data[CONF_IRK_DEVICES] = new_irk_devices
            hass.config_entries.async_update_entry(current_entry, data=data)
            # Deliberately NOT awaited on the entry's own background task (the
            # one-shot initial sync is entry.async_create_background_task-tracked,
            # specifically so HA cancels it if some *other* reload happens
            # mid-fetch -- see that call site's comment). Awaiting a reload of
            # THIS SAME entry from within a task tied to THIS SAME entry's
            # unload lifecycle is a real deadlock, confirmed live: the reload's
            # own unload phase tries to cancel every background task tied to
            # the entry, including the one currently suspended waiting for
            # that very reload to finish -- the entry got stuck in
            # ConfigEntryState.UNLOAD_IN_PROGRESS forever, and every later
            # reload attempt (including the user's own manual "sync now")
            # failed with OperationNotAllowed. hass.async_create_task() is
            # explicitly independent of any config entry's lifecycle, so the
            # reload can proceed without racing its own trigger's cancellation.
            hass.async_create_task(
                hass.config_entries.async_reload(current_entry.entry_id),
                name=f"{DOMAIN}_google_sync_reload",
            )

        # Tied to the config entry so HA cancels it on unload -- an untracked
        # hass.async_create_task() could otherwise complete after a reload
        # and issue a second, spurious update+reload on top of whatever the
        # user's action already triggered.
        entry.async_create_background_task(
            hass, _google_sync(), name=f"{DOMAIN}_initial_google_sync"
        )

        sync_interval_hours = entry.data.get(CONF_GOOGLE_SYNC_INTERVAL_HOURS, DEFAULT_GOOGLE_SYNC_INTERVAL_HOURS)
        if sync_interval_hours > 0:
            remove_google_sync_interval = async_track_time_interval(
                hass, _google_sync, timedelta(hours=sync_interval_hours)
            )
            entry.async_on_unload(remove_google_sync_interval)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    _LOGGER.info("Find Assistant watching for %d device(s)", len(resolver.device_ids))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        hass.data[DOMAIN].pop(entry.entry_id, None)
    return unloaded
