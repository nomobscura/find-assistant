"""
AES helpers for decrypting a Find My device's identity key (EIK) and
account key against the account's cached owner_key.

Trimmed, unmodified-logic extraction of GoogleFindMyTools'
KeyBackup/cloud_key_decryptor.py (c) 2024 Leon Böttger, GPLv3 -- see
../../../../LICENSE_NOTICE.md. The full original file also derives
owner_key itself from an interactive LSKF/PIN cloud-key-backup chain; that
chain is out of scope here since owner_key is supplied directly (read from
the account's secrets.json -- see session.py).
"""
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


def decrypt_aes_gcm(key: bytes, encrypted_data_and_iv: bytes, additional_data: bytes = None, iv_length=12) -> bytes:
    # IV is prepended to encrypted data
    iv = encrypted_data_and_iv[:iv_length]
    ciphertext = encrypted_data_and_iv[iv_length:]
    aes_gcm = AESGCM(key)
    return aes_gcm.decrypt(iv, ciphertext, additional_data)


def decrypt_aes_cbc_no_padding(key: bytes, encrypted_data_and_iv: bytes, iv_length=16) -> bytes:
    # IV is prepended to encrypted data
    iv = encrypted_data_and_iv[:iv_length]
    ciphertext = encrypted_data_and_iv[iv_length:]
    cipher = Cipher(algorithms.AES(key), modes.CBC(iv), backend=None)
    decryptor = cipher.decryptor()
    return decryptor.update(ciphertext) + decryptor.finalize()


def decrypt_eik(owner_key: bytes, encrypted_eik: bytes) -> bytes:
    """The EIK is encrypted using the owner key; only valid for one tracker."""
    if len(encrypted_eik) == 48:
        return decrypt_aes_cbc_no_padding(owner_key, encrypted_eik)
    if len(encrypted_eik) == 60:
        return decrypt_aes_gcm(owner_key, encrypted_eik)
    raise ValueError(f"The encrypted EIK has invalid length ({len(encrypted_eik)})")


def decrypt_account_key(owner_key: bytes, encrypted_account_key: bytes) -> bytes:
    """The account key is a *different* rotating secret from the identity
    key: it's what the tracker uses as a standard Bluetooth IRK to rotate
    its own link-layer MAC address (see room_presence/BERMUDA.md), separate
    from the identity_key/EID rotation decrypt_eik() handles."""
    if len(encrypted_account_key) == 32:
        return decrypt_aes_cbc_no_padding(owner_key, encrypted_account_key)
    if len(encrypted_account_key) == 44:
        return decrypt_aes_gcm(owner_key, encrypted_account_key)
    raise ValueError(f"The encrypted Account Key has invalid length ({len(encrypted_account_key)})")
