"""
Config + options flow for Find Assistant.

Initial setup just creates an (initially empty) entry -- all device
management (importing an FMDN devices.json, adding IRK devices, adding
static-MAC devices, removing devices) happens via the options flow
(Settings -> Devices & Services -> Find Assistant -> Configure), which
can be re-opened any time to add more devices of any kind.
"""
import json
import logging
import re
import time

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.components import bluetooth
from homeassistant.components.file_upload import process_uploaded_file
from homeassistant.core import callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import selector

from .const import (
    CONF_DETECTED_PROXY_ROOMS,
    CONF_FMDN_DEVICES,
    CONF_GOOGLE_SECRETS,
    CONF_GOOGLE_SYNC_INTERVAL_HOURS,
    CONF_IRK_DEVICES,
    CONF_PROXY_ROOMS,
    CONF_PROXY_RSSI_OFFSETS,
    CONF_STATIC_MAC_DEVICES,
    CONF_UPDATE_LOCATION,
    DEFAULT_GOOGLE_SYNC_INTERVAL_HOURS,
    DOMAIN,
    KIND_FMDN,
    KIND_IRK,
    KIND_STATIC_MAC,
)
from .google_findmy import GoogleFindMySession, validate_secrets
from .identity import compute_id, merge_irk_candidates
from .presence import _describe_source

_LOGGER = logging.getLogger(__name__)

CONF_DEVICES_FILE = "devices_json_file"
CONF_DEVICES_TEXT = "devices_json_text"
CONF_SECRETS_FILE = "secrets_json_file"
CONF_SECRETS_TEXT = "secrets_json_text"
CONF_NAME = "name"
CONF_IRK = "irk"
CONF_MAC = "mac"
CONF_REMOVE_NAMES = "remove_names"
CONF_PROXY_SOURCE = "source"
CONF_ROOM_NAME = "room"
CONF_REMOVE_PROXY_SOURCES = "remove_sources"
CONF_RSSI_OFFSET = "offset"

_MAC_HEX_RE = re.compile(r"^[0-9A-Fa-f]{12}$")
_FMDN_REQUIRED_KEYS = ("name", "identity_key", "pair_date")

# Identity (dict keys, entity unique_ids, Device-registry identifiers) is
# derived from each device's own cryptographic/addressing material -- see
# identity.py -- not from its display name. That's what makes everything
# below safe: two devices can share a name (two tags both labeled "OTAG", two
# "UGREEN Finder Pro" tags, etc.) without colliding, so there's no more
# duplicate-*name* rejection or interactive rename flow to worry about here.
# The only thing that's still genuinely invalid is two entries sharing the
# same identity *material* (e.g. the same FMDN identity_key) -- that's not
# two devices with a coincidentally equal label, it's the same physical
# device listed twice, and _parse_and_validate_fmdn below rejects that.


def _strip_hex_delimiters(raw: str) -> str:
    """Accept MAC/IRK input with colons, dashes, spaces, or no delimiter at
    all interchangeably -- e.g. "AA:BB:CC:DD:EE:FF", "AA BB CC DD EE FF",
    "AA-BB-CC-DD-EE-FF", and "AABBCCDDEEFF" all normalize the same way."""
    return re.sub(r"[:\-\s]", "", raw)


def _parse_static_mac(raw: str) -> str | None:
    """Returns the canonical AA:BB:CC:DD:EE:FF form, or None if invalid."""
    cleaned = _strip_hex_delimiters(raw).upper()
    if not _MAC_HEX_RE.match(cleaned):
        return None
    return ":".join(cleaned[i:i + 2] for i in range(0, 12, 2))


def _parse_irk(raw: str) -> str | None:
    """Returns the canonical 32-hex-char uppercase form, or None if invalid."""
    cleaned = _strip_hex_delimiters(raw).upper()
    try:
        irk_bytes = bytes.fromhex(cleaned)
    except ValueError:
        return None
    if len(irk_bytes) != 16:
        return None
    return cleaned


