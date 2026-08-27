"""
Unified identity resolution across three independent strategies:

  - static_mac: trivial equality check against a fixed address.
  - fmdn:       Google Find My Device Network EID matching (decrypt/compare
                against precomputed rotation windows derived from
                identity_key + pair_date). See eid_generator.py.
  - irk:        standard Bluetooth Core Spec resolvable-private-address
                resolution (the same mechanism Home Assistant's own
                Private BLE Device integration and Bermuda's bermuda_irk.py
                use for phones etc.), via the `bluetooth-data-tools`
                library HA itself already depends on.

All three answer the same question -- "does this advertisement belong to
device X?" -- so they can share one resolve() entry point regardless of
which mechanism a given configured device uses.

Identity note: resolve() and every internal dict here are keyed by a
derived *id* (see identity.py), not by the device's display name. Two
devices can share a name -- the id, not the name, is what's guaranteed
unique. Use name_for(id) to get the display label back.

Performance notes (resolve() runs for EVERY BLE advertisement HA sees, not
just tracked devices, so the miss path matters more than the hit path):
  - static_mac and fmdn are already O(1) dict lookups.
  - irk results are cached per address: whether an address resolves against
    a given IRK set is deterministic and permanent (addresses that rotate
    simply show up as new cache keys), so both hits and misses are cached
    for the resolver's lifetime. The miss set is capped and cleared rather
    than LRU-evicted -- it just refills, and clearing is O(1).
  - irk is only attempted at all for addresses that are actually resolvable
    private addresses per the BT Core Spec (top two bits 0b01, i.e. first
    hex digit 4-7). Public/static-random addresses can never resolve
    against any IRK, so the AES work is skipped for them entirely.
"""
import logging
import time

from bluetooth_data_tools import get_cipher_for_irk, resolve_private_address

from .eid_generator import EidGenerator, ROTATION_PERIOD
from .identity import compute_id
from .const import (
    EDDYSTONE_SERVICE_UUID,
    EID_REFRESH_SECONDS,
    EID_WINDOWS,
    FMDN_SERVICE_UUID,
    KIND_FMDN,
    KIND_IRK,
    KIND_STATIC_MAC,
)

_LOGGER = logging.getLogger(__name__)

# Resolvable private addresses have the two most significant bits set to 0b01
# (BT Core Spec Vol 6 Part B 1.3.2.2) -> first hex digit of the address is 4-7.
_RPA_FIRST_DIGITS = frozenset("4567")

# Cap on the negative IRK cache before it's cleared wholesale. Addresses in a
# busy environment rotate every ~15 min, so the set grows without bound
# otherwise; clearing (rather than evicting) is O(1) and it simply refills.
_IRK_MISS_CACHE_MAX = 4096


