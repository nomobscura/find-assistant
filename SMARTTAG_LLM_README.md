# LLM README: Samsung SmartTag support in Find Assistant

**Purpose of this file:** context for any AI assistant picking up work on Samsung
SmartTag support in this repo, written immediately after the session that
scaffolded it (2026-09-08, shipped as `v0.5.13-beta`). It captures what was
researched, what was decided and why, what was explicitly deferred, and where
to look in the code. Read this before touching anything under
`custom_components/find_assistant/smarttag/`.

## What was asked

The user linked https://github.com/KieronQuinn/uTag/wiki and asked to add
Samsung SmartTag support to Find Assistant — an independent Home Assistant
custom integration (see repo root `README.md` and
`custom_components/find_assistant/`) that already tracks Google Find My
(FMDN) tags and classic-Bluetooth (IRK) devices via local BLE scanning through
ESPHome `bluetooth_proxy` nodes, with no dependency on Bermuda.

## Sources consulted (all via WebFetch against the live GitHub wiki — treat
as reverse-engineered/unofficial, not a Samsung spec)

- https://github.com/KieronQuinn/uTag/wiki — index/overview.
- https://github.com/KieronQuinn/uTag/wiki/Authentication — Samsung Account
  login flow (RSA + PBKDF2 + AES encrypted handshake, per-API OAuth token
  exchange). **Not implemented** — see "What was explicitly NOT built" below.
- https://github.com/KieronQuinn/uTag/wiki/Find-API-Calls — confirms the
  public Find API (`api.samsungfind.com`) has **no documented
  location-retrieval endpoint at all**. Only E2E-encryption-key endpoints are
  written up; the wiki itself says "most API calls are currently not
  documented as uTag does not use them."
- https://github.com/KieronQuinn/uTag/wiki/SmartThings-Device-Info-Calls —
  `client.smartthings.com` device info: returns battery level, **not
  location**.
- https://github.com/KieronQuinn/uTag/wiki/Chaser-Location — the crowd-sourced
  mesh network (Apple Find My Network equivalent). Documented only for
  **submitting** other people's tag sightings (`POST /geolocations`), not for
  an owner **querying** their own tag's location.
- https://github.com/KieronQuinn/uTag/wiki/BLE-Privacy-ID — **this is the one
  that made local support possible.** Documents the tag's rotating identifier
  algorithm in enough detail to reimplement (see "Privacy ID algorithm"
  below).
- https://github.com/KieronQuinn/uTag/wiki/BLE-Service-Data — the 20-byte BLE
  advertisement payload layout under service UUID `0xFD5A`, including which
  bytes carry the Privacy ID.
- https://github.com/KieronQuinn/uTag/wiki/BLE-Commands — the ring command is
  a plain single-byte GATT write, but the wiki explicitly states its
  encryption is undocumented even by uTag itself ("out of scope for uTag").

## Key finding that shaped the whole design

**There is no publicly documented way to fetch a SmartTag's live location
from Samsung's cloud at all.** uTag's own app must call an undocumented Find
API endpoint it never wrote up. This is analogous to a prior finding in this
same project for Google Find My (see the main project memory / git history:
a "last known location" sensor for Google tags was built, then reverted,
because Google's list API doesn't return location without a live FCM
listener this project deliberately avoids). Given that, cloud-based location
was ruled out as a near-term goal for SmartTags too.

**However**, local BLE presence detection turned out to be fully
documented and structurally identical to how this integration already
handles Google FMDN tags (`eid_generator.py`, `resolver.py`,
`identity.py`) — a rotating identifier computed from a per-device secret,
matched against live BLE advertisements. That made "local presence/room
tracking, no cloud dependency" a genuinely shippable feature today, unlike
location or ringing.

## Decisions made, and why (in order)

1. **Scope narrowed to local BLE presence only, no cloud sync.** Presented to
   the user as an AskUserQuestion with four options (reverse-engineer the
   missing cloud endpoint / wait on uTag upstream / scaffold now with location
   stubbed / skip entirely). User chose **"scaffold now, stub the location
   call."**
2. **User asked directly: "will there be a way to resolve the MAC address and
   ring the tag locally without SmartThings API?"** Answered from the wiki
   research:
   - MAC/identity resolution: **yes**, fully local, once a tag's Privacy ID
     key material is known (see algorithm below) — same shape as FMDN.
   - Ringing: **no**, not with public information — the GATT write's
     encryption is undocumented even by uTag.
3. **User said "hold off on ring for now."** So button.py was deliberately
   left untouched (it already only creates ring buttons for `KIND_FMDN`
   devices, so SmartTag devices are naturally excluded — no explicit
   exclusion code was needed).
4. **No account-linked cloud sync (unlike Google's `google_findmy/`
   subpackage).** Decided without an explicit question to the user, based on
   risk: Samsung's login is a multi-step encrypted handshake
   (RSA-wrapped PBKDF2/AES payloads) that isn't documented at the byte level
   anywhere public. Implementing it blind risked shipping broken or
   misbehaving code against a real account. Instead, SmartTag key material is
   imported via a **manual JSON file** (`smarttag_devices.json`), mirroring
   the *original* (pre-account-sync) FMDN `devices.json` import path in this
   same integration. This sidesteps needing Samsung auth entirely, at the
   cost of the user having to obtain that JSON externally (currently: by hand
   from a SmartThings Device Info response; a future companion tool in the
   spirit of `google-findmy-device-lister` — see
   https://github.com/nomobscura/google-findmy-device-lister — would be the
   natural next step if this feature gets picked up again).
