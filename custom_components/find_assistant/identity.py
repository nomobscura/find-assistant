"""
Deterministic device identity ids, derived from each kind's own
cryptographic/addressing material rather than its (possibly non-unique)
display name.

Names alone used to be the *only* identity key everywhere in this
integration -- dict keys, entity unique_ids, Device-registry identifiers.
That meant two tags sharing a name (confirmed live: two real "UGREEN Finder
Pro" tags, and separately two "OTAG" entries in one user's devices.json)
silently collapsed into a single tracked device, with sightings from both
physical tags overwriting each other's current_mac/rssi/room.

compute_id() is deliberately NOT a random UUID: deriving it from the
identity material means re-importing/re-adding the same physical device
later reproduces the exact same id automatically, with no bookkeeping --
two genuinely different devices can never collide on it, and two entries
that DO collide really are the same physical device (which is correct to
treat as one).
"""
import hashlib
import logging

from .const import KIND_FMDN, KIND_IRK, KIND_SMARTTAG, KIND_STATIC_MAC

_LOGGER = logging.getLogger(__name__)

_ID_LENGTH = 16  # hex chars (64 bits) -- ample for collision-freedom at any realistic device count


def compute_id(kind: str, device: dict) -> str:
    """Returns a short, stable, opaque identity token for a configured
    device dict, derived from whichever field is actually unique to that
    physical device for its kind:
      - fmdn:       identity_key (the FMDN pairing secret)
      - irk:        irk (the classic-Bluetooth resolving key)
      - static_mac: mac (already unique by definition)
      - smarttag:   encryption_key + privacy_id_seed (its provisioned secret
                    pair -- see smarttag/privacy_id.py)
    Kind is included in the hashed material so that, in principle, an FMDN
    device and an IRK device could never collide even if their raw secrets
    happened to coincide (astronomically unlikely, but free to guard against).
    """
    if kind == KIND_FMDN:
        material = device["identity_key"]
    elif kind == KIND_IRK:
        material = device["irk"]
    elif kind == KIND_STATIC_MAC:
        material = device["mac"]
    elif kind == KIND_SMARTTAG:
        # Both base64 strings, verbatim as stored -- no case-folding (unlike
        # the hex materials above) since base64 is case-sensitive.
        material = device["encryption_key"] + ":" + device["privacy_id_seed"]
        return hashlib.sha256(f"{kind}:{material}".encode()).hexdigest()[:_ID_LENGTH]
    else:
        raise ValueError(f"Unknown device kind: {kind}")
    digest = hashlib.sha256(f"{kind}:{material.upper()}".encode()).hexdigest()
    return digest[:_ID_LENGTH]


def merge_irk_candidates(irk_devices: list, candidates: list) -> tuple:
    """Adds/updates IRK-kind devices from a list of {name, account_key}
    candidates (as produced by google_findmy's list_devices() for account
    devices with no usable identity_key but a usable account_key -- see
    that function's docstring). Shared between config_flow.py's interactive
    sync steps and __init__.py's periodic sync so both apply identical
    merge semantics.

    Returns (new_irk_devices, changed_names) -- new_irk_devices is
    irk_devices with each candidate's account_key added/updated in place
    (dedup by id, same as async_step_add_irk: a candidate whose account_key
    already matches an existing IRK device's id just refreshes that entry's
    name instead of appending a duplicate).

    changed_names lists only candidates that were genuinely added or whose
    stored entry actually differs -- NOT every candidate seen. Callers use
    it to decide whether to show the "added as IRK devices" interstitial and
    whether to log; reporting unchanged re-syncs there meant that dialog
    appeared on every single sync and the log line repeated every 12h
    forever, both saying "added" about devices that already existed.

    Candidates whose account_key isn't a valid 16-byte hex IRK are skipped
    rather than stored -- resolver.py would otherwise log a cipher-prep
    exception and skip them anyway, leaving a permanently-dead device with
    live entities behind."""
    irk_devices = list(irk_devices)
    changed_names = []
    for candidate in candidates:
        if not candidate.get("account_key"):
            continue
        irk = candidate["account_key"].upper()
        try:
            if len(bytes.fromhex(irk)) != 16:
                raise ValueError("wrong length")
        except ValueError:
            _LOGGER.warning(
                "Skipping IRK candidate '%s' -- account_key isn't a valid 16-byte hex value",
                candidate.get("name"),
            )
            continue
        name = candidate["name"]
        new_entry = {"name": name, "irk": irk}
        new_id = compute_id(KIND_IRK, new_entry)
        existing_idx = next(
            (i for i, d in enumerate(irk_devices) if compute_id(KIND_IRK, d) == new_id), None
        )
        if existing_idx is None:
            irk_devices.append(new_entry)
            changed_names.append(name)
        elif irk_devices[existing_idx] != new_entry:
            irk_devices[existing_idx] = new_entry
            changed_names.append(name)
    return irk_devices, changed_names


def merge_fmdn_devices(existing: list, synced: list) -> tuple:
    """Add/update-only merge of a freshly-synced FMDN device list into the
    stored one, keyed by compute_id(KIND_FMDN, ...). Returns
    (new_fmdn_devices, changed_names).

    Deliberately NEVER drops a stored device just because it's missing from
    `synced`. This is the unattended-sync path: a transient partial Nova
    response (or a device Google momentarily stops returning) would
    otherwise replace the stored list wholesale, and __init__.py's
    orphaned-device cleanup would then cascade that into deleting the
    device's HA Device entry and every sensor/button entity under it --
    silent, unprompted data loss with no undo. Removing a device stays a
    deliberate "Remove a device" action in the options flow.

    Note the interactive sync path (config_flow.py) intentionally still
    replaces the list wholesale -- that's a user-initiated action where
    mirroring the account exactly is the expected behavior."""
    merged = list(existing)
    changed_names = []
    by_id = {compute_id(KIND_FMDN, d): i for i, d in enumerate(merged)}
    for device in synced:
        device_id = compute_id(KIND_FMDN, device)
        existing_idx = by_id.get(device_id)
        if existing_idx is None:
            merged.append(device)
            by_id[device_id] = len(merged) - 1
            changed_names.append(device["name"])
        elif merged[existing_idx] != device:
            merged[existing_idx] = device
            changed_names.append(device["name"])
    return merged, changed_names
