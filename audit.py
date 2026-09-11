"""Small, content-free audit records shared by repositories and services."""
from __future__ import annotations

from datetime import datetime, timezone
import json
import sqlite3


def record_audit(connection: sqlite3.Connection, action: str, *, source: str = "system",
                 actor_id: int | None = None, target_type: str = "system",
                 target_id: int | str | None = None, details: dict | None = None) -> None:
    # Callers pass enumerated operations/counts/IDs, never incoming text, prompts,
    # credentials, environment/config dictionaries, or exception response bodies.
    connection.execute(
        """INSERT INTO audit_events
        (actor_principal_id,source,action,target_type,target_id,details,created_at)
        VALUES (?,?,?,?,?,?,?)""",
        (actor_id, source, action, target_type, str(target_id) if target_id is not None else None,
         json.dumps(details or {}, ensure_ascii=False, separators=(",", ":")),
         datetime.now(timezone.utc).isoformat(timespec="seconds")))


def bump_permission_revision(connection: sqlite3.Connection) -> None:
    connection.execute(
        "UPDATE schema_meta SET value=CAST(value AS INTEGER)+1 WHERE key='permission_revision'")
