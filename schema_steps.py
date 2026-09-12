"""Append-only schema migrations for feature steps 4-11."""

import json
from pathlib import Path


def statements(c, sql):
    for statement in sql:
        c.execute(statement)


def v3(c):
    statements(
        c,
        (
            """CREATE TABLE roles (id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE,
        description TEXT NOT NULL DEFAULT '', system_prompt TEXT NOT NULL,
        model TEXT NOT NULL DEFAULT '', temperature REAL NOT NULL DEFAULT 0.7,
        max_tokens INTEGER NOT NULL DEFAULT 600, max_reply_chars INTEGER NOT NULL DEFAULT 1200,
        enabled INTEGER NOT NULL DEFAULT 1, user_selectable INTEGER NOT NULL DEFAULT 0,
        is_default INTEGER NOT NULL DEFAULT 0, revision INTEGER NOT NULL DEFAULT 1,
        knowledge_mode TEXT NOT NULL DEFAULT 'auto' CHECK(knowledge_mode IN ('off','auto','tool')),
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)""",
            "CREATE UNIQUE INDEX one_default_role ON roles(is_default) WHERE is_default=1",
            """CREATE TABLE role_bindings (id INTEGER PRIMARY KEY, role_id INTEGER NOT NULL REFERENCES roles(id),
        chat_id INTEGER REFERENCES chats(id), principal_id INTEGER REFERENCES principals(id),
        CHECK(principal_id IS NULL OR chat_id IS NOT NULL))""",
            "CREATE UNIQUE INDEX role_binding_scope ON role_bindings(COALESCE(chat_id,0),COALESCE(principal_id,0))",
            """CREATE TABLE role_revisions (id INTEGER PRIMARY KEY, role_id INTEGER NOT NULL REFERENCES roles(id) ON DELETE CASCADE,
        revision INTEGER NOT NULL, payload TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(role_id,revision))""",
        ),
    )
    prompt = "你是微信 AI 助手。简洁回答，尊重隐私，不泄露其他会话。"
    db = c.execute("PRAGMA database_list").fetchone()[2]
    cfg = Path(db).parent.parent / "config.json"
    if cfg.is_file():
        raw = json.loads(cfg.read_text(encoding="utf-8-sig"))
        if isinstance(raw.get("system_prompt"), str) and raw["system_prompt"].strip():
            prompt = raw["system_prompt"]
    c.execute(
        "INSERT INTO roles(name,system_prompt,is_default) VALUES('默认助手',?,1)",
        (prompt,),
    )


def v4(c):
    statements(
        c,
        (
            "ALTER TABLE chats ADD COLUMN approval TEXT NOT NULL DEFAULT 'pending'",
            "ALTER TABLE chats ADD COLUMN visibility TEXT NOT NULL DEFAULT 'unknown'",
            "ALTER TABLE chats ADD COLUMN management_note TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE chats ADD COLUMN baseline_revision INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE chats ADD COLUMN last_message_at TEXT",
            "ALTER TABLE chats ADD COLUMN last_reply_at TEXT",
            "ALTER TABLE chats ADD COLUMN merged_into INTEGER REFERENCES chats(id)",
            "UPDATE chats SET approval='approved'",
            "CREATE TABLE chat_aliases(id INTEGER PRIMARY KEY,chat_id INTEGER NOT NULL REFERENCES chats(id),kind TEXT NOT NULL,name TEXT NOT NULL,created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, UNIQUE(kind,name,chat_id))",
            "CREATE TABLE chat_discoveries(id INTEGER PRIMARY KEY,chat_id INTEGER NOT NULL REFERENCES chats(id),ui_key_hash TEXT NOT NULL,first_seen_at TEXT DEFAULT CURRENT_TIMESTAMP,last_seen_at TEXT DEFAULT CURRENT_TIMESTAMP,UNIQUE(chat_id,ui_key_hash))",
            "CREATE TABLE user_profiles(principal_id INTEGER PRIMARY KEY REFERENCES principals(id),note TEXT NOT NULL DEFAULT '',updated_at TEXT DEFAULT CURRENT_TIMESTAMP)",
        ),
    )


def v5(c):
    statements(
        c,
        (
            "CREATE TABLE principal_aliases(id INTEGER PRIMARY KEY,chat_id INTEGER NOT NULL REFERENCES chats(id),principal_id INTEGER NOT NULL REFERENCES principals(id),name TEXT NOT NULL,normalized_name TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'active',created_at TEXT DEFAULT CURRENT_TIMESTAMP,UNIQUE(chat_id,principal_id,normalized_name))",
            "CREATE INDEX alias_lookup ON principal_aliases(chat_id,normalized_name,status)",
            "CREATE TABLE sender_observations(id INTEGER PRIMARY KEY,chat_id INTEGER NOT NULL REFERENCES chats(id),principal_id INTEGER REFERENCES principals(id),method TEXT NOT NULL,confidence REAL NOT NULL,reason TEXT NOT NULL,features TEXT NOT NULL,created_at TEXT DEFAULT CURRENT_TIMESTAMP)",
            "CREATE INDEX observation_chat ON sender_observations(chat_id,id)",
            "CREATE TABLE member_rosters(chat_id INTEGER PRIMARY KEY REFERENCES chats(id),synced_at TEXT NOT NULL,member_count INTEGER NOT NULL)",
            "INSERT INTO principal_aliases(chat_id,principal_id,name,normalized_name) SELECT chat_id,id,display_name,normalized_name FROM principals WHERE kind='group_member'",
        ),
    )
