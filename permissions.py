"""Identity and access policy, independent of WeChat UI and model clients.

Stage 3 identities are scoped to a verified UI chat. A group nickname is NOT a
stable WeChat ID: newly observed names require explicit local approval. Rich
alias/collision resolution will be added in stage 6; never infer privileges from
message text or automatically transfer permissions to a renamed member.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import re

from audit import bump_permission_revision, record_audit
from storage import Storage, utc_now

LEVELS = frozenset({"owner", "admin", "user", "blocked"})
STATUSES = frozenset({"active", "pending", "ambiguous", "renamed", "merged", "disabled"})


class PermissionDenied(ValueError):
    pass


@dataclass(frozen=True)
class Actor:
    principal_id: int | None
    access_level: str
    source: str
    chat_id: int | None = None


# Never constructed from request JSON. Stage 8 replaces this loopback-only actor
# with the authenticated web account; no WeChat principal can claim it.
LOCAL_OWNER = Actor(None, "owner", "web")


@dataclass(frozen=True)
class AccessDecision:
    principal_id: int | None
    chat_id: int | None
    access_level: str
    allowed: bool
    reason: str

    def as_actor(self) -> Actor:
        return Actor(self.principal_id, self.access_level, "wechat", self.chat_id)


def normalize_name(name: str) -> str:
    if not isinstance(name, str) or len(name) > 256:
        raise ValueError("昵称必须是最多 256 字符的字符串")
    # Preserve meaningful symbols/case; no fuzzy/NFKC/transliteration matching.
    return re.sub(r"[\u2005\u200b\ufeff]+", "", name).strip()


def _principal(connection, principal_id: int):
    row = connection.execute("SELECT * FROM principals WHERE id=?", (principal_id,)).fetchone()
    if row is None:
        raise ValueError("身份不存在")
    return row


def _decision(connection, principal_id: int, chat_id: int | None) -> AccessDecision:
    principal = _principal(connection, principal_id)
    if principal["status"] != "active":
        return AccessDecision(principal_id, chat_id, "blocked", False, principal["status"])
    if principal["kind"] in ("private_user", "group_member") and principal["chat_id"] != chat_id:
        return AccessDecision(principal_id, chat_id, "blocked", False, "wrong_chat")
    if chat_id is not None:
        chat = connection.execute("SELECT enabled,approval FROM chats WHERE id=?", (chat_id,)).fetchone()
        if chat is None or not chat[0] or chat["approval"] != "approved":
            return AccessDecision(principal_id, chat_id, "blocked", False, "chat_disabled")
    grants = connection.execute(
        "SELECT chat_id,access_level FROM access_grants "
        "WHERE principal_id=? AND (chat_id IS NULL OR chat_id=?)", (principal_id, chat_id)).fetchall()
    # Explicit deny wins over any global or scoped privilege.
    if any(row["access_level"] == "blocked" for row in grants):
        return AccessDecision(principal_id, chat_id, "blocked", False, "blocked")
    scoped = next((row["access_level"] for row in grants if row["chat_id"] is not None), None)
    global_level = next((row["access_level"] for row in grants if row["chat_id"] is None), "user")
    level = scoped or global_level
    if level == "owner" and principal["kind"] != "web_account":
        return AccessDecision(principal_id, chat_id, "blocked", False, "invalid_owner")
    return AccessDecision(principal_id, chat_id, level, True, "allowed")


class Permissions:
    def __init__(self, storage: Storage):
        self.storage = storage

    @staticmethod
    def _observe(connection, kind: str, chat_id: int, name: str, *, status: str) -> int:
        normalized = normalize_name(name)
        if not normalized:
            raise ValueError("昵称为空，不能登记身份")
        now = utc_now()
        # Private identity follows its registered chat, not changing row labels.
        if kind == "private_user":
            row = connection.execute(
                "SELECT id FROM principals WHERE kind='private_user' AND chat_id=?", (chat_id,)).fetchone()
        else:
            row = connection.execute(
                "SELECT id FROM principals WHERE kind=? AND chat_id=? AND normalized_name=?",
                (kind, chat_id, normalized)).fetchone()
        if row:
            connection.execute("UPDATE principals SET last_seen_at=? WHERE id=?", (now, row[0]))
            return int(row[0])
        cursor = connection.execute(
            """INSERT INTO principals
            (kind,chat_id,display_name,normalized_name,status,created_at,updated_at,first_seen_at,last_seen_at)
            VALUES (?,?,?,?,?,?,?,?,?)""",
            (kind, chat_id, name.strip(), normalized, status, now, now, now, now))
        principal_id = int(cursor.lastrowid)
        record_audit(connection, "identity.discovered", target_type="principal", target_id=principal_id,
                     details={"kind": kind, "chat_id": chat_id, "status": status})
        bump_permission_revision(connection)
        return principal_id

    def register_private_targets(self, names: list[str]) -> None:
        # Config whitelist is still authoritative until stage 5. Only explicit
        # existing targets are active; new group names are always pending.
        for name in names:
            chat_id = self.storage.chat_id("private", name)
            with self.storage.transaction() as connection:
                self._observe(connection, "private_user", chat_id, name, status="active")

    def observe_group_sender(self, chat_id: int, name: str) -> int:
        with self.storage.transaction() as connection:
            row = connection.execute("SELECT kind FROM chats WHERE id=?", (chat_id,)).fetchone()
            if row is None or row[0] != "group":
                raise ValueError("必须指定已有群聊")
            return self._observe(connection, "group_member", chat_id, name, status="pending")

    def resolve_incoming(self, kind: str, name: str, sender_name: str,
                         direction: str) -> AccessDecision:
        if direction != "incoming":
            return AccessDecision(None, None, "blocked", False, "not_incoming")
        # Do not create chats here: unknown targets are not authorized by identity.
        with self.storage._connection() as connection:
            chat = connection.execute(
                "SELECT id,enabled,approval FROM chats WHERE kind=? AND name=?", (kind, name)).fetchone()
        if chat is None or not chat["enabled"] or chat["approval"] != "approved":
            return AccessDecision(None, None, "blocked", False, "chat_unregistered_or_disabled")
        chat_id = int(chat["id"])
        if kind == "group":
            try:
                normalized_sender = normalize_name(sender_name)
            except ValueError:
                return AccessDecision(None, chat_id, "blocked", False, "sender_invalid")
            if not normalized_sender:
                return AccessDecision(None, chat_id, "blocked", False, "sender_unknown")
            principal_id = self.observe_group_sender(chat_id, sender_name)
        else:
            with self.storage._connection() as connection:
                row = connection.execute(
                    "SELECT id FROM principals WHERE kind='private_user' AND chat_id=?", (chat_id,)).fetchone()
            if row is None:
                return AccessDecision(None, chat_id, "blocked", False, "identity_pending")
            principal_id = int(row[0])
        return self.resolve(principal_id, chat_id)

    def resolve(self, principal_id: int, chat_id: int | None) -> AccessDecision:
        with self.storage._connection() as connection:
            return _decision(connection, principal_id, chat_id)

    @staticmethod
    def _authorize_management(connection, actor: Actor, target, chat_id: int | None,
                              *, level: str | None = None) -> None:
        if actor.access_level == "owner":
            if actor.source != "web":
                raise PermissionDenied("owner 仅允许 Web 后台身份")
            if actor.principal_id is not None:
                current = _decision(connection, actor.principal_id, None)
                if not current.allowed or current.access_level != "owner":
                    raise PermissionDenied("owner 权限已失效")
            return
        if actor.principal_id is None:
            raise PermissionDenied("缺少已验证的管理身份")
        current = _decision(connection, actor.principal_id, actor.chat_id)
        if not current.allowed or current.access_level != "admin":
            raise PermissionDenied("需要管理员权限")
        if actor.chat_id is not None and chat_id != actor.chat_id:
            raise PermissionDenied("管理员不能管理其他聊天或全局权限")
        privileged = connection.execute(
            "SELECT 1 FROM access_grants WHERE principal_id=? AND access_level IN ('owner','admin')",
            (target["id"],)).fetchone()
        if privileged or level in ("admin", "owner") or target["id"] == actor.principal_id:
            raise PermissionDenied("只有 owner 可以管理管理员或 owner")

    def set_grant(self, principal_id: int, level: str, chat_id: int | None,
                  *, actor: Actor) -> None:
        if level not in LEVELS:
            raise ValueError("权限等级无效")
        with self.storage.transaction() as connection:
            target = _principal(connection, principal_id)
            if level == "owner" and (target["kind"] != "web_account" or chat_id is not None):
                raise PermissionDenied("微信身份不能成为 owner")
            if chat_id is not None:
                if connection.execute("SELECT 1 FROM chats WHERE id=?", (chat_id,)).fetchone() is None:
                    raise ValueError("聊天不存在")
                if target["chat_id"] is not None and target["chat_id"] != chat_id:
                    raise ValueError("权限范围与身份所在聊天不一致")
            self._authorize_management(connection, actor, target, chat_id, level=level)
            existing = connection.execute(
                "SELECT id,access_level FROM access_grants WHERE principal_id=? AND chat_id IS ?",
                (principal_id, chat_id)).fetchone()
            now = utc_now()
            if existing:
                connection.execute("UPDATE access_grants SET access_level=?,updated_at=? WHERE id=?",
                                   (level, now, existing["id"]))
            else:
                connection.execute(
                    "INSERT INTO access_grants(principal_id,chat_id,access_level,created_at,updated_at) "
                    "VALUES(?,?,?,?,?)", (principal_id, chat_id, level, now, now))
            record_audit(connection, "permission.set", source=actor.source, actor_id=actor.principal_id,
                         target_type="principal", target_id=principal_id,
                         details={"chat_id": chat_id, "old": existing["access_level"] if existing else None,
                                  "new": level})
            bump_permission_revision(connection)

    def remove_grant(self, principal_id: int, chat_id: int | None, *, actor: Actor) -> None:
        with self.storage.transaction() as connection:
            target = _principal(connection, principal_id)
            self._authorize_management(connection, actor, target, chat_id)
            existing = connection.execute(
                "SELECT id,access_level FROM access_grants WHERE principal_id=? AND chat_id IS ?",
                (principal_id, chat_id)).fetchone()
            if existing is None:
                raise ValueError("没有该范围的权限记录")
            if existing["access_level"] == "owner":
                raise PermissionDenied("第 3 步不能移除 Web owner")
            connection.execute("DELETE FROM access_grants WHERE id=?", (existing["id"],))
            record_audit(connection, "permission.revoke", source=actor.source, actor_id=actor.principal_id,
                         target_type="principal", target_id=principal_id,
                         details={"chat_id": chat_id, "old": existing["access_level"]})
            bump_permission_revision(connection)

    def set_status(self, principal_id: int, status: str, *, actor: Actor) -> None:
        if status not in ("active", "disabled"):
            raise ValueError("只允许人工批准或停用；冲突身份须先完成消歧")
        with self.storage.transaction() as connection:
            target = _principal(connection, principal_id)
            self._authorize_management(connection, actor, target, target["chat_id"])
            if status == "active" and target["status"] in ("ambiguous", "renamed", "merged"):
                raise PermissionDenied("身份冲突不能直接批准，请先确认或修正群昵称")
            if target["kind"] not in ("private_user", "group_member"):
                raise PermissionDenied("此接口不能修改系统或 Web 身份")
            connection.execute("UPDATE principals SET status=?,updated_at=? WHERE id=?",
                               (status, utc_now(), principal_id))
            record_audit(connection, "identity.status", source=actor.source, actor_id=actor.principal_id,
                         target_type="principal", target_id=principal_id,
                         details={"old": target["status"], "new": status})
            bump_permission_revision(connection)

    def audit_command(self, decision: AccessDecision, command: str, outcome: str) -> None:
        with self.storage.transaction() as connection:
            record_audit(connection, "command." + command, source="wechat",
                         actor_id=decision.principal_id, target_type="chat", target_id=decision.chat_id,
                         details={"outcome": outcome})

    def reset_private_context(self, decision: AccessDecision) -> int:
        # Recheck access and delete within ONE transaction; group-wide deletion is
        # deliberately impossible through WeChat (member scopes arrive in step 7).
        with self.storage.transaction() as connection:
            current = _decision(connection, decision.principal_id, decision.chat_id)
            target = _principal(connection, decision.principal_id)
            if not current.allowed or target["kind"] != "private_user":
                raise PermissionDenied("只能清空自己已批准的私聊上下文")
            deleted = connection.execute(
                "DELETE FROM messages WHERE chat_id=?", (decision.chat_id,)).rowcount
            record_audit(connection, "context.clear", source="wechat", actor_id=decision.principal_id,
                         target_type="chat", target_id=decision.chat_id, details={"deleted": deleted})
            record_audit(connection, "command.reset", source="wechat", actor_id=decision.principal_id,
                         target_type="chat", target_id=decision.chat_id, details={"outcome": "executed"})
            return deleted

    def list_principals(self, query: str = "", limit: int = 200) -> list[dict]:
        with self.storage._connection() as connection:
            rows = connection.execute(
                """SELECT p.*,c.name AS chat_name,c.kind AS chat_kind FROM principals p
                   LEFT JOIN chats c ON c.id=p.chat_id
                   WHERE instr(p.display_name,?)>0 OR instr(COALESCE(c.name,''),?)>0
                   ORDER BY CASE p.status WHEN 'pending' THEN 0 ELSE 1 END,p.id DESC LIMIT ?""",
                (query, query, min(max(int(limit), 1), 200))).fetchall()
            result = []
            for row in rows:
                item = dict(row)
                item["grants"] = [dict(g) for g in connection.execute(
                    "SELECT chat_id,access_level FROM access_grants WHERE principal_id=? ORDER BY id", (row["id"],))]
                decision = _decision(connection, row["id"], row["chat_id"])
                item.update(access_level=decision.access_level, allowed=decision.allowed, reason=decision.reason)
                result.append(item)
            return result

    def list_audit(self, before_id: int | None = None, limit: int = 100) -> list[dict]:
        with self.storage._connection() as connection:
            rows = connection.execute(
                "SELECT * FROM audit_events WHERE (? IS NULL OR id<?) ORDER BY id DESC LIMIT ?",
                (before_id, before_id, min(max(int(limit), 1), 200))).fetchall()
        result = [dict(row) for row in rows]
        for row in result:
            row["details"] = json.loads(row["details"])
        return result

    def revision(self) -> int:
        with self.storage._connection() as connection:
            return int(connection.execute(
                "SELECT value FROM schema_meta WHERE key='permission_revision'").fetchone()[0])