5. **Followed this project's established versioning convention** (bump
   `manifest.json` by 0.0.1, keep `-beta`, tag + GitHub Release on every
   shipped change) without being asked — this is a standing, previously
   agreed convention for this repo, not something decided fresh this session.
   Shipped as `v0.5.13-beta`, released as a **prerelease** per explicit user
   instruction this round (earlier `-beta` releases in this repo's history
   were NOT all marked prerelease on GitHub — this one specifically was, per
   the user's request in this session).

## Privacy ID algorithm (as implemented — unverified against real hardware)

This is a best-effort reimplementation of wiki prose, not confirmed-working
code (contrast with `eid_generator.py`/`identity.py`'s FMDN/IRK paths, which
ARE confirmed live against real traffic in this project's history). If you
revisit this feature, verifying this against a real tag's advertisements is
the single most valuable next step.

```
derived_key = SHA256(encryption_key[:16] + b"privacy")[:16]
for index in range(pool_size):
    plaintext  = index.to_bytes(2, "big") + seed + index.to_bytes(2, "big")
    ciphertext = AES-CBC(derived_key, iv).encrypt(PKCS7_pad(plaintext))
    privacy_id = ciphertext[:8]   # 8 bytes
    pool.add(privacy_id)
```

- Per-tag secret material: `encryption_key`, `privacy_id_seed`, `pool_size`,
  `iv` — all provisioned by Samsung at pairing time, normally only visible via
  the SmartThings Device Info API (behind the auth flow this project
  deliberately didn't implement).
- **Unlike FMDN's EID, this does NOT rotate on a wall-clock schedule.** Tags
  pick pseudo-randomly from a fixed pool, so the whole pool is precomputed
  once (at config load) and matching is a static set-membership check
  thereafter — no periodic window refresh needed (contrast with
  `resolver.py`'s `maybe_refresh()`/EID window sliding for FMDN).
- Advertised in BLE service data under UUID `0000fd5a-0000-1000-8000-00805f9b34fb`,
  bytes offset `4:12` (8 bytes) of a 20-byte payload.

## What was explicitly NOT built, and why

- **Cloud SmartThings/Find account linking.** Auth flow too complex/
  undocumented to implement with confidence — see decision 4 above.
- **Any live location lookup.** No documented endpoint exists publicly for
  this at all (see "Key finding" above). SmartTag location in this
  integration is exclusively "seen nearby a proxy" (same presence model as
  FMDN/IRK), never GPS/network location.
- **Ringing.** GATT write's encryption is undocumented even by uTag itself.
  Held off per explicit user instruction, but also would have been blocked by
  this same documentation gap regardless.
- **Battery/other SmartThings device metadata.** Not sourced (would require
  the cloud auth this project doesn't have).

## Where the code lives

- `custom_components/find_assistant/smarttag/__init__.py` — module docstring
  explains the "why not cloud sync / why not ringing" reasoning in more
  detail; re-read this first if picking the feature back up.
- `custom_components/find_assistant/smarttag/privacy_id.py` — the algorithm
  above, plus `validate_smarttag_device()` for import-time validation.
- `custom_components/find_assistant/const.py` — `CONF_SMARTTAG_DEVICES`,
  `KIND_SMARTTAG`, `SMARTTAG_SERVICE_UUID`.
- `custom_components/find_assistant/identity.py` — `compute_id()` extended
  for `KIND_SMARTTAG` (hashes `encryption_key + privacy_id_seed`).
- `custom_components/find_assistant/resolver.py` — `IdentityResolver`
  precomputes each SmartTag's Privacy ID pool in its constructor and matches
  advertisements against it in `resolve()`; `manufacturer_for()`/`model_for()`
  hardcode `"Samsung"`/`"SmartTag"` for this kind (no per-device source for
  those fields without cloud access).
- `custom_components/find_assistant/__init__.py` — passes
  `CONF_SMARTTAG_DEVICES` into `IdentityResolver`.
- `custom_components/find_assistant/config_flow.py` — new
  `async_step_import_smarttag` (mirrors `async_step_import_fmdn`), replaces
  the whole SmartTag list on import (matches FMDN's import semantics, not
  Google account sync's add/update-only semantics).
- `custom_components/find_assistant/strings.json` +
  `translations/en.json` — kept byte-identical in this repo's convention;
  update both together.
- `sensor.py`/`button.py` needed **no SmartTag-specific code** — they're
  already generic over `resolver.kind_for()`/`resolver.device_ids`, and
  `button.py`'s ring button is already gated to `KIND_FMDN` only, so SmartTag
  devices get room/RSSI/last-seen sensors automatically and no ring button
  automatically.

## Broader project context (for orientation, not SmartTag-specific)

This repo (`find-assistant`, GitHub org `nomobscura`, separate git identity
from the user's personal `anthonymgil` account) is a from-scratch HA
integration independent of Bermuda. It already supports three other device
identity mechanisms (FMDN via Google account sync or manual devices.json,
classic-Bluetooth IRK, static MAC) using the same
"local BLE proxy scanning + rotating/fixed identifier matching" architecture
that SmartTag support now extends to a fourth mechanism. See this session's
detailed project memory (outside this repo, in the assistant's own memory
store) for the full history of prior features, bugs, and reversions on this
project — most relevantly, the Google Find My "last known location" feature
that was built and then reverted for the same class of reason SmartTag
location was never attempted: **the documented list/sync API doesn't
actually return live location data.**
