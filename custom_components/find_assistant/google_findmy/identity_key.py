"""
Decrypts a single device's identity key (EIK) and account key from its
Nova API DeviceRegistration record.

Trimmed extraction of GoogleFindMyTools' NovaApi/ExecuteAction/LocateTracker
/decrypt_locations.py (c) 2024 Leon Böttger, GPLv3 -- see
../../../../LICENSE_NOTICE.md. The full original file also decrypts
*location reports* (needs FMDNCrypto, an FCM round-trip, etc.); none of
that is needed just to list devices' identity keys and pair dates, so only
the identity/account-key retrieval logic was kept.

Unlike the original, owner_key is passed in explicitly rather than read
from a global cache -- see session.py, which owns the account's secrets.
"""
from .crypto import decrypt_account_key, decrypt_eik
from .proto.DeviceUpdate_pb2 import DeviceRegistration

# Fast Pair model id used by self-registered ESP32/Zephyr trackers (as
# opposed to genuine OEM Spot devices) -- from GoogleFindMyTools'
# SpotApi/CreateBleDevice/config.py.
_MCU_FAST_PAIR_MODEL_ID = "003200"


def _flip_bits(data: bytes, enabled: bool) -> bytes:
    """Flips all bits in each byte -- from SpotApi/CreateBleDevice/util.py.
    MCU (self-registered) trackers store their encryptedIdentityKey bit-
    flipped relative to genuine OEM devices."""
    if enabled:
        return bytes(b ^ 0xFF for b in data)
    return data


def is_mcu_tracker(device_registration: DeviceRegistration) -> bool:
    """True if this device is a custom ESP32/Zephyr tracker rather than an OEM Spot device."""
    return device_registration.fastPairModelId == _MCU_FAST_PAIR_MODEL_ID


def retrieve_identity_key(device_registration: DeviceRegistration, owner_key: bytes) -> bytes:
    """Decrypt and return the 32-byte identity key (EIK) for a device registration."""
    is_mcu = is_mcu_tracker(device_registration)
    encrypted_user_secrets = device_registration.encryptedUserSecrets
    encrypted_identity_key = _flip_bits(encrypted_user_secrets.encryptedIdentityKey, is_mcu)
    return decrypt_eik(owner_key, encrypted_identity_key)


def retrieve_account_key(device_registration: DeviceRegistration, owner_key: bytes) -> bytes:
    """
    Decrypt and return the 16-byte account key for a device registration.

    This is a *different* rotating secret from the identity key: it's what
    the tracker uses as a standard Bluetooth IRK to rotate its own
    link-layer MAC address, independent of the identity_key/EID rotation in
    the FMDN service-data payload. See room_presence/BERMUDA.md.
    """
    encrypted_account_key = device_registration.encryptedUserSecrets.encryptedAccountKey
    if not encrypted_account_key:
        raise ValueError("This device registration has no encryptedAccountKey")
    return decrypt_account_key(owner_key, encrypted_account_key)