class IdentityResolver:
    """Not thread-safe; only call from the event loop (all callers do --
    except the constructor, which __init__.py deliberately runs in an
    executor so the initial EID window computation stays off the loop)."""

    def __init__(self, fmdn_devices: list, irk_devices: list, static_mac_devices: list):
        self._fmdn_devices = fmdn_devices
        # Parallel to _fmdn_devices (same order/length) -- computed once so
        # neither this constructor nor _refresh_eid_windows need to re-hash
        # identity_key on every call.
        self._fmdn_ids = [compute_id(KIND_FMDN, d) for d in fmdn_devices]

        self._names: dict[str, str] = {}  # id -> display name (NOT guaranteed unique)
        self._kinds: dict[str, str] = {}  # id -> kind

        self._static_macs: dict[str, str] = {}  # mac -> id
        for d in static_mac_devices:
            device_id = compute_id(KIND_STATIC_MAC, d)
            self._static_macs[d["mac"].upper()] = device_id
            self._names[device_id] = d["name"]
            self._kinds[device_id] = KIND_STATIC_MAC

        self._irk_ciphers = []  # list of (id, cipher) -- order doesn't matter, checked linearly
        self._irk_hex: dict[str, str] = {}  # id -> raw IRK hex, for sensor.py's diagnostic entity
        for d in irk_devices:
            try:
                irk_bytes = bytes.fromhex(d["irk"])
                device_id = compute_id(KIND_IRK, d)
                self._irk_ciphers.append((device_id, get_cipher_for_irk(irk_bytes)))
                self._irk_hex[device_id] = d["irk"].upper()
                self._names[device_id] = d["name"]
                self._kinds[device_id] = KIND_IRK
            except Exception:
                _LOGGER.exception("Failed to prepare IRK cipher for '%s' -- skipping it", d.get("name"))

        self._fmdn_identity_key_hex: dict[str, str] = {}  # id -> raw identity_key hex, for ring.py
        self._manufacturer: dict[str, str] = {}  # id -> Google's manufacturer label, FMDN-kind only
        self._model: dict[str, str] = {}  # id -> Google's model label, FMDN-kind only
        for d, device_id in zip(fmdn_devices, self._fmdn_ids):
            self._names[device_id] = d["name"]
            self._kinds[device_id] = KIND_FMDN
            self._fmdn_identity_key_hex[device_id] = d["identity_key"].upper()
            if d.get("manufacturer"):
                self._manufacturer[device_id] = d["manufacturer"]
            if d.get("model"):
                self._model[device_id] = d["model"]

        # Per-address IRK resolution cache -- see module docstring.
        self._irk_hits: dict[str, str] = {}
        self._irk_misses: set[str] = set()

        # FMDN EID -> device id, recomputed periodically as the rotation
        # window advances. _eid_cache holds every EID we've already computed,
        # keyed by (device id, masked timestamp): the +/-EID_WINDOWS window
        # set only slides by ONE window per ROTATION_PERIOD (~17 min), so on a
        # typical refresh every window but the newest edge is already cached
        # and a refresh costs ~0-1 elliptic-curve multiplications per device
        # instead of recomputing all 2*EID_WINDOWS+1 from scratch. This is
        # what makes it safe to run maybe_refresh() synchronously on the
        # event loop (each EC multiply is ~1ms with eid_generator's
        # fixed-base table; a full from-scratch recompute of every window for
        # many devices was a 100ms+ stall).
        self._eid_cache: dict[tuple[str, int], bytes] = {}
        self._eid_to_id: dict[bytes, str] = {}
        self._last_eid_refresh = 0.0
        self._refresh_eid_windows()

    def _refresh_eid_windows(self):
        eid_to_id: dict[bytes, str] = {}
        live_keys: set[tuple[str, int]] = set()
        computed = 0
        for device, device_id in zip(self._fmdn_devices, self._fmdn_ids):
            try:
                key_bytes = bytes.fromhex(device["identity_key"])
                btc = EidGenerator.get_beacon_time_counter(device["pair_date"])
                for offset in range(-EID_WINDOWS, EID_WINDOWS + 1):
                    # Same masking generate_eid() applies internally -- the
                    # masked timestamp IS the window identity, so it's the
                    # correct cache key.
                    masked_ts = ((btc + offset * ROTATION_PERIOD) & ((-1) << 10)) & 0xFFFFFFFF
                    cache_key = (device_id, masked_ts)
                    live_keys.add(cache_key)
                    eid = self._eid_cache.get(cache_key)
                    if eid is None:
                        eid = EidGenerator.generate_eid(key_bytes, masked_ts)
                        self._eid_cache[cache_key] = eid
                        computed += 1
                    eid_to_id[eid] = device_id
            except Exception:
                _LOGGER.exception("Failed to compute EID windows for '%s' -- skipping it", device.get("name"))
        # Prune cache entries whose windows have scrolled out of range.
        for stale_key in [k for k in self._eid_cache if k not in live_keys]:
            del self._eid_cache[stale_key]
        self._eid_to_id = eid_to_id
        self._last_eid_refresh = time.time()
        if computed:
            _LOGGER.debug("EID window refresh computed %d new EID(s)", computed)

    def maybe_refresh(self):
        if self._fmdn_devices and time.time() - self._last_eid_refresh > EID_REFRESH_SECONDS:
            self._refresh_eid_windows()

    def resolve(self, address: str, service_data: dict) -> str | None:
        """Return the matching device's *id* (not name -- see module
        docstring), or None if this advertisement isn't recognized."""
        address = address.upper()

        # 1. Static MAC -- cheapest possible check, try it first.
        device_id = self._static_macs.get(address)
        if device_id is not None:
            return device_id

        # 2. FMDN -- look for FMDN/Eddystone service data and match its EID.
        if service_data and self._eid_to_id:
            data = service_data.get(FMDN_SERVICE_UUID) or service_data.get(EDDYSTONE_SERVICE_UUID)
            if data and len(data) >= 21:
                device_id = self._eid_to_id.get(bytes(data[1:21]))
                if device_id is not None:
                    return device_id

        # 3. IRK -- only for genuinely resolvable private addresses, with
        #    per-address result caching (see module docstring).
        if not self._irk_ciphers or address[0] not in _RPA_FIRST_DIGITS:
            return None
        device_id = self._irk_hits.get(address)
        if device_id is not None:
            return device_id
        if address in self._irk_misses:
            return None
        for device_id, cipher in self._irk_ciphers:
            try:
                if resolve_private_address(cipher, address):
                    self._irk_hits[address] = device_id
                    return device_id
            except Exception:
                _LOGGER.exception("Error resolving address %s against IRK id '%s'", address, device_id)
        if len(self._irk_misses) >= _IRK_MISS_CACHE_MAX:
            self._irk_misses.clear()
        self._irk_misses.add(address)
        return None

    @property
    def device_ids(self) -> list:
        return list(self._kinds)

    def kind_for(self, device_id: str) -> str:
        return self._kinds.get(device_id, KIND_FMDN)

    def name_for(self, device_id: str) -> str:
        """Display label for a device id -- NOT guaranteed unique across
        devices, unlike the id itself."""
        return self._names.get(device_id, device_id)

    def irk_for(self, device_id: str) -> str | None:
        """The raw configured IRK (hex) for an IRK-kind device, for sensor.py's
        diagnostic entity. None for every other kind."""
        return self._irk_hex.get(device_id)

    def identity_key_for(self, device_id: str) -> str | None:
        """The raw configured identity_key (hex) for an FMDN-kind device, for
        ring.py's active-BLE ring command (its authentication key is derived
        from this). None for every other kind."""
        return self._fmdn_identity_key_hex.get(device_id)

    def manufacturer_for(self, device_id: str) -> str | None:
        """Google's own manufacturer label (e.g. "Pebblebee") for an
        FMDN-kind device synced from an account, for HA's Device Registry
        manufacturer field (see sensor.py/button.py's DeviceInfo). None if
        unavailable (manually-added kinds, or a device.json import that
        predates this field)."""
        return self._manufacturer.get(device_id)

    def model_for(self, device_id: str) -> str | None:
        """Google's own model label (e.g. "Pebblebee Clip") -- see
        manufacturer_for's docstring, same availability caveats."""
        return self._model.get(device_id)
