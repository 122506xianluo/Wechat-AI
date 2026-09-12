"""Ordered, transactional SQLite migrations. Never rebuild a user's database."""
from __future__ import annotations

from contextlib import closing, contextmanager
from datetime import datetime, timezone
import errno
import os
from pathlib import Path
import sqlite3
import time
from typing import Callable, Iterator
import uuid

from schema_steps import v3, v4, v5

SCHEMA_VERSION = 5


class MigrationLock:
    """OS-backed interprocess lock; released by the OS even after a crash."""

    def __init__(self, path: Path, timeout: float = 30.0):
        self.path, self.timeout = path, timeout
        self.file = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.file = self.path.open("a+b")
        if self.file.seek(0, os.SEEK_END) == 0:
            self.file.write(b"0")
            self.file.flush()
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                self.file.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                return self
            except OSError as exc:
                if exc.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                    self.file.close()
                    raise
                if time.monotonic() >= deadline:
                    self.file.close()
                    raise TimeoutError("数据库迁移正被另一进程占用，请稍后重试") from exc
                time.sleep(0.05)

    def __exit__(self, *_):
        # Closing the descriptor releases both msvcrt and flock locks.
        self.file.close()


@contextmanager
def connect(path: Path) -> Iterator[sqlite3.Connection]:
    connection = sqlite3.connect(path, timeout=5.0)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        yield connection
    finally:
        connection.close()


def integrity_check(connection: sqlite3.Connection) -> None:
    result = [row[0] for row in connection.execute("PRAGMA integrity_check")]
    if result != ["ok"] or connection.execute("PRAGMA foreign_key_check").fetchone():
        raise RuntimeError("数据库完整性检查失败；保留原库和备份，停止升级")


def backup_database(connection: sqlite3.Connection, directory: Path, version: int) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    target = directory / f"pre-v{version}-{stamp}-{uuid.uuid4().hex[:8]}.db"
    # backup() includes committed WAL pages; copying the .db file alone does not.
    with closing(sqlite3.connect(target)) as destination:
        connection.backup(destination)
        integrity_check(destination)
    return target


def prune_backups(directory: Path, keep: int = 10) -> None:
    root = directory.resolve()
    files = sorted(
        (p for p in directory.glob("pre-v[0-9]*-*.db")
         if not p.is_symlink() and p.resolve().parent == root),
        key=lambda p: (p.stat().st_mtime_ns, p.name), reverse=True)
    for old in files[keep:]:
        old.unlink()


def _v1(connection: sqlite3.Connection) -> None:
    statements = (
        """CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)""",
        """CREATE TABLE chats (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            kind TEXT NOT NULL CHECK(kind IN ('private','group')),
            name TEXT NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0,1)),
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            UNIQUE(kind,name))""",
        """CREATE TABLE messages (
            id TEXT PRIMARY KEY,
            chat_id INTEGER NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
            role TEXT NOT NULL CHECK(role IN ('user','assistant')),
            direction TEXT NOT NULL CHECK(direction IN ('incoming','outgoing')),
            content TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('pending','received','sent','failed','unknown')),
            source_key TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            error_message TEXT)""",
        "CREATE INDEX idx_messages_chat_created ON messages(chat_id,created_at,id)",
        "CREATE INDEX idx_messages_chat_role_status ON messages(chat_id,role,status)",
    )
    for statement in statements:
        connection.execute(statement)