def _parse_and_validate_fmdn(raw: str) -> list:
    """Structural validation, plus a check for duplicate identity_key values.
    Duplicate *names* are deliberately allowed (see module docstring) -- but
    two entries sharing the exact same identity_key can't be two different
    physical devices, since that key IS the device's cryptographic identity.
    That's malformed input (most likely a copy-paste mistake in devices.json),
    so it's still rejected here."""
    devices = json.loads(raw)
    if not isinstance(devices, list):
        raise ValueError("devices.json must be a JSON array of device objects")
    if not devices:
        raise ValueError("devices.json is empty -- no devices to track")
    seen_keys = set()
    for device in devices:
        if not isinstance(device, dict):
            raise ValueError(f"Expected a device object, got: {device!r}")
        missing = [k for k in _FMDN_REQUIRED_KEYS if k not in device]
        if missing:
            raise ValueError(f"Device entry missing {missing}: {device}")
        # Type checks, not just presence. A hand-edited devices.json with
        # "pair_date": "1700000000" (string) used to validate fine here and
        # then fail much later inside the EID window computation, where the
        # broad except only logged -- the device was silently never tracked
        # and an ERROR traceback repeated every 300s forever. A non-str
        # identity_key made bytes.fromhex raise TypeError, which the caller's
        # `except (json.JSONDecodeError, ValueError)` didn't catch, so the
        # user got HA's generic "Unknown error occurred" instead of the
        # invalid_json message that exists for exactly this.
        if not isinstance(device["identity_key"], str):
            raise ValueError(
                f"'{device.get('name')}' has a non-string identity_key "
                f"({type(device['identity_key']).__name__}) -- expected 64 hex characters"
            )
        if not isinstance(device["pair_date"], int) or isinstance(device["pair_date"], bool):
            raise ValueError(
                f"'{device.get('name')}' has a non-integer pair_date "
                f"({device['pair_date']!r}) -- expected a Unix timestamp"
            )
        key_bytes = bytes.fromhex(device["identity_key"])
        if len(key_bytes) != 32:
            raise ValueError(f"'{device.get('name')}' has a {len(key_bytes)}-byte identity_key, expected 32")
        key_upper = device["identity_key"].upper()
        if key_upper in seen_keys:
            raise ValueError(
                f"'{device.get('name')}' has the same identity_key as another entry -- "
                "same physical device listed twice?"
            )
        seen_keys.add(key_upper)
    return devices


def _all_devices_with_ids(data: dict) -> list:
    """Every configured device across all three kinds as (id, name) pairs, in
    a stable order -- used to build the "Remove a device" pick list."""
    result = []
    for kind, key in (
        (KIND_FMDN, CONF_FMDN_DEVICES),
        (KIND_IRK, CONF_IRK_DEVICES),
        (KIND_STATIC_MAC, CONF_STATIC_MAC_DEVICES),
    ):
        for d in data.get(key, []):
            result.append((compute_id(kind, d), d["name"]))
    return result


