#!/usr/bin/env python3
"""
FMDN Ephemeral Identifier (EID) Generator

Generates EIDs from an identity key and pair date for FMDN device detection.
Port of the Kotlin implementation for use with the BLE monitor.

EID Algorithm (from Google FMDN spec):
1. Mask timestamp (zero lower 10 bits)
2. Build 32-byte data block
3. AES-256-ECB encrypt with identity key
4. Convert to BigInteger, mod by SECP160r1 curve order
5. Multiply by generator point: R = r * G
6. EID = x-coordinate of R (20 bytes)
"""

import time
from dataclasses import dataclass
from typing import Optional

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.backends import default_backend


# secp160r1 curve parameters
# p = 2^160 - 2^31 - 1
P = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFF7FFFFFFF
# Curve order
N = 0x0100000000000000000001F4C8F927AED3CA752257
# Generator point
GX = 0x4A96B5688EF573284664698968C38BB913CBFC82
GY = 0x23A628553168947D59DCC912042351377AC5FB32
# Curve coefficients: y^2 = x^3 + A*x + B (mod P). A = -3 mod P is the standard
# SEC-curve optimization already assumed by jacobian_double() above; B is only
# needed to recover a point's y from its x (see fmdn_crypto.py's rx_to_ry) --
# not needed for EID generation itself, which is why it wasn't defined until
# fmdn_crypto.py needed it. Cross-checked against the independent `ecdsa`
# PyPI package's own SECP160r1 definition (P/N/GX/GY above already matched it
# exactly) rather than trusting a hand-transcribed constant for something
# crypto-critical.
A = (P - 3) % P
B = 0x1C97BEFC54BD7A8B65ACF89F81D4D4ADC565FA45

K = 10  # Lower bits to mask in timestamp
ROTATION_PERIOD = 1024  # seconds (~17 minutes)


@dataclass
class JacobianPoint:
    """Point in Jacobian coordinates (X:Y:Z) where affine x = X/Z^2, y = Y/Z^3."""
    X: int
    Y: int
    Z: int

    def is_infinity(self) -> bool:
        return self.Z == 0


# Point at infinity
INFINITY = JacobianPoint(1, 1, 0)

# Generator point in Jacobian coordinates
G = JacobianPoint(GX, GY, 1)


def jacobian_double(point: JacobianPoint) -> JacobianPoint:
    """
    Point doubling in Jacobian coordinates, optimized for a = -3.
    Formula: dbl-2001-b from hyperelliptic.org
    """
    if point.is_infinity():
        return point

    X1, Y1, Z1 = point.X, point.Y, point.Z

    delta = (Z1 * Z1) % P
    gamma = (Y1 * Y1) % P
    beta = (X1 * gamma) % P

    # alpha = 3 * (X1 - delta) * (X1 + delta) -- a = -3 optimization
    x_minus_delta = (X1 - delta) % P
    x_plus_delta = (X1 + delta) % P
    alpha = (3 * x_minus_delta * x_plus_delta) % P

    # X3 = alpha^2 - 8*beta
    alpha2 = (alpha * alpha) % P
    beta8 = (8 * beta) % P
    X3 = (alpha2 - beta8) % P

    # Y3 = alpha * (4*beta - X3) - 8*gamma^2
    beta4 = (4 * beta) % P
    gamma2 = (gamma * gamma) % P
    Y3 = (alpha * (beta4 - X3) - 8 * gamma2) % P

    # Z3 = (Y1 + Z1)^2 - gamma - delta
    y1_plus_z1 = (Y1 + Z1) % P
    Z3 = (y1_plus_z1 * y1_plus_z1 - gamma - delta) % P

    return JacobianPoint(X3, Y3, Z3)


def jacobian_add(p1: JacobianPoint, p2: JacobianPoint) -> JacobianPoint:
    """
    Point addition in Jacobian coordinates.
    Formula: add-2007-bl from hyperelliptic.org
    """
    if p1.is_infinity():
        return p2
    if p2.is_infinity():
        return p1

    X1, Y1, Z1 = p1.X, p1.Y, p1.Z
    X2, Y2, Z2 = p2.X, p2.Y, p2.Z

    Z1Z1 = (Z1 * Z1) % P
    Z2Z2 = (Z2 * Z2) % P
    U1 = (X1 * Z2Z2) % P
    U2 = (X2 * Z1Z1) % P
    S1 = (Y1 * Z2 * Z2Z2) % P
    S2 = (Y2 * Z1 * Z1Z1) % P

    H = (U2 - U1) % P
    S2_minus_S1 = (S2 - S1) % P

    # Special cases
    if H == 0:
        if S2_minus_S1 == 0:
            return jacobian_double(p1)  # P == Q
        else:
            return INFINITY  # P == -Q

    I = (4 * H * H) % P
    J = (H * I) % P
    r = (2 * S2_minus_S1) % P
    V = (U1 * I) % P

    # X3 = r^2 - J - 2V
    r2 = (r * r) % P
    X3 = (r2 - J - 2 * V) % P

    # Y3 = r * (V - X3) - 2 * S1 * J
    Y3 = (r * (V - X3) - 2 * S1 * J) % P

    # Z3 = ((Z1 + Z2)^2 - Z1Z1 - Z2Z2) * H
    z1_plus_z2 = (Z1 + Z2) % P
    Z3 = ((z1_plus_z2 * z1_plus_z2 - Z1Z1 - Z2Z2) * H) % P

    return JacobianPoint(X3, Y3, Z3)


