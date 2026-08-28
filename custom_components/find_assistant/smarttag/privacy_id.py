"""
Samsung SmartTag "Privacy ID" pool generation -- the tag-side analogue of
FMDN's EID rotation (../eid_generator.py), reverse-engineered and documented
(not by us) at
https://github.com/KieronQuinn/uTag/wiki/BLE-Privacy-ID and
https://github.com/KieronQuinn/uTag/wiki/BLE-Service-Data.

UNVERIFIED AGAINST REAL HARDWARE: this is a from-scratch reimplementation of
the algorithm as described in uTag's wiki (itself a reverse-engineering
effort, not an official Samsung spec), written without access to uTag's own
source or a real SmartTag to test against. Byte-level details the wiki
prose doesn't fully pin down (exact seed length, whether the trailing index
bytes are truly a second copy of the leading ones, exact truncation point)
are implemented as literally described below -- treat this as a
best-effort starting point to validate/correct against a real tag's
advertisements, not as confirmed-working code (contrast with eid_generator.py
and identity.py's FMDN/IRK paths, both confirmed live against real traffic).

Algorithm, per the wiki:
  1. A derived 16-byte key is computed as
     SHA-256(encryption_key[:16] + b"privacy")[:16].
  2. For each pool index i in range(pool_size):
       plaintext = i.to_bytes(2, "big") + seed + i.to_bytes(2, "big")
       ciphertext = AES-CBC(derived_key, iv).encrypt(pad(plaintext))
       privacy_id = ciphertext[:8]   # first 16 hex chars == first 8 bytes
     Tags don't rotate through this pool on any fixed schedule the way FMDN
     rotates on a wall-clock timer -- they pick pseudo-randomly from it, so
     (unlike EidGenerator) there is no time-windowed recomputation here at
     all: the whole pool is precomputed once (per config load) and matching
     is a static set-membership check thereafter.

Unlike FMDN's identity_key (which is unique per tag and secret), the
encryption_key/seed/iv here are themselves the provisioned-per-tag secret --
same trust model, just a different cipher construction.
"""
import hashlib
import logging

from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

_LOGGER = logging.getLogger(__name__)

_DERIVED_KEY_CONTEXT = b"privacy"
_PRIVACY_ID_LENGTH = 8  # bytes -- matches the 8-byte field at service-data offset 4:12

# Sanity ceiling on pool_size -- a hand-edited/malformed smarttag_devices.json
# with an absurd value here would otherwise make _generate_pool spend
# unbounded time/memory during config setup (this runs synchronously in an
# executor job, same as IdentityResolver's other precomputation -- see
# __init__.py). uTag's wiki doesn't state real tags' actual pool sizes, but
# thousands is already generous headroom over any plausible real value.
_MAX_POOL_SIZE = 4096

REQUIRED_SMARTTAG_KEYS = ("name", "encryption_key", "privacy_id_seed", "pool_size", "iv")


def validate_smarttag_device(device: dict) -> None:
    """Raises ValueError with a clear message if a smarttag_devices.json
    entry is missing or has malformed fields. Mirrors
    config_flow.py's _parse_and_validate_fmdn -- called at import time so a
    bad entry is caught immediately, not on the first (silently-empty)
    presence match."""
    if not isinstance(device, dict):
        raise ValueError(f"Expected a device object, got: {device!r}")
    missing = [k for k in REQUIRED_SMARTTAG_KEYS if k not in device]
    if missing:
        raise ValueError(f"SmartTag device entry missing {missing}: {device}")
    if not isinstance(device["pool_size"], int) or isinstance(device["pool_size"], bool) or device["pool_size"] <= 0:
        raise ValueError(
            f"'{device.get('name')}' has an invalid pool_size ({device['pool_size']!r}) -- expected a positive integer"
        )
    if device["pool_size"] > _MAX_POOL_SIZE:
        raise ValueError(
            f"'{device.get('name')}' has an implausibly large pool_size ({device['pool_size']}) -- "
            f"refusing anything over {_MAX_POOL_SIZE}"
        )
    for field in ("encryption_key", "privacy_id_seed", "iv"):
        try:
            import base64
            base64.b64decode(device[field], validate=True)
        except Exception as err:
            raise ValueError(f"'{device.get('name')}' has a non-base64 {field}") from err


def _derive_privacy_key(encryption_key: bytes) -> bytes:
    return hashlib.sha256(encryption_key[:16] + _DERIVED_KEY_CONTEXT).digest()[:16]


def generate_privacy_id_pool(encryption_key: bytes, seed: bytes, iv: bytes, pool_size: int) -> set:
    """Returns the full set of 8-byte Privacy IDs this tag could currently be
    advertising, given its provisioned key material. See module docstring
    for the algorithm and its unverified status."""
    derived_key = _derive_privacy_key(encryption_key)
    padder_len = algorithms.AES.block_size
    pool = set()
    for index in range(pool_size):
        index_bytes = index.to_bytes(2, "big")
        plaintext = index_bytes + seed + index_bytes
        padder = padding.PKCS7(padder_len).padder()
        padded = padder.update(plaintext) + padder.finalize()
        encryptor = Cipher(algorithms.AES(derived_key), modes.CBC(iv)).encryptor()
        ciphertext = encryptor.update(padded) + encryptor.finalize()
        pool.add(ciphertext[:_PRIVACY_ID_LENGTH])
    return pool