def _format_relative_time(seconds_ago: float) -> str:
    if seconds_ago < 60:
        return "just now"
    minutes = int(seconds_ago // 60)
    if minutes < 60:
        return f"{minutes}m ago"
    hours = minutes // 60
    if hours < 24:
        return f"{hours}h ago"
    days = hours // 24
    return f"{days}d ago"


def _remove_dropped_device_registry_entries(hass, old_data: dict, new_data: dict) -> None:
    """Remove the HA Device-registry entry (and, by cascade, every entity
    under it) for any device present in old_data but not in new_data.

    Needed because a few options-flow steps replace a whole device list
    wholesale rather than removing entries one at a time -- "Import FMDN
    devices.json" and the interactive Google-account sync both do. Those are
    user-initiated actions where mirroring the supplied list exactly is the
    expected behavior, but without this the dropped devices' registry
    entries and entities would linger forever with no way to clear them:
    __init__.py's startup cleanup is deliberately narrow (it only reaps
    entries using the retired pre-refactor id scheme) precisely so that an
    unattended sync can't cascade into deleting real devices.
    """
    dropped = {device_id for device_id, _name in _all_devices_with_ids(old_data)} - {
        device_id for device_id, _name in _all_devices_with_ids(new_data)
    }
    if not dropped:
        return
    device_registry = dr.async_get(hass)
    for device_id in dropped:
        entry = device_registry.async_get_device(identifiers={(DOMAIN, device_id)})
        if entry is not None:
            _LOGGER.info(
                "Removing device-registry entry for %s -- no longer in the configured device list",
                device_id,
            )
            device_registry.async_remove_device(entry.id)


def _device_status_suffix(hass, entry_id: str, device_id: str) -> str:
    """Returns " -- <location>, seen <relative time>" for a device, read live from
    the running tracker (see presence.py) -- or "" if the integration isn't
    currently loaded/running (e.g. mid-setup) or the device isn't found
    there, so the caller's label just falls back to whatever it already had.
    Live location/last-seen only, never persisted."""
    tracker = hass.data.get(DOMAIN, {}).get(entry_id)
    if tracker is None:
        return ""
    device = tracker.devices.get(device_id)
    if device is None:
        return ""
    location = device.room or "Away"
    seen = "never seen" if device.last_seen_at is None else _format_relative_time(time.time() - device.last_seen_at)
    return f" -- {location}, seen {seen}"


class BleRoomPresenceConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input=None):
        # Single-instance-ish by convention: nothing meaningful to configure yet,
        # devices are all added afterward via the options flow.
        if self._async_current_entries():
            return self.async_abort(reason="already_configured")

        if user_input is not None:
            return self.async_create_entry(
                title="Find Assistant",
                data={
                    CONF_FMDN_DEVICES: [],
                    CONF_IRK_DEVICES: [],
                    CONF_STATIC_MAC_DEVICES: [],
                    CONF_PROXY_ROOMS: [],
                    CONF_UPDATE_LOCATION: False,
                },
            )

        return self.async_show_form(step_id="user", data_schema=vol.Schema({}))

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        # config_entry isn't available until after __init__ on newer HA
        # versions (it's a read-only property derived from self.handler,
        # populated by the framework once the flow is registered) -- don't
        # pass or store it ourselves.
        return BleRoomPresenceOptionsFlow()


