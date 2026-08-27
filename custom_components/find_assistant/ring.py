"""
Rings (and stops ringing) an FMDN tracker over an active BLE GATT connection
through one of this integration's ESPHome bluetooth_proxy scanners.

Protocol: Google's FMDN accessory specification's "owner operations" over the
Fast Pair Additional Data GATT characteristic (verified directly against
https://developers.google.com/nearby/fast-pair/specifications/extensions/fmdn
-- Data ID 0x05, Table 6 for the response format -- not just AirGuard-iOS's
FMDNOwnerOperations.swift (SEEMOO Lab, GPLv3), which this was originally
ported from and which independently confirms the same payload construction):
connect to the tag, read its Fast Pair characteristic to get a one-time
challenge (protocol version + 8-byte nonce), compute an HMAC-SHA256 command
authenticated with a key derived from the tag's own identity_key, and write
it back to the same characteristic. No pairing/bonding needed -- Fast Pair
authenticates each request with this per-command HMAC instead of classic BLE
bonding, which is what makes this doable without ever pairing the tag to
anything.

Ring opcode payload (4 bytes, confirmed against the spec above):
  octet 0: component bitmask -- 0xFF = ring all components, 0x00 = STOP
           ringing (there's no separate "stop" opcode -- same Data ID 0x05,
           this is what AirGuard's own reference code never needed to send,
           since its debug UI only ever starts a ring)
  octets 1-2: timeout in DECISECONDS, big-endian uint16 (ignored when octet
              0 is 0x00) -- must be nonzero, max 10 minutes
  octet 3: volume (0x00 default / 0x01 low / 0x02 medium / 0x03 high)

The accessory replies via a NOTIFICATION on the same characteristic (Table 6):
octet 0 = data ID (0x05), octet 1 = data length, octets 2-9 = an 8-byte
response authentication field (NOT cryptographically verified by this code --
its exact input isn't confirmed here, and getting that wrong risks rejecting
genuinely valid responses, which is a worse failure mode than just trusting
the connection), octets 10+ = 4 bytes of additional data whose first byte is
the reported ringing state: 0x00 started, 0x01 failed to start/stop, 0x02
stopped (timeout), 0x03 stopped (button press), 0x04 stopped (GATT request).
Some accessories may not support this notification at all -- if so, or if it
doesn't arrive within the timeout, the command's actual outcome is unknown
(the GATT write itself still succeeded) rather than assumed successful.
"""
import asyncio
import hashlib
import hmac
import logging

from homeassistant.components import bluetooth
from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

# Google's Fast Pair service/characteristic -- 0000fe2c-... is the standard
# 128-bit expansion of the 16-bit Fast Pair service UUID (same base UUID
# pattern as FMDN_SERVICE_UUID/EDDYSTONE_SERVICE_UUID in const.py). Only the
# characteristic UUID is actually needed for the GATT calls below -- bleak
# resolves it against whatever services the connection discovers -- the
# service UUID is kept here purely as a documentation/cross-reference aid.
FAST_PAIR_SERVICE_UUID = "0000fe2c-0000-1000-8000-00805f9b34fb"
FAST_PAIR_AUTH_CHAR_UUID = "fe2c1238-8366-4814-8eb0-01de32100bea"

_RING_DATA_ID = 0x05
_RING_ALL_COMPONENTS = 0xFF
_RING_STOP = 0x00
_RING_VOLUME_DEFAULT = 0x00
_RINGING_KEY_OPERATION_BYTE = 0x02  # FMDN spec's per-operation key derivation tag

# 60 seconds -- matches AirGuard-iOS's own reference value (600 deciseconds).
# The tag auto-stops on its own after this even if never explicitly told to
# stop, so button.py's own UI state can't get stuck showing "Stop Ringing"
# forever if a stop notification (see module docstring) never arrives.
RING_TIMEOUT_SECONDS = 60.0
_RING_TIMEOUT_DECISECONDS = int(RING_TIMEOUT_SECONDS * 10)

_NOTIFY_WAIT_SECONDS = 8.0

_RING_STATUS_NAMES = {
    0x00: "started",
    0x01: "failed",
    0x02: "stopped_timeout",
    0x03: "stopped_button",
    0x04: "stopped_gatt",
}


def _derive_ringing_key(identity_key: bytes) -> bytes:
    """8-byte key used to authenticate ring commands specifically -- derived
    from the tag's identity_key the same way every other FMDN "owner
    operation" key is (recovery/tracking use different trailing bytes, not
    needed here)."""
    return hashlib.sha256(identity_key + bytes([_RINGING_KEY_OPERATION_BYTE])).digest()[:8]


def _build_operation_payload(auth_data: bytes, operation_key: bytes, data_id: int, additional_data: bytes) -> bytes:
    """Builds the write payload for any owner operation from the tag's
    challenge (auth_data: 1-byte protocol version + 8-byte nonce, as read
    from FAST_PAIR_AUTH_CHAR_UUID) and the operation-specific key."""
    if len(auth_data) < 9:
        raise ValueError(f"Authentication data too short ({len(auth_data)} bytes, need at least 9)")
    protocol_version = auth_data[0]
    nonce = auth_data[1:9]
    data_length = 8 + len(additional_data)  # 8-byte truncated HMAC + additional data

    message = bytes([protocol_version]) + nonce + bytes([data_id, data_length]) + additional_data
    one_time_auth_key = hmac.new(operation_key, message, hashlib.sha256).digest()[:8]

    return bytes([data_id, data_length]) + one_time_auth_key + additional_data


