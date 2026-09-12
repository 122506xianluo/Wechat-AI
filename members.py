"""Exact group aliases and diagnostics; never infer a stable ID from a nickname."""

from collections import Counter
from hashlib import sha256
import json
import logging
import time
from permissions import Permissions, normalize_name
from roles import require_manager
from audit import record_audit, bump_permission_revision
from storage import utc_now


class Members:
    def __init__(self, storage):
        self.storage = storage
        self.counts = {}

    def observe(self, chat_id, name, method="uia_avatar", confidence=1.0, features=()):
        normalized = normalize_name(name)
        with self.storage.transaction() as c:
            principal = None
            reason = "sender_unknown"
            if normalized:
                aliases = c.execute(
                    "SELECT DISTINCT principal_id FROM principal_aliases WHERE chat_id=? AND normalized_name=? AND status='active'",
                    (chat_id, normalized),
                ).fetchall()
                if len(aliases) > 1:
                    reason = "duplicate_nickname"
                    c.executemany(
                        "UPDATE principals SET status='ambiguous' WHERE id=?",
                        [(r[0],) for r in aliases],
                    )
                else:
                    principal = (
                        aliases[0][0]
                        if aliases
                        else Permissions._observe(
                            c, "group_member", chat_id, name, status="pending"
                        )
                    )
                    c.execute(
                        "INSERT OR IGNORE INTO principal_aliases(chat_id,principal_id,name,normalized_name) VALUES(?,?,?,?)",
                        (chat_id, principal, name, normalized),
                    )
                    reason = c.execute(
                        "SELECT status FROM principals WHERE id=?", (principal,)
                    ).fetchone()[0]
            c.execute(
                "INSERT INTO sender_observations(chat_id,principal_id,method,confidence,reason,features) VALUES(?,?,?,?,?,?)",
                (
                    chat_id,
                    principal,
                    method[:40],
                    confidence,
                    reason,
                    json.dumps(
                        {"shape_hash": sha256(repr(features).encode()).hexdigest()}
                    ),
                ),
            )
        if reason != "active":
            key = (chat_id, reason)
            count, last = self.counts.get(key, (0, 0))
            count += 1
            if time.monotonic() - last >= 60:
                logging.getLogger("minimal_wechat_ai").warning(
                    "sender_skipped chat_id=%s reason=%s count=%s",
                    chat_id,
                    reason,
                    count,
                )
                count, last = 0, time.monotonic()
            self.counts[key] = (count, last)
        return principal

    def sync_roster(self, chat_id, names, actor):
        counts = Counter(normalize_name(n) for n in names if normalize_name(n))
        with self.storage.transaction() as c:
            require_manager(c, actor)
            row = c.execute(
                "SELECT id FROM chats WHERE id=? AND kind='group'", (chat_id,)
            ).fetchone()
            if not row:
                raise ValueError("群不存在")
            for name, count in counts.items():
                pid = Permissions._observe(
                    c, "group_member", chat_id, name, status="pending"
                )
                c.execute(
                    "INSERT OR IGNORE INTO principal_aliases(chat_id,principal_id,name,normalized_name) VALUES(?,?,?,?)",
                    (chat_id, pid, name, name),
                )
                if count > 1:
                    c.execute(
                        "UPDATE principals SET status='ambiguous' WHERE id=?", (pid,)
                    )
            # Removed/renamed roster members lose active authority until confirmed.
            for row in c.execute(
                "SELECT id,normalized_name FROM principals WHERE chat_id=? AND status='active'",
                (chat_id,),
            ).fetchall():
                if row["normalized_name"] not in counts:
                    c.execute(
                        "UPDATE principals SET status='renamed' WHERE id=?",
                        (row["id"],),
                    )
            c.execute(
                "INSERT INTO member_rosters VALUES(?,?,?) ON CONFLICT(chat_id) DO UPDATE SET synced_at=excluded.synced_at,member_count=excluded.member_count",
                (chat_id, utc_now(), sum(counts.values())),
            )
            record_audit(
                c,
                "members.sync",
                source=actor.source,
                actor_id=actor.principal_id,
                target_type="chat",
                target_id=chat_id,
                details={"count": sum(counts.values())},
            )
            bump_permission_revision(c)
        return {
            "count": sum(counts.values()),
            "conflicts": sum(n > 1 for n in counts.values()),
        }

    def merge(self, source, target, actor):
        with self.storage.transaction() as c:
            require_manager(c, actor)
            a = c.execute("SELECT * FROM principals WHERE id=?", (source,)).fetchone()
            b = c.execute("SELECT * FROM principals WHERE id=?", (target,)).fetchone()
            if (
                source == target
                or not a
                or not b
                or a["kind"] != "group_member"
                or b["kind"] != "group_member"
                or a["chat_id"] != b["chat_id"]
                or a["status"] == "ambiguous"
                or b["status"] == "ambiguous"
            ):
                raise ValueError("仅允许同群内无重名冲突的成员合并")
            c.execute(
                "INSERT OR IGNORE INTO principal_aliases(chat_id,principal_id,name,normalized_name) SELECT chat_id,?,name,normalized_name FROM principal_aliases WHERE principal_id=?",
                (target, source),
            )
            c.execute(
                "UPDATE principal_aliases SET status='retired' WHERE principal_id=?",
                (source,),
            )
            c.execute("UPDATE principals SET status='merged' WHERE id=?", (source,))
            c.execute("UPDATE principals SET status='active' WHERE id=?", (target,))
            record_audit(
                c,
                "member.merge",
                source=actor.source,
                actor_id=actor.principal_id,
                target_type="principal",
                target_id=target,
                details={"source": source},
            )
            bump_permission_revision(c)

    def diagnostics(self, chat_id=None):
        with self.storage._connection() as c:
            return [
                dict(r)
                for r in c.execute(
                    "SELECT * FROM sender_observations WHERE (? IS NULL OR chat_id=?) ORDER BY id DESC LIMIT 200",
                    (chat_id, chat_id),
                )
            ]


def extract_sender(row, known_names=()):
    """Only unique, structurally supported candidates; arbitrary message text is not identity."""

    def walk(node, depth=0):
        if depth > 5:
            return
        try:
            children = node.children()
        except Exception:
            return
        for child in children:
            yield child
            yield from walk(child, depth + 1)

    controls = list(walk(row))

    def kind(ctrl):
        return str(
            getattr(getattr(ctrl, "element_info", None), "control_type", "")
        ).lower()

    def text(ctrl):
        try:
            return ctrl.window_text().strip()
        except Exception:
            return ""

    buttons = {text(c) for c in controls if kind(c) == "button" and text(c)}
    if len(buttons) == 1:
        return buttons.pop(), "uia_avatar", 1.0
    if len(buttons) > 1:
        return "", "avatar_conflict", 0.0
    # Inline nickname must be above a separate text bubble, never the sole Text.
    texts = [c for c in controls if kind(c) == "text" and text(c)]
    if len(texts) >= 2:
        try:
            ordered = sorted(texts, key=lambda x: x.rectangle().top)
            if ordered[0].rectangle().bottom <= ordered[1].rectangle().top:
                return text(ordered[0]), "inline_nickname", 0.9
        except Exception:
            pass
    full = text(row)
    known = {normalize_name(n): n for n in known_names}
    # Row/content difference only accepted if it is an already registered exact alias.
    for ctrl in texts:
        body = text(ctrl)
        prefix = full[: -len(body)].strip() if body and full.endswith(body) else ""
        if normalize_name(prefix) in known:
            return known[normalize_name(prefix)], "exact_alias_difference", 0.85
    return "", "no_structural_sender", 0.0