class BleRoomPresenceOptionsFlow(config_entries.OptionsFlow):
    # Newer HA versions expose config_entry as a read-only property that the
    # base OptionsFlow class populates automatically -- don't set it ourselves.

    async def _save(self, data: dict):
        # Every options-flow step funnels through here, so this is the one
        # place that reliably sees both the old and new device lists -- which
        # makes it the right place to reap registry entries for devices this
        # change dropped. A no-op for the steps that only append/update, and
        # idempotent for "Remove a device", which already removed them.
        _remove_dropped_device_registry_entries(self.hass, self.config_entry.data, data)
        self.hass.config_entries.async_update_entry(self.config_entry, data=data)
        await self.hass.config_entries.async_reload(self.config_entry.entry_id)
        return self.async_create_entry(title="", data={})

    async def async_step_init(self, user_input=None):
        menu_options = [
            "import_fmdn",
            "sync_google_account",
        ]
        # Only offer "sync now" once an account is actually linked -- nothing
        # useful for it to do before then.
        if self.config_entry.data.get(CONF_GOOGLE_SECRETS):
            menu_options.append("sync_google_now")
        menu_options += [
            "add_irk",
            "add_static_mac",
            "remove_device",
            "map_proxy_room",
            "remove_proxy_room",
            "set_proxy_rssi_offset",
            "settings",
        ]
        return self.async_show_menu(step_id="init", menu_options=menu_options)

    async def async_step_import_fmdn(self, user_input=None):
        errors = {}

        if user_input is not None:
            raw = None
            if user_input.get(CONF_DEVICES_FILE):
                try:
                    with process_uploaded_file(self.hass, user_input[CONF_DEVICES_FILE]) as file_path:
                        raw = await self.hass.async_add_executor_job(file_path.read_text, "utf-8")
                except Exception:
                    _LOGGER.exception("Failed to read uploaded devices.json")
                    errors["base"] = "file_read_failed"
            elif user_input.get(CONF_DEVICES_TEXT):
                raw = user_input[CONF_DEVICES_TEXT]
            else:
                errors["base"] = "no_devices_provided"

            if not errors:
                try:
                    fmdn_devices = await self.hass.async_add_executor_job(_parse_and_validate_fmdn, raw)
                except (json.JSONDecodeError, ValueError, TypeError) as err:
                    _LOGGER.debug("devices.json validation failed: %s", err)
                    errors["base"] = "invalid_json"
                else:
                    # This import replaces the whole FMDN list wholesale --
                    # ids are derived from identity_key, so re-importing the
                    # same devices.json later reproduces the same ids
                    # automatically with no merge logic needed here.
                    data = dict(self.config_entry.data)
                    data[CONF_FMDN_DEVICES] = fmdn_devices
                    return await self._save(data)

        schema = vol.Schema({
            vol.Optional(CONF_DEVICES_FILE): selector.FileSelector(
                selector.FileSelectorConfig(accept=".json,application/json")
            ),
            vol.Optional(CONF_DEVICES_TEXT): selector.TextSelector(
                selector.TextSelectorConfig(multiline=True)
            ),
        })
        return self.async_show_form(step_id="import_fmdn", data_schema=schema, errors=errors)

    async def async_step_sync_google_account(self, user_input=None):
        """
        Link (or re-link) a Google Find My account: upload the secrets.json
        produced by a one-time, external GoogleFindMyTools interactive login
        (see google_findmy/__init__.py and ../../../LICENSE_NOTICE.md for
        exactly what that credential file is and what's vendored here to use
        it). Structurally validates it, then immediately performs a live
        sync as this step's own connectivity test -- a bad/expired
        credential is caught here, not silently on the first background
        resync.

        Like import_fmdn, this replaces the whole FMDN device list. If you
        also import devices.json by hand later, that import overwrites
        whatever the account sync last produced, until the next resync runs
        and overwrites it back -- the two input methods aren't meant to be
        mixed for the same device kind.
        """
        errors = {}
        description_placeholders = None

        if user_input is not None:
            raw = None
            if user_input.get(CONF_SECRETS_FILE):
                try:
                    with process_uploaded_file(self.hass, user_input[CONF_SECRETS_FILE]) as file_path:
                        raw = await self.hass.async_add_executor_job(file_path.read_text, "utf-8")
                except Exception:
                    _LOGGER.exception("Failed to read uploaded secrets.json")
                    errors["base"] = "file_read_failed"
            elif user_input.get(CONF_SECRETS_TEXT):
                raw = user_input[CONF_SECRETS_TEXT]
            else:
                errors["base"] = "no_secrets_provided"

            if not errors:
                try:
                    secrets = json.loads(raw)
                    validate_secrets(secrets)
                except (json.JSONDecodeError, ValueError, TypeError) as err:
                    _LOGGER.debug("secrets.json validation failed: %s", err)
                    errors["base"] = "invalid_secrets"
                else:
                    try:
                        fmdn_devices, unmatched = await self.hass.async_add_executor_job(
                            GoogleFindMySession(secrets).list_devices
                        )
                    except Exception as err:
                        _LOGGER.exception("Initial Google account sync failed")
                        errors["base"] = "sync_failed"
                        description_placeholders = {"error": str(err)}
                    else:
                        data = dict(self.config_entry.data)
                        data[CONF_GOOGLE_SECRETS] = secrets
                        return await self._finish_google_sync(data, fmdn_devices, unmatched)

        schema = vol.Schema({
            vol.Optional(CONF_SECRETS_FILE): selector.FileSelector(
                selector.FileSelectorConfig(accept=".json,application/json")
            ),
            vol.Optional(CONF_SECRETS_TEXT): selector.TextSelector(
                selector.TextSelectorConfig(multiline=True)
            ),
        })
        return self.async_show_form(
            step_id="sync_google_account", data_schema=schema, errors=errors,
            description_placeholders=description_placeholders,
        )

    async def async_step_sync_google_now(self, user_input=None):
        """Re-run a sync against the already-linked account, on demand --
        only reachable from the menu once CONF_GOOGLE_SECRETS is set (see
        async_step_init). No form of its own: runs immediately and either
        moves on (possibly via the unmatched-devices interstitial, possibly
        straight to save+reload) on success, or aborts with the error on
        failure."""
        secrets = self.config_entry.data[CONF_GOOGLE_SECRETS]
        try:
            fmdn_devices, unmatched = await self.hass.async_add_executor_job(
                GoogleFindMySession(secrets).list_devices
            )
        except Exception as err:
            _LOGGER.exception("Google account resync failed")
            return self.async_abort(reason="sync_failed", description_placeholders={"error": str(err)})

        data = dict(self.config_entry.data)
        return await self._finish_google_sync(data, fmdn_devices, unmatched)

    async def _finish_google_sync(self, data: dict, fmdn_devices: list, unmatched: list):
        """Shared tail end of both sync_google_account and sync_google_now:
        stamp the new FMDN list + timestamp into data, and automatically add
        an IRK-kind device (using account_key as the IRK) for every account
        device with no usable identity_key at all -- typically phones, which
        have no real EIK but do use standard classic-Bluetooth private-
        address rotation, the same mechanism HA's own Private BLE Device
        integration already uses for them (see room_presence/BERMUDA.md --
        unvalidated for locator tags specifically, more plausible for
        phones). Every FMDN-matched device (real locator tags) is NOT also
        added as IRK -- FMDN is the validated mechanism for those, an extra
        unvalidated IRK entry would just be redundant.

        (A previous round of this also dual-tracked LE Audio devices --
        headphones/earbuds -- as IRK using account_key, since Google's FMDN
        spec requires that category to rotate via classic RPA. Reverted:
        confirmed live against a real Sony WF-1000XM5 that account_key does
        NOT resolve its actual RPA, and the FMDN spec itself says RPA
        generation for LE Audio devices is outside its scope -- governed by
        ordinary Bluetooth pairing (SMP), a secret Google's API has no
        access to at all. See google_findmy/session.py for where LE Audio
        devices are now excluded from fmdn_devices entirely instead.)

        Dedup/update is by id (same as async_step_add_irk) -- re-syncing
        later just updates these entries in place rather than piling up
        duplicates.
        """
        data[CONF_FMDN_DEVICES] = fmdn_devices

        data[CONF_IRK_DEVICES], added_irk_names = merge_irk_candidates(
            data.get(CONF_IRK_DEVICES, []), unmatched
        )

        if added_irk_names:
            self._pending_google_sync_data = data
            self._pending_google_sync_added_irk = added_irk_names
            return await self.async_step_sync_google_irk_added()

        return await self._save(data)

    async def async_step_sync_google_irk_added(self, user_input=None):
        """Informational-only interstitial: lists the IRK-kind devices this
        sync just added automatically from an account_key (see
        _finish_google_sync above for why, and its caveats). Submitting
        just continues on to save the sync results."""
        if user_input is not None:
            data = self._pending_google_sync_data
            del self._pending_google_sync_data
            del self._pending_google_sync_added_irk
            return await self._save(data)

        added_irk_names = self._pending_google_sync_added_irk
        return self.async_show_form(
            step_id="sync_google_irk_added", data_schema=vol.Schema({}),
            description_placeholders={"names": ", ".join(added_irk_names)},
        )

    async def async_step_add_irk(self, user_input=None):
        errors = {}

        if user_input is not None:
            name = user_input[CONF_NAME]
            irk = _parse_irk(user_input[CONF_IRK])
            if irk is None:
                errors[CONF_IRK] = "invalid_irk"
            else:
                data = dict(self.config_entry.data)
                irk_devices = list(data.get(CONF_IRK_DEVICES, []))
                new_id = compute_id(KIND_IRK, {"irk": irk})
                existing_idx = next(
                    (i for i, d in enumerate(irk_devices) if compute_id(KIND_IRK, d) == new_id), None
                )
                if existing_idx is not None:
                    # Same physical device (identical IRK) re-added -- update
                    # its entry in place instead of appending a second one
                    # that would collide on id anyway.
                    irk_devices[existing_idx] = {"name": name, "irk": irk}
                else:
                    irk_devices.append({"name": name, "irk": irk})
                data[CONF_IRK_DEVICES] = irk_devices
                return await self._save(data)

        schema = vol.Schema({
            vol.Required(CONF_NAME): str,
            vol.Required(CONF_IRK): str,
        })
        return self.async_show_form(step_id="add_irk", data_schema=schema, errors=errors)

    async def async_step_add_static_mac(self, user_input=None):
        errors = {}

        if user_input is not None:
            name = user_input[CONF_NAME]
            mac = _parse_static_mac(user_input[CONF_MAC])
            if mac is None:
                errors[CONF_MAC] = "invalid_mac"
            else:
                data = dict(self.config_entry.data)
                static_mac_devices = list(data.get(CONF_STATIC_MAC_DEVICES, []))
                new_id = compute_id(KIND_STATIC_MAC, {"mac": mac})
                existing_idx = next(
                    (i for i, d in enumerate(static_mac_devices) if compute_id(KIND_STATIC_MAC, d) == new_id), None
                )
                if existing_idx is not None:
                    # Same physical device (identical MAC) re-added -- update
                    # its entry in place instead of appending a second one
                    # that would collide on id anyway.
                    static_mac_devices[existing_idx] = {"name": name, "mac": mac}
                else:
                    static_mac_devices.append({"name": name, "mac": mac})
                data[CONF_STATIC_MAC_DEVICES] = static_mac_devices
                return await self._save(data)

        # Offer every advertisement HA currently knows about as a pick-list
        # (strongest signal first), so the MAC can be chosen instead of typed.
        # connectable=False returns habluetooth's all-history -- the complete
        # set, a superset of the connectable-only view. custom_value keeps
        # manual entry possible for a device that isn't broadcasting right now;
        # either way the selected/typed value is normalized/validated by
        # _parse_static_mac() above (colons, dashes, spaces, or no delimiter
        # are all accepted).
        mac_options = []
        try:
            infos = sorted(
                bluetooth.async_discovered_service_info(self.hass, False),
                key=lambda i: i.rssi if i.rssi is not None else -999,
                reverse=True,
            )
            for info in infos:
                label = f"{info.address} | {info.name or 'Unknown'} | {info.rssi} dBm"
                mac_options.append(selector.SelectOptionDict(value=info.address, label=label))
        except Exception:
            _LOGGER.exception("Could not enumerate discovered Bluetooth advertisements for the MAC picker")

        if mac_options:
            mac_selector = selector.SelectSelector(
                selector.SelectSelectorConfig(options=mac_options, custom_value=True)
            )
        else:
            # Nothing discovered (or enumeration failed) -- fall back to a
            # plain text field rather than an empty dropdown.
            mac_selector = selector.TextSelector()

        schema = vol.Schema({
            vol.Required(CONF_NAME): str,
            vol.Required(CONF_MAC): mac_selector,
        })
        return self.async_show_form(step_id="add_static_mac", data_schema=schema, errors=errors)

    async def async_step_remove_device(self, user_input=None):
        devices = _all_devices_with_ids(self.config_entry.data)

        if not devices:
            return self.async_abort(reason="no_devices_to_remove")

        # Always show "Name (id)" -- not just when two names collide -- so the
        # list is consistent regardless of what else is configured, and the
        # short id is on hand if you ever need to cross-reference a specific
        # entry (e.g. against logs, which also log the id). Also appends
        # current location + last-seen, read live from the running tracker
        # (presence.py) when available -- makes it possible to tell same-
        # named devices apart by where/when they were last seen, not just
        # by id, and gives an at-a-glance status view of everything tracked.
        options = [
            selector.SelectOptionDict(
                value=device_id,
                label=f"{name} ({device_id[:6]}){_device_status_suffix(self.hass, self.config_entry.entry_id, device_id)}",
            )
            for device_id, name in devices
        ]

        errors = {}

        if user_input is not None:
            to_remove = set(user_input[CONF_REMOVE_NAMES])
            if not to_remove:
                errors["base"] = "no_devices_selected"
            else:
                # Removing from config data alone leaves the HA Device-registry
                # entry (and its sensor.* entities) behind forever -- reload
                # only ever *adds* devices for the current list, it never
                # notices one went missing. device_registry.async_remove_device()
                # cascades to remove every entity registered under that device
                # too (confirmed from entity_registry.py's async_device_modified
                # listener).
                device_registry = dr.async_get(self.hass)
                for device_id in to_remove:
                    entry = device_registry.async_get_device(identifiers={(DOMAIN, device_id)})
                    if entry is not None:
                        device_registry.async_remove_device(entry.id)

                data = dict(self.config_entry.data)
                data[CONF_FMDN_DEVICES] = [
                    d for d in data.get(CONF_FMDN_DEVICES, []) if compute_id(KIND_FMDN, d) not in to_remove
                ]
                data[CONF_IRK_DEVICES] = [
                    d for d in data.get(CONF_IRK_DEVICES, []) if compute_id(KIND_IRK, d) not in to_remove
                ]
                data[CONF_STATIC_MAC_DEVICES] = [
                    d for d in data.get(CONF_STATIC_MAC_DEVICES, [])
                    if compute_id(KIND_STATIC_MAC, d) not in to_remove
                ]
                return await self._save(data)

        schema = vol.Schema({
            # Optional + an explicit empty-list default, NOT Required with no
            # default -- a Required multi-select with no default is what made
            # the frontend auto-preselect the first option every time this
            # step opened.
            vol.Optional(CONF_REMOVE_NAMES, default=[]): selector.SelectSelector(
                selector.SelectSelectorConfig(options=options, multiple=True)
            ),
        })
        return self.async_show_form(step_id="remove_device", data_schema=schema, errors=errors)

    async def async_step_map_proxy_room(self, user_input=None):
        """
        Pick from currently-known proxies (each already showing its
        automatically-detected room -- a real HA Area if one resolves, else
        the proxy's own device name) and optionally type an override.
        Leaving the override blank reverts that proxy to the automatic
        default rather than requiring you to know/type its MAC address.
        """
        current_overrides = {
            m["source"].upper(): m["room"] for m in self.config_entry.data.get(CONF_PROXY_ROOMS, [])
        }
        detected = {
            m["source"].upper(): m["room"]
            for m in self.config_entry.data.get(CONF_DETECTED_PROXY_ROOMS, [])
        }

        scanners = bluetooth.async_current_scanners(self.hass)
        if not scanners:
            return self.async_abort(reason="no_proxies_detected")

        options = []
        for scanner in scanners:
            # Same merged view presence.py uses, so the label shown here is
            # the room that's actually in effect (manual override wins).
            effective_room, _area_id, _proxy_device_id, _friendly, area_name = _describe_source(
                self.hass, scanner.source, {**detected, **current_overrides}
            )
            override = current_overrides.get(scanner.source.upper())
            if override is not None:
                label = f"{effective_room} (override)"
            elif area_name is not None:
                label = f"{effective_room} (auto-detected)"
            else:
                label = effective_room
            options.append(selector.SelectOptionDict(value=scanner.source, label=label))

        errors = {}

        if user_input is not None:
            source = user_input[CONF_PROXY_SOURCE].upper()
            override = user_input.get(CONF_ROOM_NAME, "").strip()
            data = dict(self.config_entry.data)
            proxy_rooms = [m for m in data.get(CONF_PROXY_ROOMS, []) if m["source"] != source]
            if override:
                proxy_rooms.append({"source": source, "room": override})
            # else: leaving it blank removes any existing override for this proxy,
            # falling back through to the automatic Area/proxy-name default.
            data[CONF_PROXY_ROOMS] = proxy_rooms
            return await self._save(data)

        schema = vol.Schema({
            vol.Required(CONF_PROXY_SOURCE): selector.SelectSelector(
                selector.SelectSelectorConfig(options=options)
            ),
            vol.Optional(CONF_ROOM_NAME, default=""): str,
        })
        return self.async_show_form(step_id="map_proxy_room", data_schema=schema, errors=errors)

    async def async_step_remove_proxy_room(self, user_input=None):
        proxy_rooms = self.config_entry.data.get(CONF_PROXY_ROOMS, [])
        if not proxy_rooms:
            return self.async_abort(reason="no_proxy_rooms_to_remove")

        options = [f'{m["source"]} -> {m["room"]}' for m in proxy_rooms]
        errors = {}

        if user_input is not None:
            to_remove = set(user_input[CONF_REMOVE_PROXY_SOURCES])
            if not to_remove:
                errors["base"] = "no_devices_selected"
            else:
                data = dict(self.config_entry.data)
                data[CONF_PROXY_ROOMS] = [
                    m for m in proxy_rooms if f'{m["source"]} -> {m["room"]}' not in to_remove
                ]
                return await self._save(data)

        schema = vol.Schema({
            # See async_step_remove_device's comment -- Optional + explicit
            # default=[] avoids the frontend auto-preselecting the first option.
            vol.Optional(CONF_REMOVE_PROXY_SOURCES, default=[]): selector.SelectSelector(
                selector.SelectSelectorConfig(options=options, multiple=True)
            ),
        })
        return self.async_show_form(step_id="remove_proxy_room", data_schema=schema, errors=errors)

    async def async_step_set_proxy_rssi_offset(self, user_input=None):
        """
        Pick from currently-known proxies and set a dBm offset applied to
        every RSSI reading from it before room-picking compares proxies
        against each other -- compensates for proxy hardware with a
        genuinely stronger/weaker radio than the others (e.g. a Shelly vs
        an ESP32) systematically winning/losing regardless of actual
        distance. Entering 0 clears any existing offset for that proxy.
        """
        current_offsets = {
            m["source"].upper(): m["offset"] for m in self.config_entry.data.get(CONF_PROXY_RSSI_OFFSETS, [])
        }

        scanners = bluetooth.async_current_scanners(self.hass)
        if not scanners:
            return self.async_abort(reason="no_proxies_detected")

        options = []
        for scanner in scanners:
            effective_room, _area_id, _proxy_device_id, friendly, _area_name = _describe_source(
                self.hass, scanner.source,
                {
                    **{m["source"].upper(): m["room"] for m in self.config_entry.data.get(CONF_DETECTED_PROXY_ROOMS, [])},
                    **{m["source"].upper(): m["room"] for m in self.config_entry.data.get(CONF_PROXY_ROOMS, [])},
                },
            )
            offset = current_offsets.get(scanner.source.upper())
            label = f"{effective_room} ({friendly})"
            label = f"{label} -- offset {offset:+d} dBm" if offset else f"{label} -- no offset"
            options.append(selector.SelectOptionDict(value=scanner.source, label=label))

        errors = {}

        if user_input is not None:
            source = user_input[CONF_PROXY_SOURCE].upper()
            offset = int(user_input[CONF_RSSI_OFFSET])
            data = dict(self.config_entry.data)
            offsets = [m for m in data.get(CONF_PROXY_RSSI_OFFSETS, []) if m["source"] != source]
            if offset:
                offsets.append({"source": source, "offset": offset})
            # else: 0 removes any existing offset for this proxy.
            data[CONF_PROXY_RSSI_OFFSETS] = offsets
            return await self._save(data)

        schema = vol.Schema({
            vol.Required(CONF_PROXY_SOURCE): selector.SelectSelector(
                selector.SelectSelectorConfig(options=options)
            ),
            vol.Required(CONF_RSSI_OFFSET, default=0): selector.NumberSelector(
                selector.NumberSelectorConfig(min=-40, max=40, step=1, mode=selector.NumberSelectorMode.BOX)
            ),
        })
        return self.async_show_form(step_id="set_proxy_rssi_offset", data_schema=schema, errors=errors)

    async def async_step_settings(self, user_input=None):
        if user_input is not None:
            data = dict(self.config_entry.data)
            data[CONF_UPDATE_LOCATION] = user_input[CONF_UPDATE_LOCATION]
            data[CONF_GOOGLE_SYNC_INTERVAL_HOURS] = user_input[CONF_GOOGLE_SYNC_INTERVAL_HOURS]
            return await self._save(data)

        schema = vol.Schema({
            vol.Required(
                CONF_UPDATE_LOCATION,
                default=self.config_entry.data.get(CONF_UPDATE_LOCATION, False),
            ): selector.BooleanSelector(),
            # Only meaningful once a Google account is linked (see
            # sync_google_account) -- harmless to show either way. 0 disables
            # the periodic timer; "Sync Google account now" always works
            # regardless of this setting.
            vol.Required(
                CONF_GOOGLE_SYNC_INTERVAL_HOURS,
                default=self.config_entry.data.get(
                    CONF_GOOGLE_SYNC_INTERVAL_HOURS, DEFAULT_GOOGLE_SYNC_INTERVAL_HOURS
                ),
            ): selector.NumberSelector(
                selector.NumberSelectorConfig(min=0, max=168, step=1, mode=selector.NumberSelectorMode.BOX)
            ),
        })
        return self.async_show_form(step_id="settings", data_schema=schema)
