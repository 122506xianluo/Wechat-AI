"""History-only identity merge. Never inherit permissions, role or KB grants."""


def move_history(c, source_chat, target_chat, source_pid, target_pid):
    # Check both identities explicitly: unknown cannot be silently made resendable.
    for chat, pid in ((source_chat, source_pid), (target_chat, target_pid)):
        if c.execute(
            "SELECT 1 FROM message_jobs WHERE chat_id=? AND principal_id=? AND state NOT IN ('sent','failed','cancelled')",
            (chat, pid),
        ).fetchone():
            raise ValueError("合并前请处理/取消双方全部未完成任务；unknown必须人工核对")
    scopes = c.execute(
        "SELECT * FROM conversation_scopes WHERE chat_id=? AND principal_id=?",
        (source_chat, source_pid),
    ).fetchall()
    for scope in scopes:
        c.execute(
            "INSERT OR IGNORE INTO conversation_scopes(chat_id,principal_id,mode) VALUES(?,?,?)",
            (target_chat, target_pid, scope["mode"]),
        )
        dest = c.execute(
            "SELECT id FROM conversation_scopes WHERE chat_id=? AND principal_id=? AND mode=?",
            (target_chat, target_pid, scope["mode"]),
        ).fetchone()[0]
        for table in ("messages", "message_jobs", "tool_runs"):
            c.execute(
                "UPDATE " + table + " SET scope_id=? WHERE scope_id=?",
                (dest, scope["id"]),
            )
        c.execute(
            "UPDATE conversation_scopes SET revision=revision+1 WHERE id IN (?,?)",
            (dest, scope["id"]),
        )
    c.execute(
        "UPDATE messages SET chat_id=?,sender_principal_id=? WHERE chat_id=? AND sender_principal_id=?",
        (target_chat, target_pid, source_chat, source_pid),
    )
    c.execute(
        "UPDATE message_jobs SET chat_id=?,principal_id=? WHERE chat_id=? AND principal_id=?",
        (target_chat, target_pid, source_chat, source_pid),
    )
    c.execute(
        "UPDATE attachments SET chat_id=?,principal_id=? WHERE chat_id=? AND principal_id=?",
        (target_chat, target_pid, source_chat, source_pid),
    )
    c.execute(
        "UPDATE tool_runs SET principal_id=? WHERE principal_id=?",
        (target_pid, source_pid),
    )
