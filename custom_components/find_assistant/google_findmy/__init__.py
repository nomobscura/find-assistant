"""
Trimmed, HA-embedded extraction of GoogleFindMyTools (c) 2024 Leon Boettger,
licensed GPLv3 -- see ../../../../LICENSE_NOTICE.md for exactly what was
kept/changed and why. Only the code path needed to list an account's Find
My devices and decrypt their identity/account keys from an already-cached
secrets.json is included: no interactive login, no PIN/LSKF key-backup
derivation, no live FCM push listener.

Public entry point: session.GoogleFindMySession(secrets_dict).list_devices().
"""
from .session import GoogleFindMySession, validate_secrets

__all__ = ["GoogleFindMySession", "validate_secrets"]
