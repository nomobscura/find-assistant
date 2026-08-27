"""
One Google Find My account's session: wraps the cached credentials a user
copies in from `secrets.json` (produced once by an external, interactive
run of the original GoogleFindMyTools login flow -- see
../../../../device_lister/README and ../../../../LICENSE_NOTICE.md) and
knows how to list + decrypt that account's Find My devices.

This is a consolidated, HA-appropriate rewrite of several small
device_lister/Auth + NovaApi modules (aas_token_retrieval.py,
adm_token_retrieval.py, token_retrieval.py, username_provider.py,
fcm_receiver.py, nova_request.py, list_devices.py) -- unlike those, this
class:

  - Takes the secrets dict as a plain constructor argument instead of
    reading a hardcoded Auth/secrets.json path off disk, since here it's
    already stored in (and passed in from) the HA config entry.
  - Never mutates or persists the secrets dict -- everything read from it
    (aas_token, owner_key, the cached fcm_credentials android_id, username)
    is treated as a fixed, externally-refreshed credential.
  - Never starts a live FCM listener/background thread: listing devices
    only needs the *cached* android_id already present in fcm_credentials
    from that one-time login, not a live push connection.
  - Drops the BeautifulSoup dependency nova_request.py used only to
    pretty-print an HTML error page -- errors are just logged/raised with
    the raw response body (truncated).

If Google ever invalidates aas_token (password change, security event,
long dormancy), every call here will start failing with a clear
RuntimeError -- there is no headless way to mint a new one from inside HA.
The fix is the same as when this ever happens outside HA: redo the
interactive GoogleFindMyTools login once and re-upload the fresh
secrets.json via the options flow.
"""
import binascii
import logging

from .identity_key import retrieve_account_key, retrieve_identity_key
from .util import generate_random_uuid

_LOGGER = logging.getLogger(__name__)

# Same values GoogleFindMyTools' Auth/token_retrieval.py / fcm_receiver.py
# use to identify as the official Android Device Manager app -- Google's
# API keys these to that app's package/signing cert, not to us.
_ADM_PACKAGE = "com.google.android.apps.adm"
_CLIENT_SIG = "38918a453d07199354f8b19af05ec6562ced5788"

# Ceiling for the gpsoauth token request -- see _request_scoped_token for why
# this has to be enforced via socket.setdefaulttimeout rather than a normal
# requests timeout= argument. Matches _nova_request's own 30s timeout.
_TOKEN_REQUEST_TIMEOUT_SECONDS = 30

# shared_key is deliberately NOT listed: it's present in a real secrets.json
# but no code path here ever reads it, so requiring it only turned an unused
# field into a hard upload failure for otherwise-valid credential files.
REQUIRED_SECRET_KEYS = ("fcm_credentials", "username", "aas_token", "owner_key")

# DeviceUpdate_pb2.SpotDeviceType values for headphones/earbuds -- these get
# excluded from list_devices()'s output entirely, even though Google returns
# a perfectly valid identity_key for them. Both tracking mechanisms this
# integration supports are confirmed dead ends for this product category:
#   - FMDN EID matching: confirmed live against a real Sony WF-1000XM5 --
#     despite a valid synced identity_key/pair_date, it was never once seen
#     broadcasting FMDN/Eddystone service data sitting right next to a proxy.
#   - IRK matching via account_key: an earlier round auto-registered these as
#     IRK devices too (Google's FMDN spec requires LE Audio devices to rotate
#     via classic RPA), but direct testing confirmed account_key does NOT
#     resolve the device's actual advertised address. The spec itself
#     explains why: RPA generation for LE Audio devices is explicitly
#     "outside this document's scope," governed by ordinary Bluetooth
#     pairing (SMP) between the accessory and a phone -- a secret Google's
#     Find My Device API has no access to at all, and unrelated to
#     account_key. See https://developers.google.com/nearby/fast-pair/
#     specifications/extensions/fmdn's "ID rotation" section.
# So there's currently no path to actually resolving these devices'
# advertisements -- excluded here rather than added as a permanently-
# "not_home" phantom device.
_LE_AUDIO_SPOT_DEVICE_TYPES = frozenset({2, 26})  # DEVICE_TYPE_HEADPHONES, DEVICE_TYPE_EARBUDS


def validate_secrets(secrets: dict) -> None:
    """Raises ValueError with a clear message if secrets.json is missing
    anything list_devices() will need. Called at upload time so a bad file
    is caught immediately rather than surfacing as an opaque failure the
    first time a sync actually runs."""
    if not isinstance(secrets, dict):
        raise ValueError("secrets.json must be a JSON object")
    missing = [k for k in REQUIRED_SECRET_KEYS if not secrets.get(k)]
    if missing:
        raise ValueError(f"secrets.json is missing required key(s): {missing}")
    try:
        secrets["fcm_credentials"]["gcm"]["android_id"]
    except (KeyError, TypeError) as err:
        raise ValueError(
            "secrets.json's fcm_credentials has no cached gcm.android_id -- "
            "this needs to be a secrets.json from a completed GoogleFindMyTools login."
        ) from err
    try:
        bytes.fromhex(secrets["owner_key"])
    except ValueError as err:
        raise ValueError("secrets.json's owner_key is not valid hex") from err


