#!/usr/bin/env python3
"""
One-time migration for the "BLE Room Presence" -> "Find Assistant" rename
(integration domain: ble_room_presence -> find_assistant, as of v0.5.0-beta).

Renaming the custom_components folder/domain is NOT something Home Assistant
migrates on its own: the config entry, every entity_registry entry, and
every device_registry entry for this integration are all keyed (in part) by
the literal domain string "ble_room_presence". Without this script, after
upgrading you'd see the integration as "not found", all its entities would
show unavailable/orphaned, and you'd have to remove + re-add it from scratch
(losing dashboards/automations that reference the old entity_ids, though
recorder history is keyed by entity_id, not domain, so history itself is
NOT at risk from this specific rename -- only from a full remove+re-add).

What this script does, operating directly on your Home Assistant config
directory's `.storage/` JSON files:
  1. core.config_entries  -- flips the one config entry with
     domain == "ble_room_presence" to domain == "find_assistant".
  2. core.entity_registry -- for every entity with platform ==
     "ble_room_presence": sets platform to "find_assistant", and rewrites
     unique_id's "ble_room_presence_" prefix to "find_assistant_" (matching
     this integration's `f"{DOMAIN}_{device_id}_{suffix}"` scheme).
     entity_id itself is left untouched, so your existing automations/
     dashboards/history keep working unchanged.
  3. core.device_registry -- for every device with an identifiers pair of
     ["ble_room_presence", <id>], rewrites the domain element to
     "find_assistant".

Each of the three files is backed up (`<file>.pre_find_assistant_migration.bak`)
before being modified. Nothing is written in --dry-run mode.

USAGE (Home Assistant must be STOPPED first):
    python migrate_domain_rename.py --config-dir /path/to/homeassistant/config
    python migrate_domain_rename.py --config-dir /path/to/config --dry-run

After it reports success, restart Home Assistant and confirm devices/
entities show up as before under the "Find Assistant" integration.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

BACKUP_SUFFIX = ".pre_find_assistant_migration.bak"


def _load(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def _save(path: Path, payload: dict) -> None:
    with path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
        f.write("\n")


def _backup(path: Path) -> None:
    backup_path = path.with_name(path.name + BACKUP_SUFFIX)
    if backup_path.exists():
        raise FileExistsError(
            f"Backup already exists at {backup_path} -- refusing to overwrite. "
            "Remove it first if you intend to re-run this migration."
        )
    shutil.copy2(path, backup_path)


def migrate_config_entries(payload: dict, old_domain: str, new_domain: str) -> int:
    changed = 0
    for entry in payload.get("data", {}).get("config_entries", []):
        if entry.get("domain") == old_domain:
            entry["domain"] = new_domain
            if entry.get("title") == "BLE Room Presence":
                entry["title"] = "Find Assistant"
            changed += 1
    return changed


def migrate_entity_registry(payload: dict, old_domain: str, new_domain: str) -> int:
    changed = 0
    old_prefix = f"{old_domain}_"
    new_prefix = f"{new_domain}_"
    for entity in payload.get("data", {}).get("entities", []):
        if entity.get("platform") != old_domain:
            continue
        entity["platform"] = new_domain
        unique_id = entity.get("unique_id")
        if isinstance(unique_id, str) and unique_id.startswith(old_prefix):
            entity["unique_id"] = new_prefix + unique_id[len(old_prefix):]
        changed += 1
    return changed


def migrate_device_registry(payload: dict, old_domain: str, new_domain: str) -> int:
    changed = 0
    for device in payload.get("data", {}).get("devices", []):
        identifiers = device.get("identifiers")
        if not identifiers:
            continue
        touched = False
        new_identifiers = []
        for pair in identifiers:
            if isinstance(pair, list) and len(pair) == 2 and pair[0] == old_domain:
                new_identifiers.append([new_domain, pair[1]])
                touched = True
            else:
                new_identifiers.append(pair)
        if touched:
            device["identifiers"] = new_identifiers
            changed += 1
    return changed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config-dir", required=True, help="Path to your Home Assistant config directory (the one containing .storage/)")
    parser.add_argument("--old-domain", default="ble_room_presence")
    parser.add_argument("--new-domain", default="find_assistant")
    parser.add_argument("--dry-run", action="store_true", help="Report what would change without writing anything")
    args = parser.parse_args()

    storage_dir = Path(args.config_dir).expanduser().resolve() / ".storage"
    if not storage_dir.is_dir():
        print(f"ERROR: {storage_dir} not found -- is --config-dir correct?", file=sys.stderr)
        return 1

    targets = {
        "core.config_entries": migrate_config_entries,
        "core.entity_registry": migrate_entity_registry,
        "core.device_registry": migrate_device_registry,
    }

    results = {}
    for filename, migrate_fn in targets.items():
        path = storage_dir / filename
        if not path.is_file():
            print(f"ERROR: {path} not found", file=sys.stderr)
            return 1
        payload = _load(path)
        changed = migrate_fn(payload, args.old_domain, args.new_domain)
        results[filename] = changed
        if changed and not args.dry_run:
            _backup(path)
            _save(path, payload)

    print(f"{'[DRY RUN] ' if args.dry_run else ''}Migration summary ({args.old_domain} -> {args.new_domain}):")
    for filename, changed in results.items():
        print(f"  {filename}: {changed} entr{'y' if changed == 1 else 'ies'} updated")

    if all(c == 0 for c in results.values()):
        print(
            "\nNothing matched -- either this was already migrated, or "
            f"--old-domain/--new-domain don't match what's in {storage_dir}."
        )
    elif args.dry_run:
        print("\nDry run only -- re-run without --dry-run to apply.")
    else:
        print(f"\nBackups written alongside originals with suffix '{BACKUP_SUFFIX}'.")
        print("Restart Home Assistant now and confirm Find Assistant's devices/entities look right.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