def _build_ring_payload(
    auth_data: bytes, ringing_key: bytes, component_bitmask: int,
    timeout_deciseconds: int = 0, volume: int = _RING_VOLUME_DEFAULT,
) -> bytes:
    additional_data = bytes([
        component_bitmask,
        (timeout_deciseconds >> 8) & 0xFF,
        timeout_deciseconds & 0xFF,
        volume,
    ])
    return _build_operation_payload(auth_data, ringing_key, _RING_DATA_ID, additional_data)


def _parse_ring_response(response: bytes) -> dict:
    """Parses a Table-6-shaped notification response. Returns
    {"confirmed": True, "status": <name>, "raw": bytes} on a recognizable
    response, or {"confirmed": False, "raw": bytes} if it doesn't match the
    expected shape (unknown accessory firmware, corrupted notification,
    etc.) -- callers treat that the same as "no response at all", not as an
    error, since the GATT write itself already succeeded either way."""
    if len(response) < 11 or response[0] != _RING_DATA_ID:
        _LOGGER.debug("Unexpected ring response format: %s", response.hex())
        return {"confirmed": False, "raw": response}
    status_byte = response[10]
    status = _RING_STATUS_NAMES.get(status_byte, f"unknown(0x{status_byte:02x})")
    return {"confirmed": True, "status": status, "raw": response}


async def _send_ring_command(
    hass: HomeAssistant, address: str, identity_key: bytes,
    component_bitmask: int, timeout_deciseconds: int, volume: int,
) -> dict:
    """Connects to `address` (the tag's current advertising MAC -- see
    DevicePresence.current_mac) via whichever connectable proxy currently
    has it in range, issues one Fast Pair Ring-opcode command (start or
    stop, depending on component_bitmask), and returns whatever confirmation
    (if any) the tag sent back -- see _parse_ring_response(). Raises on
    genuine failure to even attempt the command (no connectable proxy,
    connection error, GATT error); a confirmed "failed" status from the tag
    itself is returned, not raised, so callers can decide how to react."""
    from bleak_retry_connector import BleakClientWithServiceCache, establish_connection

    ble_device = bluetooth.async_ble_device_from_address(hass, address, connectable=True)
    if ble_device is None:
        raise RuntimeError(
            f"No connectable Bluetooth proxy currently has {address} in range "
            "-- the tag may be out of range, or only seen by a non-connectable scanner."
        )

    client = await establish_connection(BleakClientWithServiceCache, ble_device, address)
    try:
        auth_data = bytes(await client.read_gatt_char(FAST_PAIR_AUTH_CHAR_UUID))
        ringing_key = _derive_ringing_key(identity_key)
        payload = _build_ring_payload(auth_data, ringing_key, component_bitmask, timeout_deciseconds, volume)
        _LOGGER.debug("Sending ring command to %s -- auth_data=%s payload=%s", address, auth_data.hex(), payload.hex())

        loop = asyncio.get_running_loop()
        response_received = asyncio.Event()
        response_holder: dict = {}

        def _on_notify(_handle, data) -> None:
            response_holder["data"] = bytes(data)
            loop.call_soon_threadsafe(response_received.set)

        notify_supported = True
        try:
            await client.start_notify(FAST_PAIR_AUTH_CHAR_UUID, _on_notify)
        except Exception:
            _LOGGER.debug(
                "Accessory doesn't support notifications on the auth characteristic -- "
                "proceeding without a confirmed response", exc_info=True,
            )
            notify_supported = False

        await client.write_gatt_char(FAST_PAIR_AUTH_CHAR_UUID, payload, response=True)

        if not notify_supported:
            return {"confirmed": False}

        try:
            await asyncio.wait_for(response_received.wait(), timeout=_NOTIFY_WAIT_SECONDS)
        except asyncio.TimeoutError:
            _LOGGER.debug("No ring-status notification received within %.0fs", _NOTIFY_WAIT_SECONDS)
            return {"confirmed": False}
        finally:
            try:
                await client.stop_notify(FAST_PAIR_AUTH_CHAR_UUID)
            except Exception:
                pass  # best-effort cleanup -- the connection is about to be torn down anyway

        return _parse_ring_response(response_holder["data"])
    finally:
        await client.disconnect()


async def ring_device(hass: HomeAssistant, address: str, identity_key: bytes) -> dict:
    """Starts ringing all components at default volume for RING_TIMEOUT_SECONDS
    (the tag stops on its own after that if never explicitly stopped).
    Raises RuntimeError if the tag confirms the command failed; returns the
    parsed response dict otherwise (which may have confirmed=False if no
    confirmation was available at all -- see module docstring)."""
    result = await _send_ring_command(
        hass, address, identity_key, _RING_ALL_COMPONENTS, _RING_TIMEOUT_DECISECONDS, _RING_VOLUME_DEFAULT
    )
    if result.get("confirmed") and result["status"] == "failed":
        raise RuntimeError("The tag reported that it failed to start ringing")
    return result


async def stop_ring_device(hass: HomeAssistant, address: str, identity_key: bytes) -> dict:
    """Stops an in-progress ring -- component bitmask 0x00 per the spec
    ("if ring operation is set to 0x00, the timeout is ignored"), same Data
    ID as starting one."""
    result = await _send_ring_command(hass, address, identity_key, _RING_STOP, 0, 0)
    if result.get("confirmed") and result["status"] == "failed":
        raise RuntimeError("The tag reported that it failed to stop ringing")
    return result