def _v2(connection: sqlite3.Connection) -> None:
    statements = (
        """CREATE TABLE principals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            kind TEXT NOT NULL CHECK(kind IN ('private_user','group_member','web_account','system')),
            chat_id INTEGER REFERENCES chats(id) ON DELETE RESTRICT,
            display_name TEXT NOT NULL,
            normalized_name TEXT NOT NULL CHECK(length(normalized_name)>0),
            status TEXT NOT NULL DEFAULT 'pending'
                CHECK(status IN ('active','pending','ambiguous','renamed','merged','disabled')),
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL,
            CHECK((kind IN ('private_user','group_member') AND chat_id IS NOT NULL)
                OR (kind IN ('web_account','system') AND chat_id IS NULL)))""",
        """CREATE UNIQUE INDEX idx_principal_chat_name
            ON principals(kind,chat_id,normalized_name) WHERE chat_id IS NOT NULL""",
        """CREATE UNIQUE INDEX idx_principal_global_name
            ON principals(kind,normalized_name) WHERE chat_id IS NULL""",
        """CREATE UNIQUE INDEX idx_private_chat_principal
            ON principals(chat_id) WHERE kind='private_user'""",
        """CREATE TABLE access_grants (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            principal_id INTEGER NOT NULL REFERENCES principals(id) ON DELETE RESTRICT,
            chat_id INTEGER REFERENCES chats(id) ON DELETE RESTRICT,
            access_level TEXT NOT NULL CHECK(access_level IN ('owner','admin','user','blocked')),
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            CHECK(access_level!='owner' OR chat_id IS NULL))""",
        """CREATE UNIQUE INDEX idx_grant_global ON access_grants(principal_id)
            WHERE chat_id IS NULL""",
        """CREATE UNIQUE INDEX idx_grant_chat ON access_grants(principal_id,chat_id)
            WHERE chat_id IS NOT NULL""",
        """CREATE TRIGGER owner_web_only_insert BEFORE INSERT ON access_grants
            WHEN NEW.access_level='owner' AND NOT EXISTS (
                SELECT 1 FROM principals WHERE id=NEW.principal_id AND kind='web_account')
            BEGIN SELECT RAISE(ABORT,'owner requires a web account'); END""",
        """CREATE TRIGGER owner_web_only_update BEFORE UPDATE ON access_grants
            WHEN NEW.access_level='owner' AND NOT EXISTS (
                SELECT 1 FROM principals WHERE id=NEW.principal_id AND kind='web_account')
            BEGIN SELECT RAISE(ABORT,'owner requires a web account'); END""",
        """CREATE TABLE audit_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            actor_principal_id INTEGER REFERENCES principals(id) ON DELETE SET NULL,
            source TEXT NOT NULL CHECK(source IN ('web','wechat','system')),
            action TEXT NOT NULL, target_type TEXT NOT NULL, target_id TEXT,
            details TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL)""",
        "CREATE INDEX idx_audit_created ON audit_events(created_at,id)",
    )
    for statement in statements:
        connection.execute(statement)
    connection.execute("INSERT INTO schema_meta(key,value) VALUES('permission_revision','0')")


MIGRATIONS: dict[int, Callable[[sqlite3.Connection], None]] = {1: _v1, 2: _v2, 3: v3, 4: v4, 5: v5}


def migrate(path: Path, *, target_version: int = SCHEMA_VERSION,
            migrations: dict[int, Callable] | None = None) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    steps = MIGRATIONS if migrations is None else migrations
    with MigrationLock(path.parent / "migration.lock"), connect(path) as connection:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        if version > target_version:
            raise RuntimeError(f"数据库版本 {version} 高于程序版本 {target_version}，拒绝降级")
        connection.execute("PRAGMA journal_mode = WAL")
        integrity_check(connection)
        for next_version in range(version + 1, target_version + 1):
            step = steps[next_version]
            backup_database(connection, path.parent / "backups", next_version)
            try:
                connection.execute("BEGIN IMMEDIATE")
                step(connection)  # execute(), NOT executescript() (implicit commit).
                connection.execute(f"PRAGMA user_version = {int(next_version)}")
                connection.execute(
                    "INSERT INTO schema_meta(key,value) VALUES('schema_version',?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(next_version),))
                integrity_check(connection)
                connection.commit()
            except Exception:
                connection.rollback()
                raise
            integrity_check(connection)
            prune_backups(path.parent / "backups")
