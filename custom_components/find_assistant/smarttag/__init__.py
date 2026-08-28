"""
Samsung SmartTag (Galaxy SmartTag/SmartTag2, uTag-compatible) local BLE
presence support -- a third identity/resolution mechanism alongside FMDN and
IRK (see ../resolver.py), added independently of the Google Find My
integration.

Unlike FMDN (Google) and IRK (classic Bluetooth), there is currently NO
cloud-sync path here comparable to google_findmy/'s account linking. That's
deliberate, not an oversight:

  - Samsung's SmartThings/Find account login is a multi-step encrypted
    handshake (RSA-wrapped PBKDF2/AES payloads, per-API OAuth token exchange)
    that isn't documented in enough byte-level detail anywhere public to
    reimplement with confidence here -- see
    https://github.com/KieronQuinn/uTag/wiki/Authentication. Shipping a
    guessed implementation of a real account's crypto login risked either
    silently failing or, worse, misbehaving against Samsung's servers.
  - The one thing this module actually needs per tag -- its Privacy ID key
    material (encryption_key/privacy_id_seed/pool_size/iv) -- is normally
    only obtainable via that same login, through SmartThings' Device Info
    API. There is no public "list my SmartTags' key material" endpoint
    equivalent to Google's Nova API that this integration could call directly
    without doing that login itself.

So for now, this only supports a one-time/manual "smarttag_devices.json"
import (config_flow.py's import_smarttag step), analogous to the FMDN
devices.json import path -- the key material has to be obtained externally
(e.g. a future companion tool in the spirit of GoogleFindMyDeviceLister, or
by hand from a SmartThings Device Info response) and handed to this
integration as a JSON file. See privacy_id.py for what's actually done with
it once imported.

Explicitly NOT implemented (see Find Assistant's README/CLAUDE notes for
current status):
  - Cloud SmartThings/Find account linking (see above).
  - Live location lookups from Samsung's Find API -- the wiki documents no
    location-retrieval endpoint at all, only Chaser-network crowd-sourced
    *submission* (see privacy_id.py's module docstring for the full
    breakdown). Location for SmartTags is therefore local-BLE-only, exactly
    like FMDN/IRK: "seen nearby" via privacy ID matching, nothing else.
  - Ringing. The BLE ring command itself is a plain single-byte GATT write
    (0xFD5A service, DEE30001-182D-5496-B1AD-14F216324184 characteristic),
    but uTag's own wiki states the write is encrypted and that "encryption &
    decryption of commands is out of scope for uTag" -- i.e. even uTag
    doesn't document that cipher. Deliberately held off rather than shipping
    a guessed implementation of an authenticated command channel.
"""
from .privacy_id import generate_privacy_id_pool, validate_smarttag_device

__all__ = ["generate_privacy_id_pool", "validate_smarttag_device"]
