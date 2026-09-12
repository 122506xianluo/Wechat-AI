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


def v6(c):
    statements(
        c,
        (
            "ALTER TABLE chats ADD COLUMN context_mode TEXT NOT NULL DEFAULT 'member'",
            "CREATE TABLE conversation_scopes(id INTEGER PRIMARY KEY,chat_id INTEGER NOT NULL REFERENCES chats(id),principal_id INTEGER REFERENCES principals(id),mode TEXT NOT NULL CHECK(mode IN ('private','group_shared','group_member')),revision INTEGER NOT NULL DEFAULT 0,created_at TEXT DEFAULT CURRENT_TIMESTAMP)",
            "CREATE UNIQUE INDEX scope_identity ON conversation_scopes(chat_id,COALESCE(principal_id,0),mode)",
            "ALTER TABLE messages ADD COLUMN scope_id INTEGER REFERENCES conversation_scopes(id)",
            "ALTER TABLE messages ADD COLUMN sender_principal_id INTEGER REFERENCES principals(id)",
            "ALTER TABLE messages ADD COLUMN turn_id TEXT",
            "CREATE INDEX message_scope_turn ON messages(scope_id,turn_id,status)",
            "CREATE TABLE group_summaries(chat_id INTEGER PRIMARY KEY REFERENCES chats(id),content TEXT NOT NULL DEFAULT '',last_message_rowid INTEGER NOT NULL DEFAULT 0,updated_at TEXT DEFAULT CURRENT_TIMESTAMP)",
        ),
    )
    # Pair only adjacent successful legacy incoming/outgoing rows; never guess failures.
    for chat in c.execute("SELECT id,kind FROM chats").fetchall():
        mode = "private" if chat["kind"] == "private" else "group_shared"
        pid = c.execute(
            "SELECT id FROM principals WHERE chat_id=? AND kind='private_user'",
            (chat["id"],),
        ).fetchone()
        scope = c.execute(
            "INSERT INTO conversation_scopes(chat_id,principal_id,mode) VALUES(?,?,?)",
            (chat["id"], pid[0] if pid else None, mode),
        ).lastrowid
        rows = c.execute(
            "SELECT rowid,* FROM messages WHERE chat_id=? ORDER BY created_at,rowid",
            (chat["id"],),
        ).fetchall()
        pending = None
        for row in rows:
            c.execute("UPDATE messages SET scope_id=? WHERE id=?", (scope, row["id"]))
            if row["role"] == "user" and row["status"] == "received":
                pending = row["id"]
            elif row["role"] == "assistant" and row["status"] == "sent" and pending:
                c.execute(
                    "UPDATE messages SET turn_id=? WHERE id IN (?,?)",
                    (pending, pending, row["id"]),
                )
                pending = None
            else:
                pending = None


def v7(c):
    statements(
        c,
        (
            "CREATE TABLE web_accounts(id INTEGER PRIMARY KEY,principal_id INTEGER UNIQUE NOT NULL REFERENCES principals(id),username TEXT UNIQUE NOT NULL,password_hash TEXT NOT NULL,recovery_hash TEXT,enabled INTEGER NOT NULL DEFAULT 1,created_at TEXT DEFAULT CURRENT_TIMESTAMP)",
            "CREATE TABLE web_sessions(token_hash TEXT PRIMARY KEY,account_id INTEGER NOT NULL REFERENCES web_accounts(id) ON DELETE CASCADE,csrf_token TEXT NOT NULL,expires_at REAL NOT NULL,created_at REAL NOT NULL)",
            "CREATE TABLE login_attempts(id INTEGER PRIMARY KEY,username TEXT NOT NULL,source TEXT NOT NULL,at REAL NOT NULL,success INTEGER NOT NULL)",
            "CREATE INDEX login_window ON login_attempts(at,username,source)",
            "CREATE TABLE notifications(id INTEGER PRIMARY KEY,kind TEXT NOT NULL,content TEXT NOT NULL,read_at TEXT,created_at TEXT DEFAULT CURRENT_TIMESTAMP)",
            "CREATE TABLE runtime_settings(key TEXT PRIMARY KEY,value TEXT NOT NULL,revision INTEGER NOT NULL DEFAULT 1,updated_at TEXT DEFAULT CURRENT_TIMESTAMP)",
        ),
    )


def v8(c):
    statements(
        c,
        (
            "CREATE TABLE message_jobs(id TEXT PRIMARY KEY,inbound_message_id TEXT REFERENCES messages(id) ON DELETE CASCADE,scope_id INTEGER NOT NULL REFERENCES conversation_scopes(id),chat_id INTEGER NOT NULL REFERENCES chats(id),principal_id INTEGER REFERENCES principals(id),job_type TEXT NOT NULL DEFAULT 'reply',state TEXT NOT NULL,attempts INTEGER NOT NULL DEFAULT 0,next_attempt_at REAL NOT NULL DEFAULT 0,lease_until REAL,lease_owner TEXT,generated_reply TEXT,last_error TEXT,entered_sending INTEGER NOT NULL DEFAULT 0,payload TEXT NOT NULL,created_at REAL NOT NULL,updated_at REAL NOT NULL)",
            "CREATE INDEX job_ready ON message_jobs(state,next_attempt_at,created_at)",
            "CREATE INDEX job_scope_order ON message_jobs(scope_id,created_at)",
            "CREATE UNIQUE INDEX job_incoming_unique ON message_jobs(inbound_message_id) WHERE inbound_message_id IS NOT NULL",
            "CREATE TABLE inbound_dedup(chat_id INTEGER NOT NULL REFERENCES chats(id),source_key TEXT NOT NULL,job_id TEXT NOT NULL,created_at REAL NOT NULL,PRIMARY KEY(chat_id,source_key))",
            "CREATE TABLE job_attempts(id INTEGER PRIMARY KEY,job_id TEXT NOT NULL REFERENCES message_jobs(id) ON DELETE CASCADE,attempt INTEGER NOT NULL,status TEXT NOT NULL,error TEXT,created_at REAL NOT NULL)",
            "CREATE TABLE job_leases(job_id TEXT PRIMARY KEY REFERENCES message_jobs(id) ON DELETE CASCADE,owner TEXT NOT NULL,expires_at REAL NOT NULL)",
            "CREATE TABLE queue_fairness(chat_id INTEGER PRIMARY KEY REFERENCES chats(id),last_claim REAL NOT NULL)",
        ),
    )


def v9(c):
    statements(
        c,
        (
            "ALTER TABLE messages ADD COLUMN content_type TEXT NOT NULL DEFAULT 'text'",
            "ALTER TABLE messages ADD COLUMN structured_content TEXT NOT NULL DEFAULT '{}'",
            "CREATE TABLE attachments(id TEXT PRIMARY KEY,message_id TEXT REFERENCES messages(id) ON DELETE SET NULL,chat_id INTEGER NOT NULL REFERENCES chats(id),principal_id INTEGER REFERENCES principals(id),content_type TEXT NOT NULL,original_name TEXT NOT NULL,relative_path TEXT,sha256 TEXT,size_bytes INTEGER NOT NULL DEFAULT 0,mime_type TEXT,status TEXT NOT NULL,error TEXT,created_at REAL NOT NULL,expires_at REAL NOT NULL)",
            "CREATE INDEX attachment_retention ON attachments(expires_at,status)",
            "CREATE TABLE attachment_extractions(attachment_id TEXT PRIMARY KEY REFERENCES attachments(id) ON DELETE CASCADE,method TEXT NOT NULL,content TEXT NOT NULL DEFAULT '',status TEXT NOT NULL,updated_at REAL NOT NULL)",
        ),
    )
