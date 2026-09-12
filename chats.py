"""One-time whitelist import, fail-closed discovery and manual identity merges."""

from hashlib import sha256
from audit import record_audit, bump_permission_revision
from permissions import Permissions
from roles import require_manager
from storage import utc_now


class Chats:
    def __init__(self, storage):
        self.storage = storage

    def import_legacy(self, private_chats, groups):
        with self.storage.transaction() as c:
            if c.execute(
                "SELECT 1 FROM schema_meta WHERE key='legacy_chats_imported'"
            ).fetchone():
                return
            now = utc_now()
            for kind, names in [("private", private_chats), ("group", groups)]:
                for name in names:
                    c.execute(
                        "INSERT INTO chats(kind,name,approval,created_at,updated_at) VALUES(?,?,'approved',?,?) ON CONFLICT(kind,name) DO NOTHING",
                        (kind, name, now, now),
                    )
                    chat = c.execute(
                        "SELECT id FROM chats WHERE kind=? AND name=?", (kind, name)
                    ).fetchone()[0]
                    if kind == "private":
                        Permissions._observe(
                            c, "private_user", chat, name, status="active"
                        )
            c.execute(
                "INSERT INTO schema_meta(key,value) VALUES('legacy_chats_imported','1')"
            )
            record_audit(c, "chats.import_legacy")

    def discover(self, kind, name, ui_key=""):
        if (
            kind not in ("private", "group")
            or not isinstance(name, str)
            or not name.strip()
            or len(name) > 256
        ):
            raise ValueError("无法识别的会话")
        with self.storage.transaction() as c:
            now = utc_now()
            c.execute(
                "INSERT INTO chats(kind,name,enabled,approval,visibility,created_at,updated_at) VALUES(?,?,0,'pending','visible',?,?) ON CONFLICT(kind,name) DO UPDATE SET visibility='visible'",
                (kind, name, now, now),
            )
            row = c.execute(
                "SELECT * FROM chats WHERE kind=? AND name=?", (kind, name)
            ).fetchone()
            c.execute(
                "INSERT INTO chat_discoveries(chat_id,ui_key_hash) VALUES(?,?) ON CONFLICT(chat_id,ui_key_hash) DO UPDATE SET last_seen_at=CURRENT_TIMESTAMP",
                (row["id"], sha256(ui_key.encode()).hexdigest()),
            )
            return dict(row)

    def get(self, kind, name):
        with self.storage._connection() as c:
            row = c.execute(
                "SELECT * FROM chats WHERE kind=? AND name=?", (kind, name)
            ).fetchone()
            return dict(row) if row else None

    def allowed(self, kind, name):
        row = self.get(kind, name)
        return bool(
            row
            and row["enabled"]
            and row["approval"] == "approved"
            and not row["merged_into"]
        )

    def list(self, query=""):
        with self.storage._connection() as c:
            return [
                dict(r)
                for r in c.execute(
                    "SELECT c.*, (SELECT COUNT(*) FROM messages m WHERE m.chat_id=c.id) message_count FROM chats c WHERE c.name LIKE ? ORDER BY c.id",
                    ("%" + query[:256] + "%",),
                )
            ]

    def set_visibility(self, seen):
        with self.storage.transaction() as c:
            c.execute(
                "UPDATE chats SET visibility='not_visible' WHERE merged_into IS NULL"
            )
            c.executemany(
                "UPDATE chats SET visibility='visible' WHERE kind=? AND name=?", seen
            )

    def update(self, chat_id, data, actor):
        if set(data) - {"approval", "enabled", "management_note"}:
            raise ValueError("会话字段无效")
        with self.storage.transaction() as c:
            require_manager(c, actor)
            row = c.execute("SELECT * FROM chats WHERE id=?", (chat_id,)).fetchone()
            if not row or row["merged_into"]:
                raise ValueError("会话不存在或已合并")
            approval = data.get("approval", row["approval"])
            if approval not in ("pending", "approved", "rejected"):
                raise ValueError("审批状态无效")
            enabled = data.get(
                "enabled",
                approval == "approved" if "approval" in data else bool(row["enabled"]),
            )
            if type(enabled) is not bool:
                raise ValueError("enabled 必须为布尔值")
            note = data.get("management_note", row["management_note"])
            if not isinstance(note, str) or len(note) > 4000:
                raise ValueError("备注过长")
            c.execute(
                "UPDATE chats SET approval=?,enabled=?,management_note=?,baseline_revision=baseline_revision+1,updated_at=? WHERE id=?",
                (approval, enabled, note, utc_now(), chat_id),
            )
            if row["kind"] == "private" and approval == "approved":
                Permissions._observe(
                    c, "private_user", chat_id, row["name"], status="active"
                )
            record_audit(
                c,
                "chat.update",
                source=actor.source,
                actor_id=actor.principal_id,
                target_type="chat",
                target_id=chat_id,
                details={"approval": approval, "enabled": enabled},
            )
            bump_permission_revision(c)

    def merge(self, source, target, actor):
        if source == target:
            raise ValueError("不能合并到自身")
        with self.storage.transaction() as c:
            require_manager(c, actor)
            a = c.execute("SELECT * FROM chats WHERE id=?", (source,)).fetchone()
            b = c.execute("SELECT * FROM chats WHERE id=?", (target,)).fetchone()
            if (
                not a
                or not b
                or a["kind"] != "private"
                or b["kind"] != "private"
                or a["merged_into"]
                or b["merged_into"]
            ):
                raise ValueError("只能合并两个未合并的私聊")
            from identity_merge import move_history

            source_pid = Permissions._observe(
                c, "private_user", source, a["name"], status="active"
            )
            target_pid = Permissions._observe(
                c, "private_user", target, b["name"], status="active"
            )
            move_history(c, source, target, source_pid, target_pid)
            # Permissions are intentionally NOT inherited from the retired name.
            c.execute("UPDATE messages SET chat_id=? WHERE chat_id=?", (target, source))
            c.execute(
                "INSERT OR IGNORE INTO chat_aliases(chat_id,kind,name) VALUES(?,?,?)",
                (target, a["kind"], a["name"]),
            )
            c.execute(
                "UPDATE principals SET status='merged' WHERE chat_id=?", (source,)
            )
            c.execute(
                "UPDATE chats SET enabled=0,merged_into=?,baseline_revision=baseline_revision+1 WHERE id=?",
                (target, source),
            )
            c.execute(
                "UPDATE chats SET baseline_revision=baseline_revision+1 WHERE id=?",
                (target,),
            )
            record_audit(
                c,
                "chat.merge",
                source=actor.source,
                actor_id=actor.principal_id,
                target_type="chat",
                target_id=target,
                details={"source": source},
            )
            bump_permission_revision(c)
