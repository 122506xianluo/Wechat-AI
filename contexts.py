"""Complete turn histories scoped by chat and exact approved principal."""

from audit import record_audit, bump_permission_revision
from roles import require_manager
from permissions import _decision, PermissionDenied


class Contexts:
    def __init__(self, storage):
        self.storage = storage

    def resolve(self, chat_id, principal_id):
        with self.storage.transaction() as c:
            chat = c.execute("SELECT * FROM chats WHERE id=?", (chat_id,)).fetchone()
            if not chat:
                raise ValueError("会话不存在")
            if chat["kind"] == "private":
                mode = "private"
            elif chat["context_mode"] == "shared":
                mode = "group_shared"
                principal_id = None
            else:
                mode = "group_member"
                if not principal_id:
                    raise PermissionDenied("群成员身份未知")
            if principal_id:
                p = c.execute(
                    "SELECT chat_id FROM principals WHERE id=?", (principal_id,)
                ).fetchone()
                if not p or p[0] != chat_id:
                    raise PermissionDenied("成员不属于此会话")
            # Preserve the legacy private scope even if it was created before identity import.
            if mode == "private":
                c.execute(
                    "UPDATE conversation_scopes SET principal_id=? WHERE chat_id=? AND mode='private' AND principal_id IS NULL",
                    (principal_id, chat_id),
                )
            row = c.execute(
                "SELECT * FROM conversation_scopes WHERE chat_id=? AND principal_id IS ? AND mode=?",
                (chat_id, principal_id, mode),
            ).fetchone()
            if not row:
                sid = c.execute(
                    "INSERT INTO conversation_scopes(chat_id,principal_id,mode) VALUES(?,?,?)",
                    (chat_id, principal_id, mode),
                ).lastrowid
                row = c.execute(
                    "SELECT * FROM conversation_scopes WHERE id=?", (sid,)
                ).fetchone()
            return dict(row)

    def history(self, scope_id, turns):
        limit = max(0, min(50, int(turns)))
        if not limit:
            return []
        with self.storage._connection() as c:
            rows = c.execute(
                """SELECT u.content question,a.content answer FROM messages u JOIN messages a
              ON a.turn_id=u.turn_id AND a.scope_id=u.scope_id AND a.role='assistant' AND a.status='sent'
              WHERE u.scope_id=? AND u.role='user' AND u.status='received' AND u.direction='incoming'
              ORDER BY u.created_at DESC,u.rowid DESC LIMIT ?""",
                (scope_id, limit),
            ).fetchall()
        result = []
        for row in reversed(rows):
            result.extend(
                [
                    {"role": "user", "content": row["question"]},
                    {"role": "assistant", "content": row["answer"]},
                ]
            )
        return result

    def attach(self, message_id, scope, principal_id):
        with self.storage.transaction() as c:
            c.execute(
                "UPDATE messages SET scope_id=?,sender_principal_id=?,turn_id=? WHERE id=? AND chat_id=?",
                (scope["id"], principal_id, message_id, message_id, scope["chat_id"]),
            )

    def list(self):
        with self.storage._connection() as c:
            return [
                dict(r)
                for r in c.execute(
                    "SELECT s.*,c.name chat_name,p.display_name,COUNT(m.id) message_count FROM conversation_scopes s JOIN chats c ON c.id=s.chat_id LEFT JOIN principals p ON p.id=s.principal_id LEFT JOIN messages m ON m.scope_id=s.id GROUP BY s.id ORDER BY s.id"
                )
            ]

    def set_mode(self, chat_id, mode, actor, confirm=False):
        if mode not in ("member", "shared", "hybrid") or (
            mode == "hybrid" and confirm is not True
        ):
            raise ValueError("hybrid 将跨成员共享群公开摘要，必须明确确认")
        with self.storage.transaction() as c:
            require_manager(c, actor)
            c.execute(
                "UPDATE chats SET context_mode=?,baseline_revision=baseline_revision+1 WHERE id=? AND kind='group'",
                (mode, chat_id),
            )
            record_audit(
                c,
                "context.mode",
                source=actor.source,
                actor_id=actor.principal_id,
                target_type="chat",
                target_id=chat_id,
                details={"mode": mode},
            )
            bump_permission_revision(c)

    def clear(self, scope_id, actor):
        with self.storage.transaction() as c:
            row = c.execute(
                "SELECT * FROM conversation_scopes WHERE id=?", (scope_id,)
            ).fetchone()
            if not row:
                raise ValueError("上下文不存在")
            if actor.source == "wechat":
                access = _decision(c, actor.principal_id, row["chat_id"])
                if (
                    not access.allowed
                    or row["principal_id"] != actor.principal_id
                    or row["mode"] == "group_shared"
                ):
                    raise PermissionDenied("只能清空自己的独立上下文")
            else:
                require_manager(c, actor)
            if c.execute(
                "SELECT 1 FROM sqlite_master WHERE name='message_jobs'"
            ).fetchone():
                if c.execute(
                    "SELECT 1 FROM message_jobs WHERE scope_id=? AND state IN ('processing','ready_to_send','sending')",
                    (scope_id,),
                ).fetchone():
                    raise ValueError("当前范围有进行中的任务，请先取消或等待结束")
            if c.execute(
                "SELECT 1 FROM messages WHERE scope_id=? AND status='pending'",
                (scope_id,),
            ).fetchone():
                raise ValueError("当前范围有未完成的消息")
            count = c.execute(
                "DELETE FROM messages WHERE scope_id=?", (scope_id,)
            ).rowcount
            c.execute(
                "UPDATE conversation_scopes SET revision=revision+1 WHERE id=?",
                (scope_id,),
            )
            record_audit(
                c,
                "context.clear_scope",
                source=actor.source,
                actor_id=actor.principal_id,
                target_type="scope",
                target_id=scope_id,
                details={"count": count},
            )
            return count

    def summary_input(self, chat_id):
        with self.storage._connection() as c:
            chat = c.execute(
                "SELECT context_mode FROM chats WHERE id=?", (chat_id,)
            ).fetchone()
            if not chat or chat[0] != "hybrid":
                return None
            old = c.execute(
                "SELECT * FROM group_summaries WHERE chat_id=?", (chat_id,)
            ).fetchone()
            last = old["last_message_rowid"] if old else 0
            new = c.execute(
                "SELECT COUNT(*) FROM messages WHERE chat_id=? AND rowid>? AND role='user' AND status='received'",
                (chat_id, last),
            ).fetchone()[0]
            if new < 10:
                return None
            rows = c.execute(
                "SELECT rowid,content FROM messages WHERE chat_id=? AND role='user' AND status='received' ORDER BY rowid DESC LIMIT 20",
                (chat_id,),
            ).fetchall()
            return {
                "last": rows[0]["rowid"],
                "text": "\n".join(r["content"] for r in reversed(rows))[:20000],
            }

    def save_summary(self, chat_id, text, last):
        with self.storage.transaction() as c:
            c.execute(
                "INSERT INTO group_summaries(chat_id,content,last_message_rowid) VALUES(?,?,?) ON CONFLICT(chat_id) DO UPDATE SET content=excluded.content,last_message_rowid=excluded.last_message_rowid,updated_at=CURRENT_TIMESTAMP",
                (chat_id, text[:600], last),
            )

    def summary(self, chat_id):
        with self.storage._connection() as c:
            row = c.execute(
                "SELECT s.content FROM group_summaries s JOIN chats c ON c.id=s.chat_id WHERE c.id=? AND c.context_mode='hybrid'",
                (chat_id,),
            ).fetchone()
            return row[0] if row else ""
