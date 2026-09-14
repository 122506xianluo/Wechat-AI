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

    def observe(self, chat_id, name, method="uia_avatar", confidence=1.0, features=(), *, auto_enable=False):
        normalized = normalize_name(name)
        with self.storage.transaction() as c:
            chat = c.execute("SELECT enabled,approval FROM chats WHERE id=? AND kind='group' AND merged_into IS NULL", (chat_id,)).fetchone()
            auto_enable = bool(auto_enable and chat and chat['enabled'] and chat['approval'] == 'approved')
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
                            c, "group_member", chat_id, name, status="active" if auto_enable else "pending"
                        )
                    )
                    c.execute(
                        "INSERT OR IGNORE INTO principal_aliases(chat_id,principal_id,name,normalized_name) VALUES(?,?,?,?)",
                        (chat_id, principal, name, normalized),
                    )
                    # Only a previously unreviewed nickname may become ordinary user.
                    # Explicit bans, disabled/renamed/ambiguous identities never revive.
                    if auto_enable:
                        activated = c.execute("""UPDATE principals SET status='active',updated_at=?
                            WHERE id=? AND status='pending' AND NOT EXISTS
                            (SELECT 1 FROM access_grants WHERE principal_id=? AND access_level='blocked')""",
                            (utc_now(), principal, principal)).rowcount
                        if activated:
                            record_audit(c, 'member.auto_enabled', source='wechat',
                                         target_type='principal', target_id=principal)
                            bump_permission_revision(c)
                    c.execute('UPDATE principals SET last_seen_at=? WHERE id=?', (utc_now(), principal))
                    reason = c.execute(
                        "SELECT status FROM principals WHERE id=?", (principal,)
                    ).fetchone()[0]
            feature_payload = {
                "shape_hash": sha256(repr(features).encode()).hexdigest(),
            }
            if isinstance(features, dict):
                # Probe diagnostics contain only control types, dimensions and
                # counters. Message text and nicknames are never included.
                feature_payload["probe"] = features
            c.execute(
                "INSERT INTO sender_observations(chat_id,principal_id,method,confidence,reason,features) VALUES(?,?,?,?,?,?)",
                (
                    chat_id,
                    principal,
                    method[:40],
                    confidence,
                    reason,
                    json.dumps(feature_payload, ensure_ascii=True),
                ),
            )
        # A deliberately disabled user is normal policy, not a sender parser warning.
        if reason not in ("active", "disabled"):
            key = (chat_id, reason)
            count, last = self.counts.get(key, (0, 0))
            count += 1
            if time.monotonic() - last >= 60:
                logging.getLogger("minimal_wechat_ai").warning(
                    "sender_skipped chat_id=%s reason=%s method=%s count=%s",
                    chat_id,
                    reason,
                    method[:40],
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
            from identity_merge import move_history

            move_history(c, a["chat_id"], b["chat_id"], source, target)
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


def extract_sender(row, known_names=(), *, element_from_point=None, scale=1.0,
                   diagnostics=None):
    """Read the nickname shown above an incoming group message bubble."""

    probe = diagnostics if isinstance(diagnostics, dict) else {}

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

    try:
        row_box = row.rectangle()
        probe.update(
            strategy="visible_nickname_band",
            row_control_type=kind(row) or "unknown",
            row_width=max(0, row_box.right - row_box.left),
            row_height=max(0, row_box.bottom - row_box.top),
            descendant_count=len(controls),
        )
    except Exception:
        probe.update(strategy="visible_nickname_band", row_geometry="unavailable")
    descendant_types = Counter(kind(c) or "unknown" for c in controls)
    probe["descendant_types"] = dict(descendant_types.most_common(8))

    buttons = {text(c) for c in controls if kind(c) == "button" and text(c)}
    if len(buttons) == 1:
        probe["result"] = "single_named_button"
        return buttons.pop(), "uia_avatar", 1.0
    if len(buttons) > 1:
        probe["result"] = "multiple_named_buttons"
        return "", "avatar_conflict", 0.0
    # Inline nickname must be above a separate text bubble, never the sole Text.
    texts = [c for c in controls if kind(c) == "text" and text(c)]
    if len(texts) >= 2:
        try:
            ordered = sorted(texts, key=lambda x: x.rectangle().top)
            if ordered[0].rectangle().bottom <= ordered[1].rectangle().top:
                probe["result"] = "stacked_text_controls"
                return text(ordered[0]), "inline_nickname", 0.9
        except Exception:
            pass
    # WeChat 4.1.13.12 can paint the visible nickname without exposing it as a
    # Control View child. UIA hit-testing at the actual label position can still
    # return that Text element. This is read-only and never clicks the avatar.
    if element_from_point is not None:
        try:
            box = row.rectangle()
            width, height = box.right - box.left, box.bottom - box.top
            x_start = box.left + max(42, round(42 * max(scale, 1.0)))
            x_stop = box.left + min(max(120, width // 2), 360)
            # Scan the whole visible nickname band. Candidate text still has to
            # expose its own compact rectangle, so the message bubble is rejected.
            y_stop = box.top + min(max(22, round(34 * max(scale, 1.0))), max(1, height // 2))
            hits = {}
            row_text = text(row)
            hit_types = Counter()
            sampled = row_hits = blank_hits = named_hits = 0
            for y in range(box.top + 2, y_stop + 1, max(4, round(5 * max(scale, 1.0)))):
                for x in range(x_start, x_stop + 1, max(6, round(8 * max(scale, 1.0)))):
                    sampled += 1
                    ctrl = element_from_point(x, y)
                    value = text(ctrl)
                    control_kind = kind(ctrl) or "unknown"
                    hit_types[control_kind] += 1
                    try:
                        rect = ctrl.rectangle()
                    except Exception:
                        continue
                    if (rect.left <= box.left and rect.top <= box.top
                            and rect.right >= box.right and rect.bottom >= box.bottom):
                        row_hits += 1
                    if not value:
                        blank_hits += 1
                        continue
                    if value.strip() == row_text.strip():
                        row_hits += 1
                        continue
                    if control_kind and "text" not in control_kind:
                        continue
                    if (rect.left < box.left or rect.right > box.right
                            or rect.top < box.top or rect.bottom > box.bottom):
                        continue
                    if rect.bottom > y_stop + max(4, round(4 * max(scale, 1.0))):
                        continue
                    try:
                        normalized_value = normalize_name(value)
                    except ValueError:
                        continue
                    named_hits += 1
                    hits[normalized_value] = (value, rect.top, rect.left)
            probe.update(
                sampled_points=sampled,
                hit_types=dict(hit_types.most_common(8)),
                row_hits=row_hits,
                blank_hits=blank_hits,
                named_hits=named_hits,
                candidate_count=len(hits),
            )
            if len(hits) == 1:
                probe["result"] = "point_text"
                return next(iter(hits.values()))[0], "uia_nickname_hit_test", 0.95
            if hits:
                ordered = sorted(hits.values(), key=lambda item: (item[1], item[2]))
                if len(ordered) == 1 or ordered[0][1] < ordered[1][1]:
                    probe["result"] = "topmost_point_text"
                    return ordered[0][0], "uia_nickname_hit_test", 0.9
                probe["result"] = "point_text_conflict"
            elif row_hits and row_hits >= max(1, sampled - blank_hits):
                probe["result"] = "point_row_only"
            else:
                probe["result"] = "point_no_named_text"
        except Exception as exc:
            probe["result"] = "point_probe_error"
            probe["probe_error"] = type(exc).__name__
    full = text(row)
    known = {normalize_name(n): n for n in known_names}
    # Row/content difference only accepted if it is an already registered exact alias.
    for ctrl in texts:
        body = text(ctrl)
        prefix = full[: -len(body)].strip() if body and full.endswith(body) else ""
        if normalize_name(prefix) in known:
            probe["result"] = "registered_alias_difference"
            return known[normalize_name(prefix)], "exact_alias_difference", 0.85
    result = probe.get("result", "no_structural_sender")
    return "", "uia_" + result[:36], 0.0


def strip_sender_prefix(text, sender):
    """Remove the verified UIA nickname prefix before trigger/LLM handling."""
    if not sender or not isinstance(text, str):
        return text
    if text.startswith(sender) and text[len(sender):len(sender) + 1].isspace():
        return text[len(sender):].lstrip()
    return text
