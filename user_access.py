"""Local user list: allow/disable, role, and optional remaining AI requests."""
from audit import bump_permission_revision, record_audit
from permissions import _decision
from roles import Roles, require_manager
from storage import utc_now


class UserAccess:
    def __init__(self, storage):
        self.storage = storage

    def list(self, query="", kind="", offset=0, limit=50):
        if len(query) > 256 or kind not in ("", "private_user", "group_member"):
            raise ValueError("用户筛选条件无效")
        if offset < 0 or not 1 <= limit <= 200:
            raise ValueError("分页参数无效")
        where = """p.kind IN ('private_user','group_member') AND p.status!='merged'
            AND c.merged_into IS NULL AND (?='' OR p.kind=?)
            AND (instr(p.display_name,?)>0 OR instr(c.name,?)>0)"""
        args = (kind, kind, query, query)
        with self.storage._connection() as c:
            total = c.execute("SELECT COUNT(*) FROM principals p JOIN chats c ON c.id=p.chat_id WHERE " + where, args).fetchone()[0]
            rows = c.execute("""SELECT p.*,c.name chat_name,c.enabled chat_enabled,c.approval,
                (SELECT role_id FROM role_bindings b WHERE b.chat_id=p.chat_id AND b.principal_id=p.id) role_id
                FROM principals p JOIN chats c ON c.id=p.chat_id WHERE """ + where +
                " ORDER BY p.last_seen_at DESC,p.id DESC LIMIT ? OFFSET ?", (*args, limit, offset)).fetchall()
            items = []
            for row in rows:
                item = dict(row)
                denied = c.execute("SELECT 1 FROM access_grants WHERE principal_id=? AND access_level='blocked'", (row['id'],)).fetchone()
                item['enabled'] = row['status'] == 'active' and not denied
                decision = _decision(c, row['id'], row['chat_id'])
                item.update(allowed=decision.allowed, reason=decision.reason)
                item['effective_role'] = Roles(self.storage).resolve(row['chat_id'], row['id'])['name']
                items.append(item)
        return dict(items=items, total=total, offset=offset, limit=limit)

    def update(self, principal_id, data, actor):
        if not data or set(data) - {'enabled', 'role_id', 'reply_quota'}:
            raise ValueError("只能修改允许使用、角色和剩余次数")
        with self.storage.transaction() as c:
            require_manager(c, actor)
            row = c.execute("SELECT * FROM principals WHERE id=? AND kind IN ('private_user','group_member')", (principal_id,)).fetchone()
            if not row or row['status'] == 'merged':
                raise ValueError("用户不存在或已经合并")
            if 'enabled' in data:
                enabled = data['enabled']
                if type(enabled) is not bool:
                    raise ValueError("允许使用必须是布尔值")
                if enabled and row['status'] in ('ambiguous', 'renamed'):
                    raise ValueError("昵称冲突或改名尚未确认，不能直接开启")
                # One switch replaces all legacy global/scoped user grants.
                c.execute("DELETE FROM access_grants WHERE principal_id=?", (principal_id,))
                c.execute("UPDATE principals SET status=? WHERE id=?", ('active' if enabled else 'disabled', principal_id))
            if 'reply_quota' in data:
                quota = data['reply_quota']
                if quota is not None and (type(quota) is not int or not 0 <= quota <= 1000000000):
                    raise ValueError("剩余次数须为 0~1000000000 的整数，留空表示不限")
                c.execute("UPDATE principals SET reply_quota=? WHERE id=?", (quota, principal_id))
            if 'role_id' in data:
                role_id = data['role_id']
                if role_id is not None:
                    if type(role_id) is not int or not c.execute("SELECT 1 FROM roles WHERE id=? AND enabled=1", (role_id,)).fetchone():
                        raise ValueError("角色不存在或未启用")
                c.execute("DELETE FROM role_bindings WHERE chat_id=? AND principal_id=?", (row['chat_id'], principal_id))
                if role_id is not None:
                    c.execute("INSERT INTO role_bindings(role_id,chat_id,principal_id) VALUES(?,?,?)", (role_id, row['chat_id'], principal_id))
            c.execute("UPDATE principals SET updated_at=? WHERE id=?", (utc_now(), principal_id))
            record_audit(c, 'user.access', source=actor.source, actor_id=actor.principal_id,
                         target_type='principal', target_id=principal_id, details=data)
            bump_permission_revision(c)

    def request_block_reason(self, principal_id, chat_id):
        with self.storage._connection() as c:
            decision = _decision(c, principal_id, chat_id)
            if not decision.allowed:
                return decision.reason
            row = c.execute(
                "SELECT reply_quota FROM principals WHERE id=?", (principal_id,)
            ).fetchone()
            if row is not None and row["reply_quota"] == 0:
                return "reply_quota_exhausted"
        return None

    @staticmethod
    def reserve_request(c, principal_id, chat_id):
        """Inside enqueue's transaction, after dedup and before attachment/LLM work.

        One new AI task costs one request (also on failure); retries of that same
        job are free. Clearing history never replenishes the persistent quota.
        """
        if not _decision(c, principal_id, chat_id).allowed:
            return False
        return bool(c.execute("""UPDATE principals SET request_count=request_count+1,
            reply_quota=CASE WHEN reply_quota IS NULL THEN NULL ELSE reply_quota-1 END
            WHERE id=? AND (reply_quota IS NULL OR reply_quota>0)""", (principal_id,)).rowcount)