def scalar_multiply(k: int, point: JacobianPoint) -> JacobianPoint:
    """Scalar multiplication using double-and-add."""
    if k == 0 or point.is_infinity():
        return INFINITY

    k = k % N
    if k == 0:
        return INFINITY

    result = INFINITY
    addend = point

    while k > 0:
        if k & 1:
            result = jacobian_add(result, addend)
        addend = jacobian_double(addend)
        k >>= 1

    return result


def to_affine_x(point: JacobianPoint) -> int:
    """Convert Jacobian point to affine x-coordinate: x = X / Z^2."""
    if point.is_infinity():
        raise ValueError("Point at infinity has no affine coordinates")

    Z2 = (point.Z * point.Z) % P
    # pow(x, -1, p) uses CPython's native extended-Euclid modular inverse,
    # which is considerably faster than the Fermat pow(x, p-2, p) exponentiation.
    Z2_inv = pow(Z2, -1, P)
    return (point.X * Z2_inv) % P


# Fixed-base windowed precomputation for the generator point G.
#
# multiply_g() is only ever called with G as the base, so we can trade a
# one-time table build for a big per-call speedup: precompute
# T[i][d] = (d << (4*i)) * G for every 4-bit window position i and digit d,
# after which each scalar multiplication is just ~40 point additions
# (one table lookup + add per nonzero 4-bit digit of the scalar) instead of
# ~160 doublings + ~80 additions of plain double-and-add.
#
# Built lazily on first use rather than at import: the first caller in the
# integration is IdentityResolver's constructor, which HA runs in an
# executor thread (see __init__.py), so the build cost never lands on the
# event loop.
_G_WINDOW_BITS = 4
_G_WINDOWS = (161 + _G_WINDOW_BITS - 1) // _G_WINDOW_BITS + 1  # scalars are < N (161 bits)
_G_TABLE: Optional[list] = None


def _build_g_table() -> list:
    table = []
    base = G
    for _i in range(_G_WINDOWS):
        row: list = [None] * (1 << _G_WINDOW_BITS)
        row[1] = base
        for d in range(2, 1 << _G_WINDOW_BITS):
            row[d] = jacobian_add(row[d - 1], base)
        table.append(row)
        for _ in range(_G_WINDOW_BITS):  # base = base * 2^window_bits
            base = jacobian_double(base)
    return table


def multiply_g(k: int) -> int:
    """Multiply generator point by scalar and return affine x-coordinate."""
    global _G_TABLE
    k = k % N
    if k == 0:
        raise ValueError("Scalar is zero -- result is the point at infinity")
    if _G_TABLE is None:
        _G_TABLE = _build_g_table()

    result = INFINITY
    mask = (1 << _G_WINDOW_BITS) - 1
    i = 0
    while k:
        digit = k & mask
        if digit:
            result = jacobian_add(result, _G_TABLE[i][digit])
        k >>= _G_WINDOW_BITS
        i += 1
    return to_affine_x(result)


