# License / Attribution Notice

`custom_components/find_assistant/google_findmy/` in this project is a
further-trimmed re-extraction of a subset of
[GoogleFindMyTools](https://github.com/leonboe1/GoogleFindMyTools) by
Leon Böttger (© 2024, licensed under **GPLv3**, see [LICENSE](LICENSE)).
Because this project vendors and modifies that code, it is distributed
under the same license.

Embedded directly in the Find Assistant HA integration so it can sync a
linked Google account's device list itself instead of requiring a manual
`devices.json` re-import from a separate tool.

Consolidates the read-cached-credentials-only code path (no interactive
login, no LSKF/PIN cloud-key-backup derivation -- reads an already-populated
`secrets.json` only) into fewer, HA-appropriate files:

| File | Replaces / consolidates |
|---|---|
| `session.py` | `Auth/aas_token_retrieval.py`, `Auth/adm_token_retrieval.py`, `Auth/token_retrieval.py`, `Auth/username_provider.py`, `NovaApi/nova_request.py`, upstream's `list_devices.py` request/extract logic |
| `identity_key.py` | `NovaApi/identity_key.py`, with `SpotApi/CreateBleDevice/util.py`'s `flip_bits` and `config.py`'s `mcu_fast_pair_model_id` inlined |
| `crypto.py` | `KeyBackup/cloud_key_decryptor.py`, unmodified logic |
| `util.py` | `NovaApi/util.py`, unmodified |
| `proto/` | `ProtoDecoders/*_pb2.py`, unmodified generated code (only the cross-file import path was adjusted for this location) |

Deliberately left out relative to the full upstream project:
- No live FCM listener/background thread at all (`Auth/fcm_receiver.py`'s
  `FcmPushClient`/`firebase_messaging` machinery) -- listing devices only
  needs the `android_id` already cached in a completed `secrets.json`'s
  `fcm_credentials`, never a live push connection.
- No `BeautifulSoup`/`beautifulsoup4` dependency -- `nova_request.py`'s use
  of it was just to pretty-print an HTML error page; errors are logged/
  raised with the raw response body instead.
- The secrets dict is passed in directly (from the HA config entry, where
  a user uploads it via the options flow) instead of being read from a
  hardcoded `Auth/secrets.json` path on disk -- nothing in `google_findmy/`
  touches the filesystem itself.
- No interactive credential derivation: if `secrets.json`'s `aas_token` is
  ever invalidated by Google, there's no headless way to mint a new one
  from inside Home Assistant -- redo the original GoogleFindMyTools
  interactive login and re-upload a fresh `secrets.json` via the options
  flow.
