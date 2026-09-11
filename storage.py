"""SQLite persistence for chat metadata, messages, and bounded LLM history."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
from typing import Iterator
import uuid


from audit import record_audit
from migrations import SCHEMA_VERSION, migrate


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Storage:
    """Small connection-per-operation SQLite repository.

    The bot and the web panel run in separate processes. Short-lived connections,
    WAL mode, and a busy timeout keep reads and maintenance operations safe while
    the bot is polling.
    """

    def __init__(self, root: Path):
        self.root = Path(root)
        self.path = self.root / "data" / "wechat_ai.db"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA busy_timeout = 5000")
            yield connection
        finally:
            connection.close()

    def _initialize(self) -> None:
        migrate(self.path)

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Short atomic write, including audit; no UI or network inside it."""
        with self._connection() as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                yield connection
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    def ensure_chat(self, kind: str, name: str) -> int:
        if kind not in ("private", "group") or not name:
            raise ValueError("会话类型或名称无效")
        now = utc_now()
        with self._connection() as connection:
            connection.execute(
                "INSERT INTO chats(kind, name, created_at, updated_at) VALUES(?, ?, ?, ?) "
                "ON CONFLICT(kind, name) DO UPDATE SET updated_at=excluded.updated_at",
                (kind, name, now, now))
            row = connection.execute(
                "SELECT id FROM chats WHERE kind=? AND name=?", (kind, name)
            ).fetchone()
            connection.commit()
            return int(row[0])

    def register_targets(self, private_chats: list[str], groups: list[str]) -> None:
        for name in private_chats:
            self.ensure_chat("private", name)
        for name in groups:
            self.ensure_chat("group", name)

    def chat_id(self, kind: str, name: str) -> int:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT id FROM chats WHERE kind=? AND name=?", (kind, name)
            ).fetchone()
        if row is None:
            return self.ensure_chat(kind, name)
        return int(row[0])

    def add_incoming(
        self, kind: str, name: str, content: str, source_key: str = ""
    ) -> str:
        message_id = uuid.uuid4().hex
        now = utc_now()
        chat_id = self.ensure_chat(kind, name)
        with self._connection() as connection:
            connection.execute(
                """INSERT INTO messages
                (id, chat_id, role, direction, content, status, source_key,
                 created_at, updated_at, error_message)
                VALUES (?, ?, 'user', 'incoming', ?, 'pending', ?, ?, ?, NULL)""",
                (message_id, chat_id, content, source_key or None, now, now))
            connection.commit()
        return message_id

    def add_assistant(
        self, kind: str, name: str, content: str, status: str = "sent",
        error_message: str = ""
    ) -> str:
        if status not in ("sent", "failed", "unknown"):
            raise ValueError("assistant 状态无效")
        message_id = uuid.uuid4().hex
        now = utc_now()
        chat_id = self.ensure_chat(kind, name)
        with self._connection() as connection:
            connection.execute(
                """INSERT INTO messages
                (id, chat_id, role, direction, content, status, source_key,
                 created_at, updated_at, error_message)
                VALUES (?, ?, 'assistant', 'outgoing', ?, ?, NULL, ?, ?, ?)""",
                (message_id, chat_id, content, status, now, now, error_message or None))
            connection.commit()
        return message_id

    def mark_message(self, message_id: str, status: str, error_message: str = "") -> None:
        if status not in ("pending", "received", "sent", "failed", "unknown"):
            raise ValueError("消息状态无效")
        now = utc_now()
        with self._connection() as connection:
            connection.execute(
                "UPDATE messages SET status=?, updated_at=?, error_message=? WHERE id=?",
                (status, now, error_message or None, message_id))
            connection.commit()

    def complete_turn(self, incoming_id: str, kind: str, name: str, content: str) -> str:
        """Commit an incoming turn and its verified outgoing answer together."""
        assistant_id = uuid.uuid4().hex
        now = utc_now()
        chat_id = self.chat_id(kind, name)
        with self.transaction() as connection:
            cursor = connection.execute(
                "UPDATE messages SET status='received', updated_at=? "
                "WHERE id=? AND chat_id=? AND status='pending' AND role='user' AND direction='incoming'",
                (now, incoming_id, chat_id))
            if cursor.rowcount != 1:
                raise ValueError("入站记录不存在、已结束或不属于当前会话")
            connection.execute(
                """INSERT INTO messages
                (id, chat_id, role, direction, content, status, source_key,
                 created_at, updated_at, error_message)
                VALUES (?, ?, 'assistant', 'outgoing', ?, 'sent', NULL, ?, ?, NULL)""",
                (assistant_id, chat_id, content, now, now))
        return assistant_id

    def recover_incomplete(self) -> int:
        """Make interrupted pre-send work non-retryable on the next startup."""
        now = utc_now()
        with self._connection() as connection:
            cursor = connection.execute(
                "UPDATE messages SET status='failed', updated_at=?, error_message=? WHERE status='pending'",
                (now, "previous_run_interrupted"))
            connection.commit()
            return cursor.rowcount

    def history(self, kind: str, name: str, context_turns: int) -> list[dict[str, str]]:
        """Return only completed messages, bounded to context_turns turns."""
        chat_id = self.chat_id(kind, name)
        limit = max(1, int(context_turns)) * 2
        with self._connection() as connection:
            rows = connection.execute(
                """SELECT role, content FROM messages
                   WHERE chat_id=?
                     AND ((role='user' AND direction='incoming' AND status='received')
                       OR (role='assistant' AND direction='outgoing' AND status='sent'))
                   ORDER BY created_at DESC, rowid DESC LIMIT ?""",
                (chat_id, limit),
            ).fetchall()
        return [{"role": row["role"], "content": row["content"]}
                for row in reversed(rows)]

    def stats(self) -> dict:
        with self._connection() as connection:
            chats = connection.execute("SELECT COUNT(*) FROM chats").fetchone()[0]
            messages = connection.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
            failed = connection.execute(
                "SELECT COUNT(*) FROM messages WHERE status='failed'").fetchone()[0]
            unknown = connection.execute(
                "SELECT COUNT(*) FROM messages WHERE status='unknown'").fetchone()[0]
        try:
            size = self.path.stat().st_size
        except OSError:
            size = 0
        return {
            "ok": True,
            "path": str(self.path),
            "size_bytes": size,
            "chat_count": int(chats),
            "message_count": int(messages),
            "failed_count": int(failed),
            "unknown_count": int(unknown),
            "schema_version": SCHEMA_VERSION,
        }

    def list_chats(self) -> list[dict]:
        with self._connection() as connection:
            rows = connection.execute(
                """SELECT c.id, c.kind, c.name, c.enabled, c.updated_at,
                          COUNT(m.id) AS message_count
                   FROM chats c LEFT JOIN messages m ON m.chat_id=c.id
                   GROUP BY c.id ORDER BY c.kind, c.name"""
            ).fetchall()
        return [dict(row) for row in rows]

    def clear_chat(self, kind: str, name: str, *, actor_id: int | None = None,
                   source: str = "system") -> int:
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT id FROM chats WHERE kind=? AND name=?", (kind, name)).fetchone()
            if row is None:
                raise ValueError("会话不存在")
            cursor = connection.execute("DELETE FROM messages WHERE chat_id=?", (row[0],))
            deleted = cursor.rowcount
            record_audit(connection, "context.clear", source=source, actor_id=actor_id,
                         target_type="chat", target_id=row[0], details={"deleted": deleted})
            return deleted

    def clear_all_history(self, *, actor_id: int | None = None, source: str = "system") -> int:
        with self.transaction() as connection:
            cursor = connection.execute("DELETE FROM messages")
            deleted = cursor.rowcount
            record_audit(connection, "context.clear_all", source=source, actor_id=actor_id,
                         details={"deleted": deleted})
            return deleted

    def close(self) -> None:
        """Compatibility hook; operations intentionally use short-lived connections."""
        return None