class EidGenerator:
    """Generates FMDN Ephemeral Identifiers."""

    ROTATION_PERIOD = ROTATION_PERIOD

    @staticmethod
    def calculate_r(identity_key: bytes, beacon_time_counter: int) -> int:
        """
        Steps 1-4 of EID generation: derive the scalar r from the identity
        key and (masked) beacon time counter. Split out from generate_eid()
        below because fmdn_crypto.py's location-report decryption needs this
        exact same r (it's also R = r*G in that scheme -- an FMDN location
        report's "sender" ephemeral key IS this window's EID point), without
        needing the rest of EID generation.
        """
        if len(identity_key) != 32:
            raise ValueError(f"Identity key must be 32 bytes, got {len(identity_key)}")

        # Step 1: Mask timestamp (zero lower K bits)
        ts_masked = (beacon_time_counter & ((-1) << K)) & 0xFFFFFFFF

        # Step 2: Build 32-byte data block
        # Format: 0xFF*11 + K + ts(4) + 0x00*11 + K + ts(4)
        ts_bytes = ts_masked.to_bytes(4, 'big')
        data = (
            b'\xFF' * 11 +
            bytes([K]) +
            ts_bytes +
            b'\x00' * 11 +
            bytes([K]) +
            ts_bytes
        )

        # Step 3: AES-256-ECB encrypt
        cipher = Cipher(algorithms.AES(identity_key), modes.ECB(), backend=default_backend())
        encryptor = cipher.encryptor()
        r_dash = encryptor.update(data) + encryptor.finalize()

        # Step 4: r = r' mod n (curve order)
        r_dash_int = int.from_bytes(r_dash, 'big')
        return r_dash_int % N

    @staticmethod
    def generate_eid(identity_key: bytes, beacon_time_counter: int) -> bytes:
        """
        Generate an EID for the given identity key and beacon time counter.

        Args:
            identity_key: 32-byte identity key from Google Find My Device
            beacon_time_counter: Seconds since device pairing

        Returns:
            20-byte EID
        """
        r = EidGenerator.calculate_r(identity_key, beacon_time_counter)

        # Step 5: R = r * G (generator point multiplication)
        # Step 6: Return x-coordinate as 20 bytes (big-endian, zero-padded)
        x = multiply_g(r)
        return x.to_bytes(20, 'big')

    @staticmethod
    def get_beacon_time_counter(pair_date: int) -> int:
        """Calculate beacon time counter from current time and pair date."""
        now = int(time.time())
        return now - pair_date

    @staticmethod
    def get_current_eid(identity_key: bytes, pair_date: int) -> bytes:
        """Generate the current EID for the given identity key and pair date."""
        beacon_time_counter = EidGenerator.get_beacon_time_counter(pair_date)
        return EidGenerator.generate_eid(identity_key, beacon_time_counter)

    @staticmethod
    def get_eids_with_offsets(identity_key: bytes, pair_date: int, windows: int = 7) -> list[tuple[bytes, int]]:
        """
        Generate EIDs for current and adjacent time windows to handle clock skew.

        Args:
            identity_key: 32-byte identity key
            pair_date: Unix timestamp when device was paired
            windows: Number of windows to check in each direction (default 7 = ~2 hours)

        Returns:
            List of (EID, window_offset) tuples
        """
        beacon_time_counter = EidGenerator.get_beacon_time_counter(pair_date)
        results = []
        for offset in range(-windows, windows + 1):
            eid = EidGenerator.generate_eid(
                identity_key,
                beacon_time_counter + offset * ROTATION_PERIOD
            )
            results.append((eid, offset))
        return results

    @staticmethod
    def compute_eid(identity_key: bytes, pair_date: int, offset: int = 0) -> bytes:
        """Compute EID for a specific window offset."""
        beacon_time_counter = EidGenerator.get_beacon_time_counter(pair_date)
        return EidGenerator.generate_eid(
            identity_key,
            beacon_time_counter + offset * ROTATION_PERIOD
        )

    @staticmethod
    def find_eid_offset(
        identity_key: bytes,
        pair_date: int,
        advertised_eid: bytes,
        max_hours: int = 24
    ) -> Optional[int]:
        """
        Search for matching EID across a wide time range.

        Args:
            identity_key: 32-byte identity key
            pair_date: Unix timestamp when device was paired
            advertised_eid: The EID to search for
            max_hours: Maximum hours to search in each direction

        Returns:
            Window offset if found, None otherwise
        """
        if len(advertised_eid) != 20:
            return None

        beacon_time_counter = EidGenerator.get_beacon_time_counter(pair_date)
        windows_to_check = (max_hours * 3600) // ROTATION_PERIOD

        for offset in range(-windows_to_check, windows_to_check + 1):
            eid = EidGenerator.generate_eid(
                identity_key,
                beacon_time_counter + offset * ROTATION_PERIOD
            )
            if eid == advertised_eid:
                return offset

        return None


def main():
    """Test EID generation."""
    import sys

    if len(sys.argv) < 3:
        print("Usage: python eid_generator.py <identity_key_hex> <pair_date>")
        print("  identity_key_hex: 64-character hex string (32 bytes)")
        print("  pair_date: Unix timestamp when device was paired")
        sys.exit(1)

    identity_key_hex = sys.argv[1]
    pair_date = int(sys.argv[2])

    # Clean and validate identity key
    identity_key_hex = ''.join(c for c in identity_key_hex.lower() if c in '0123456789abcdef')
    if len(identity_key_hex) != 64:
        print(f"Error: Identity key must be 64 hex chars, got {len(identity_key_hex)}")
        sys.exit(1)

    identity_key = bytes.fromhex(identity_key_hex)

    print(f"Identity key: {identity_key_hex[:16]}...{identity_key_hex[-16:]}")
    print(f"Pair date: {pair_date}")
    print()

    # Generate current EID
    current_eid = EidGenerator.get_current_eid(identity_key, pair_date)
    print(f"Current EID: {current_eid.hex()}")
    print()

    # Generate EIDs with clock skew tolerance
    print("EIDs with clock skew tolerance (±7 windows = ~2 hours):")
    for eid, offset in EidGenerator.get_eids_with_offsets(identity_key, pair_date):
        drift_secs = offset * ROTATION_PERIOD
        marker = " <-- current" if offset == 0 else ""
        print(f"  offset {offset:+3d} ({drift_secs:+6d}s): {eid.hex()}{marker}")


if __name__ == "__main__":
    main()
