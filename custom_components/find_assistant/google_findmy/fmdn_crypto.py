"""
Decrypts a "network" (crowdsourced) FMDN location report -- one found by
someone ELSE's phone via the Find My Device network, as opposed to an "own"
report (found by a device signed into the same Google account, which is
just plain AES-GCM -- see google_findmy/crypto.py's decrypt_aes_gcm).

Trimmed re-implementation of GoogleFindMyTools' FMDNCrypto/
foreign_tracker_cryptor.py (c) 2024 Leon Boettger, GPLv3 -- see
../../../../LICENSE_NOTICE.md. Only decrypt() is needed here (never encrypt
-- that's the finder's phone's job, not ours); the elliptic-curve point
arithmetic is NOT reimplemented from that file, it reuses this project's
own secp160r1 Jacobian-coordinate implementation in eid_generator.py
(already validated against real EIDs matching live advertisements) rather
than adding the `ecdsa` package as a runtime dependency.

The scheme, in short (see the Google FMDN accessory spec for the real
description): a finder's phone picks a random scalar s, computes S = s*G,
and encrypts the location under a key derived from s*R (R being this EID
window's public point -- the *same* R = r*G used for BLE EID broadcast,
where r comes from the identity_key + this time window, exactly as
eid_generator.py already computes for EID matching). The report carries
(ciphertext+tag, S.x) -- since S.x alone doesn't uniquely determine S.y, a
point is reconstructed by solving the curve equation for y and picking one
of the two Y roots by convention (matching what the encrypting side did) --
that's rx_to_ry() below. We -- the identity_key holder -- can then recompute
R's scalar r ourselves and derive the same shared point r*S = s*R, hence
the same key, without ever needing s.
"""
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from ..eid_generator import A, B, P, EidGenerator, JacobianPoint, multiply_g, scalar_multiply, to_affine_x


def _rx_to_ry(rx: int) -> int:
    """Recovers a point's y-coordinate from its x, given only that the point
    lies on the secp160r1 curve (y^2 = x^3 + A*x + B mod P) -- there are two
    valid y values (y and P-y); the convention (matching the encrypting
    side) is to pick the even one."""
    ryy = (pow(rx, 3, P) + A * rx + B) % P
    ry = pow(ryy, (P + 1) // 4, P)  # modular square root (valid since P % 4 == 3)
    if (ry * ry) % P != ryy:
        raise ValueError("Not a valid point on secp160r1 -- corrupted or non-FMDN public key")
    if ry % 2 != 0:
        ry = P - ry
    return ry


def decrypt(identity_key: bytes, encrypted_and_tag: bytes, sx: bytes, beacon_time_counter: int) -> bytes:
    """Decrypts one crowdsourced location report.

    Args:
        identity_key: this device's 32-byte FMDN identity key.
        encrypted_and_tag: the report's ciphertext with a 16-byte AES-EAX
            tag appended (as stored in the report's encryptedLocation field).
        sx: the report's publicKeyRandom field -- the finder's ephemeral
            public point's x-coordinate, 20 bytes big-endian.
        beacon_time_counter: seconds since pairing for the report's time
            window (masked the same way EID generation masks it -- pass the
            *unmasked* raw counter, same as EidGenerator.generate_eid()).

    Returns: the decrypted plaintext (a serialized DeviceUpdate_pb2.Location
    protobuf message for geo reports).
    """
    from Cryptodome.Cipher import AES  # imported lazily -- only needed when actually decrypting

    ciphertext, tag = encrypted_and_tag[:-16], encrypted_and_tag[-16:]

    r = EidGenerator.calculate_r(identity_key, beacon_time_counter)
    # R = r*G; R.x is exactly this window's EID (multiply_g already returns
    # the x-coordinate, same function EID generation itself uses).
    rx_bytes = multiply_g(r).to_bytes(20, "big")

    sx_int = int.from_bytes(sx, "big")
    sy_int = _rx_to_ry(sx_int)
    shared_point = scalar_multiply(r, JacobianPoint(sx_int, sy_int, 1))
    shared_x = to_affine_x(shared_point).to_bytes(20, "big")

    key = HKDF(algorithm=hashes.SHA256(), length=32, salt=None, info=b"").derive(shared_x)
    nonce = rx_bytes[12:] + sx[12:]  # lower 8 bytes of each point's x-coordinate

    cipher = AES.new(key, AES.MODE_EAX, nonce=nonce)
    return cipher.decrypt_and_verify(ciphertext, tag)