class GoogleFindMySession:
    def __init__(self, secrets: dict):
        validate_secrets(secrets)
        self._secrets = secrets

    @property
    def username(self) -> str:
        return self._secrets["username"]

    @property
    def _android_id(self):
        return self._secrets["fcm_credentials"]["gcm"]["android_id"]

    @property
    def _owner_key(self) -> bytes:
        return bytes.fromhex(self._secrets["owner_key"])

    def _request_scoped_token(self, scope: str, play_services: bool = False) -> str:
        """Mint a short-lived OAuth token for one API scope via gpsoauth.
        Not worth caching across calls -- each is one cheap HTTPS
        round-trip and the result is short-lived anyway.

        Runs on an executor thread (never the event loop). gpsoauth's
        perform_oauth() issues a requests.post() with NO timeout and exposes
        no way to pass one, so a black-holed TCP connection would otherwise
        block this thread forever -- and since the periodic sync re-fires on
        its own schedule regardless, each hang would permanently consume
        another thread from HA's shared pool. socket.setdefaulttimeout() is
        the only lever that reaches inside gpsoauth; it's set and restored
        around just this call. Note it applies process-wide for the duration,
        which is acceptable here because the window is one short HTTPS
        request and the value is a ceiling, not a deadline -- but it's the
        reason this isn't simply wrapped in asyncio.timeout at the caller
        (that would surface the error without ever freeing the thread)."""
        import socket

        import gpsoauth  # imported lazily -- only needed on the executor thread that actually syncs

        app = "com.google.android.gms" if play_services else _ADM_PACKAGE
        previous_timeout = socket.getdefaulttimeout()
        socket.setdefaulttimeout(_TOKEN_REQUEST_TIMEOUT_SECONDS)
        try:
            auth_response = gpsoauth.perform_oauth(
                self.username, self._secrets["aas_token"], self._android_id,
                service=f"oauth2:https://www.googleapis.com/auth/{scope}",
                app=app,
                client_sig=_CLIENT_SIG,
            )
        finally:
            socket.setdefaulttimeout(previous_timeout)
        token = auth_response.get("Auth")
        if not token:
            raise RuntimeError(
                f"Google rejected the account credentials while requesting a '{scope}' token "
                f"({auth_response.get('Error', 'unknown error')}). secrets.json's aas_token may "
                "have expired or been revoked -- redo the GoogleFindMyTools login flow and "
                "re-upload a fresh secrets.json."
            )
        return token

    def _nova_request(self, api_scope: str, hex_payload: str) -> str:
        import requests  # imported lazily -- see _request_scoped_token

        token = self._request_scoped_token("android_device_manager")
        response = requests.post(
            f"https://android.googleapis.com/nova/{api_scope}",
            headers={
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                "Authorization": f"Bearer {token}",
                "Accept-Language": "en-US",
                "User-Agent": "fmd/20006320; gzip",
            },
            data=binascii.unhexlify(hex_payload),
            timeout=30,
        )
        if response.status_code != 200:
            raise RuntimeError(
                f"Nova API '{api_scope}' returned HTTP {response.status_code}: {response.text[:500]!r}"
            )
        return response.content.hex()

    def list_devices(self) -> tuple:
        """Returns (devices, unmatched):
          - devices: [{name, identity_key, pair_date, manufacturer, model},
            ...] for every account device with a usable identity_key
            EXCLUDING LE Audio devices (headphones/earbuds, see
            _LE_AUDIO_SPOT_DEVICE_TYPES -- neither tracking mechanism this
            integration supports works for that category, confirmed live) --
            the rest become tracked FMDN devices. manufacturer/model are
            Google's own free-text labels (e.g. "Pebblebee" / "Pebblebee
            Clip") -- fed into HA's Device Registry manufacturer/model
            fields (__init__.py), None if Google didn't report one.

            Deliberately does NOT include this device's decrypted
            account_key. Nothing consumes it for FMDN-kind devices (only
            the `unmatched` entries below feed identity.merge_irk_candidates),
            and every field here is persisted verbatim into the config entry
            -- i.e. into cleartext .storage/core.config_entries, which lands
            in unencrypted HA backups. Storing a live per-device secret with
            no reader is pure downside. spot_device_type is likewise omitted:
            the LE Audio filtering that used it reads the protobuf field
            directly, before this dict is built.
          - unmatched: [{name, account_key}, ...] for account devices with NO
            usable identity_key -- typically phones/tablets, which the Nova
            API lists alongside real locator tags but which don't carry an
            EIK the same way. account_key is still None here if that also
            wasn't retrievable. These are never added automatically
            (identity_key is what this integration matches FMDN
            advertisements against), but a device's account_key is worth
            surfacing: per room_presence/BERMUDA.md, it's plausible (though
            unvalidated for locator tags, more plausible for phones since
            it's the standard mechanism Android/HA's own Private BLE Device
            integration already uses) as a classic Bluetooth IRK -- see
            config_flow.py's handling of this list for where it's shown.

        identity_key and account_key are decrypted independently -- a device
        missing one doesn't skip the attempt at the other.

        Does NOT fetch location reports: getting one from Google requires a
        separate `nbe_execute_action` "locate" request per device plus a live
        FCM push connection to receive the (async) result -- the plain
        `nbe_list_devices` call this uses doesn't return them as a side
        effect. An earlier revision assumed it did (based on reading
        GoogleFindMyTools' decrypt_locations.py in isolation, without
        checking how its input is actually obtained) and shipped a
        last-known-location sensor that could never populate; removed once
        live logs confirmed zero location data ever came back across many
        real syncs. Doing this properly would mean adding the live FCM
        listener this class deliberately doesn't have (see the module
        docstring) -- a real feature, not a quick fix.
        """
        from .proto import DeviceUpdate_pb2

        wrapper = DeviceUpdate_pb2.DevicesListRequest()
        wrapper.deviceListRequestPayload.type = DeviceUpdate_pb2.DeviceType.SPOT_DEVICE
        wrapper.deviceListRequestPayload.id = generate_random_uuid()
        hex_payload = binascii.hexlify(wrapper.SerializeToString()).decode("utf-8")

        result_hex = self._nova_request("nbe_list_devices", hex_payload)
        if not result_hex:
            raise RuntimeError("Empty response from the Nova device-list API.")

        device_list = DeviceUpdate_pb2.DevicesList()
        device_list.ParseFromString(bytes.fromhex(result_hex))

        owner_key = self._owner_key
        devices = []
        unmatched = []
        for device in device_list.deviceMetadata:
            name = device.userDefinedDeviceName
            registration = device.information.deviceRegistration

            if registration.deviceTypeInformation.deviceType in _LE_AUDIO_SPOT_DEVICE_TYPES:
                # LE Audio devices (headphones/earbuds) are excluded entirely,
                # not just skipped from IRK dual-tracking -- see the constant's
                # docstring above for why neither tracking mechanism this
                # integration supports actually works for this category, so
                # there's no point adding them as a permanently-"not_home"
                # phantom device at all.
                _LOGGER.info(
                    "'%s' is an LE Audio device (headphones/earbuds) -- not added as a "
                    "tracked device (neither FMDN nor IRK matching works for this category)",
                    name,
                )
                continue

            account_key = None
            try:
                account_key = retrieve_account_key(registration, owner_key).hex()
            except ValueError:
                pass  # normal: this device simply has no encryptedAccountKey (e.g. MCU trackers)
            except Exception:
                # Anything else (corrupted ciphertext, AES-GCM tag mismatch, wrong owner_key,
                # etc.) is NOT the normal "no account key" case -- worth knowing about, unlike
                # the expected ValueError above.
                _LOGGER.debug("Could not decrypt account key for '%s'", name, exc_info=True)

            try:
                identity_key = retrieve_identity_key(registration, owner_key)
            except ValueError:
                # Normal: this device (typically a phone/tablet, or some LE Audio
                # accessories) simply has no encryptedIdentityKey -- not every Nova
                # "device" is a real locator tag with EID rotation.
                _LOGGER.info(
                    "'%s' has no usable identity_key -- not added as a tracked device "
                    "(phones and some LE Audio devices are normal here)%s",
                    name, "; it does have an account_key, see the sync results" if account_key else "",
                )
                # Deliberately does NOT dump the raw protobuf message. An
                # earlier revision logged the whole DeviceMetadata at DEBUG as
                # a temporary diagnostic; that message contains the account's
                # full device inventory plus encryptedIdentityKey /
                # encryptedAccountKey ciphertexts, and HA users routinely
                # paste home-assistant.log into public issue trackers after
                # enabling debug logging. The field-level facts below are all
                # the diagnostic value that dump actually provided.
                _LOGGER.debug(
                    "Nova entry for '%s': no usable identity_key "
                    "(device_type=%s, fast_pair_model_id=%s, encrypted_identity_key_len=%d)",
                    name, registration.deviceTypeInformation.deviceType,
                    registration.fastPairModelId,
                    len(registration.encryptedUserSecrets.encryptedIdentityKey),
                )
                unmatched.append({"name": name, "account_key": account_key})
                continue
            except Exception:
                _LOGGER.exception("Skipping '%s' -- could not decrypt its identity key", name)
                unmatched.append({"name": name, "account_key": account_key})
                continue

            devices.append({
                "name": name,
                "identity_key": identity_key.hex(),
                "pair_date": registration.pairDate,
                # For HA's Device Registry manufacturer/model fields -- shown
                # directly on the device page. "" (protobuf's unset-string
                # default) is normalized to None rather than an empty field.
                "manufacturer": registration.manufacturer or None,
                "model": registration.model or None,
            })

        return devices, unmatched
