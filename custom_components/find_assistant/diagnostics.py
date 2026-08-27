"""
Diagnostics support for Find Assistant.

Without this, HA's device page tries to fetch diagnostics for every device
regardless of integration, gets `{"code": "not_found", "message": "Domain
not supported"}` back for any domain that hasn't implemented this module,
and logs an "Uncaught (in promise)" console error -- harmless, but this
makes the "Download diagnostics" button actually work and gives something
useful to attach to a bug report instead.

CONF_GOOGLE_SECRETS (a full Google account credential blob) and each
device's own identity material (identity_key/irk/mac -- the values that
let something impersonate a tracked tag) are redacted; everything else here
is live presence state, not credentials.
"""
from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntry

from .const import (
    CONF_FMDN_DEVICES,
    CONF_GOOGLE_SECRETS,
    CONF_IRK_DEVICES,
    CONF_STATIC_MAC_DEVICES,
    DOMAIN,
)
from .presence import DevicePresence, RoomPresenceTracker

TO_REDACT = {
    CONF_GOOGLE_SECRETS,
    "identity_key",
    "irk",
    "mac",
    "current_mac",
    "synced_mac",
}


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry: ConfigEntry) -> dict[str, Any]:
    """Return diagnostics for the whole config entry."""
    tracker: RoomPresenceTracker | None = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    return {
        "entry_data": async_redact_data(dict(entry.data), TO_REDACT),
        "options": dict(entry.options),
        "device_counts": {
            "fmdn": len(entry.data.get(CONF_FMDN_DEVICES, [])),
            "irk": len(entry.data.get(CONF_IRK_DEVICES, [])),
            "static_mac": len(entry.data.get(CONF_STATIC_MAC_DEVICES, [])),
        },
        "devices": (
            [_device_snapshot(d) for d in tracker.devices.values()] if tracker else None
        ),
    }


async def async_get_device_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry, device: DeviceEntry
) -> dict[str, Any]:
    """Return diagnostics for a single tracked device (or proxy) Device-registry entry."""
    tracker: RoomPresenceTracker | None = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    tag_id = next((ident[1] for ident in device.identifiers if ident[0] == DOMAIN), None)
    if tracker is None or tag_id is None:
        return {"tag_id": tag_id, "note": "not a tracked tag device for this config entry"}

    presence = tracker.devices.get(tag_id)
    if presence is None:
        return {"tag_id": tag_id, "note": "not tracked by this config entry"}
    return _device_snapshot(presence)


def _device_snapshot(device: DevicePresence) -> dict[str, Any]:
    return async_redact_data(
        {
            "id": device.id,
            "name": device.name,
            "kind": device.kind,
            "room": device.room,
            "last_room": device.last_room,
            "area_id": device.area_id,
            "via_device_id": device.via_device_id,
            "rssi": device.rssi,
            "current_mac": device.current_mac,
            "synced_mac": device.synced_mac,
            "winning_source": device.winning_source,
            "updated_at": device.updated_at,
            "last_known_location": device.last_known_location,
            "sightings": device.sightings,
            "entity_ids": [e.entity_id for e in device.entities],
        },
        TO_REDACT,
    )
