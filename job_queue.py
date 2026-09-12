"""Durable ordered jobs; all transitions atomic, no network inside transactions."""

import json
import time
import uuid
from audit import record_audit
from roles import require_manager
from storage import utc_now

ACTIVE = ("queued", "processing", "retry_wait", "ready_to_send", "sending", "unknown")


class JobQueue:
    def __init__(self, storage):
        self.storage = storage
        self.owner = uuid.uuid4().hex

    def enqueue(
        self,
        chat_id,
        principal_id,
        scope_id,
        payload,
        question,
        source_key,
        *,
        answer=None,
        job_type="reply",
    ):
        now = time.time()
        jid = uuid.uuid4().hex
        mid = uuid.uuid4().hex
        with self.storage.transaction() as c:
            old = c.execute(
                "SELECT job_id FROM inbound_dedup WHERE chat_id=? AND source_key=?",
                (chat_id, source_key),
            ).fetchone()
            if old:
                return None
            c.execute(
                "INSERT INTO inbound_dedup VALUES(?,?,?,?)",
                (chat_id, source_key, jid, now),
            )
            if job_type == "reply":
                c.execute(
                    "INSERT INTO messages(id,chat_id,role,direction,content,status,source_key,created_at,updated_at,scope_id,sender_principal_id,turn_id) VALUES(?,?,'user','incoming',?,'pending',?,?,?,?,?,?)",
                    (
                        mid,
                        chat_id,
                        question,
                        source_key,
                        utc_now(),
                        utc_now(),
                        scope_id,
                        principal_id,
                        mid,
                    ),
                )
            else:
                mid = None
            state = "ready_to_send" if answer is not None else "queued"
            c.execute(
                "INSERT INTO message_jobs(id,inbound_message_id,scope_id,chat_id,principal_id,job_type,state,payload,generated_reply,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    jid,
                    mid,
                    scope_id,
                    chat_id,
                    principal_id,
                    job_type,
                    state,
                    json.dumps(payload, ensure_ascii=False),
                    answer,
                    now,
                    now,
                ),
            )
            c.execute(
                "UPDATE chats SET last_message_at=? WHERE id=?", (utc_now(), chat_id)
            )
        return jid

    def seen(self, chat_id, source_key):
        with self.storage._connection() as c:
            return c.execute("SELECT 1 FROM inbound_dedup WHERE chat_id=? AND source_key=?", (chat_id,source_key)).fetchone() is not None

    def get(self, jid):
        with self.storage._connection() as c:
            r = c.execute("SELECT * FROM message_jobs WHERE id=?", (jid,)).fetchone()
            return dict(r) if r else None

    def list(self):
        with self.storage._connection() as c:
            return [
                dict(r)
                for r in c.execute(
                    "SELECT id,chat_id,principal_id,scope_id,job_type,state,attempts,entered_sending,last_error,created_at,updated_at FROM message_jobs ORDER BY created_at DESC LIMIT 300"
                )
            ]

    def recover(self):
        now = time.time()
        with self.storage.transaction() as c:
            c.execute(
                "UPDATE message_jobs SET state='unknown',last_error='interrupted_during_send',updated_at=? WHERE state='sending'",
                (now,),
            )
            c.execute(
                "UPDATE messages SET status='unknown',error_message='interrupted_during_send' WHERE id IN (SELECT inbound_message_id FROM message_jobs WHERE state='unknown')"
            )
            c.execute(
                "UPDATE message_jobs SET state='needs_review',last_error='older_than_30_minutes',updated_at=? WHERE state IN ('queued','retry_wait','processing','ready_to_send') AND created_at<?",
                (now, now - 1800),
            )
            c.execute(
                "UPDATE message_jobs SET state=CASE WHEN attempts>=3 THEN 'needs_review' ELSE 'queued' END,lease_owner=NULL,lease_until=NULL,updated_at=? WHERE state='processing'",
                (now,),
            )
            c.execute("DELETE FROM job_leases")
            # Orphaned P1 work is never automatically replayed.
            c.execute(
                "UPDATE messages SET status='failed',error_message='legacy_interrupted' WHERE status='pending' AND id NOT IN (SELECT inbound_message_id FROM message_jobs WHERE inbound_message_id IS NOT NULL)"
            )

    def claim(self, jid=None):
        now = time.time()
        with self.storage.transaction() as c:
            c.execute(
                "UPDATE message_jobs SET state=CASE WHEN attempts>=3 THEN 'needs_review' ELSE 'queued' END,lease_owner=NULL,lease_until=NULL WHERE state='processing' AND lease_until<?",
                (now,),
            )
            row = c.execute(
                """SELECT j.* FROM message_jobs j LEFT JOIN queue_fairness f ON f.chat_id=j.chat_id
              WHERE j.state IN ('queued','retry_wait') AND j.next_attempt_at<=? AND (? IS NULL OR j.id=?)
              AND NOT EXISTS(SELECT 1 FROM message_jobs earlier WHERE earlier.scope_id=j.scope_id
                AND earlier.state IN ('queued','retry_wait','processing','ready_to_send','sending','unknown')
                AND (earlier.created_at<j.created_at OR (earlier.created_at=j.created_at AND earlier.rowid<j.rowid)))
              ORDER BY COALESCE(f.last_claim,0),j.created_at,j.rowid LIMIT 1""",
                (now, jid, jid),
            ).fetchone()
            if not row:
                return None
            c.execute(
                "UPDATE message_jobs SET state='processing',attempts=attempts+1,lease_owner=?,lease_until=?,updated_at=? WHERE id=?",
                (self.owner, now + 900, now, row["id"]),
            )
            c.execute(
                "INSERT OR REPLACE INTO job_leases VALUES(?,?,?)",
                (row["id"], self.owner, now + 900),
            )
            c.execute(
                "INSERT OR REPLACE INTO queue_fairness VALUES(?,?)",
                (row["chat_id"], now),
            )
            c.execute(
                "INSERT INTO job_attempts(job_id,attempt,status,created_at) VALUES(?,?,?,?)",
                (row["id"], row["attempts"] + 1, "started", now),
            )
            result = dict(row)
            result.update(
                state="processing", attempts=row["attempts"] + 1, lease_owner=self.owner
            )
            return result

    def generated(self, jid, answer):
        with self.storage.transaction() as c:
            count = c.execute(
                "UPDATE message_jobs SET generated_reply=?,state='ready_to_send',lease_owner=NULL,lease_until=NULL,updated_at=? WHERE id=? AND state='processing' AND lease_owner=?",
                (answer, time.time(), jid, self.owner),
            ).rowcount
            c.execute("DELETE FROM job_leases WHERE job_id=?", (jid,))
            return bool(count)

    def failure(self, jid, exc):
        cause = exc
        while cause.__cause__ is not None:
            cause = cause.__cause__
        status = getattr(getattr(cause, "response", None), "status_code", None)
        permanent = isinstance(cause, (ValueError, PermissionError)) or (
            status is not None and 400 <= status < 500 and status not in (408, 429)
        )
        with self.storage.transaction() as c:
            row = c.execute("SELECT * FROM message_jobs WHERE id=?", (jid,)).fetchone()
            if (
                not row
                or row["state"] != "processing"
                or row["lease_owner"] != self.owner
            ):
                return
            state = (
                "needs_review" if permanent or row["attempts"] >= 3 else "retry_wait"
            )
            delay = 5 if row["attempts"] == 1 else 30
            if status == 429:
                try:
                    delay = max(
                        delay,
                        min(
                            300, float(cause.response.headers.get("Retry-After", delay))
                        ),
                    )
                except (ValueError, TypeError):
                    pass
            error = type(cause).__name__ + (" HTTP " + str(status) if status else "")
            c.execute(
                "UPDATE message_jobs SET state=?,next_attempt_at=?,lease_owner=NULL,lease_until=NULL,last_error=?,updated_at=? WHERE id=?",
                (state, time.time() + delay, error, time.time(), jid),
            )
            c.execute(
                "INSERT INTO job_attempts(job_id,attempt,status,error,created_at) VALUES(?,?,?,?,?)",
                (jid, row["attempts"], state, error, time.time()),
            )
            c.execute("DELETE FROM job_leases WHERE job_id=?", (jid,))
            if state == "needs_review":
                c.execute(
                    "UPDATE messages SET status='failed',error_message=? WHERE id=?",
                    (error, row["inbound_message_id"]),
                )

    def ready(self):
        with self.storage._connection() as c:
            return [
                dict(r)
                for r in c.execute("""SELECT j.* FROM message_jobs j WHERE j.state='ready_to_send' AND NOT EXISTS(
              SELECT 1 FROM message_jobs e WHERE e.scope_id=j.scope_id AND e.state IN ('queued','retry_wait','processing','ready_to_send','sending','unknown')
              AND (e.created_at<j.created_at OR (e.created_at=j.created_at AND e.rowid<j.rowid))) ORDER BY j.updated_at LIMIT 20""")
            ]

    def sending(self, jid):
        with self.storage.transaction() as c:
            return bool(
                c.execute(
                    "UPDATE message_jobs SET state='sending',entered_sending=1,updated_at=? WHERE id=? AND state='ready_to_send'",
                    (time.time(), jid),
                ).rowcount
            )

    def finish(self, jid, state, error=None):
        if state not in ("sent", "unknown", "cancelled", "failed", "needs_review"):
            raise ValueError("无效结束状态")
        with self.storage.transaction() as c:
            row = c.execute("SELECT * FROM message_jobs WHERE id=?", (jid,)).fetchone()
            if not row:
                raise ValueError("任务不存在")
            if row["state"] == "sent":
                return
            if state == "sent" and row["state"] not in (
                "sending",
                "unknown",
                "ready_to_send",
            ):
                raise ValueError("任务不能确认发送")
            c.execute(
                "UPDATE message_jobs SET state=?,last_error=?,updated_at=? WHERE id=?",
                (state, error, time.time(), jid),
            )
            if row["inbound_message_id"]:
                mid = row["inbound_message_id"]
                c.execute(
                    "UPDATE messages SET status=?,updated_at=?,error_message=? WHERE id=?",
                    (
                        "received"
                        if state == "sent"
                        else "unknown"
                        if state == "unknown"
                        else "failed",
                        utc_now(),
                        error,
                        mid,
                    ),
                )
                if state == "sent":
                    c.execute(
                        "INSERT INTO messages(id,chat_id,scope_id,sender_principal_id,turn_id,role,direction,content,status,created_at,updated_at) VALUES(?,?,?,?,?,'assistant','outgoing',?,'sent',?,?)",
                        (
                            uuid.uuid4().hex,
                            row["chat_id"],
                            row["scope_id"],
                            row["principal_id"],
                            mid,
                            row["generated_reply"],
                            utc_now(),
                            utc_now(),
                        ),
                    )
                    c.execute(
                        "UPDATE chats SET last_reply_at=? WHERE id=?",
                        (utc_now(), row["chat_id"]),
                    )
            c.execute("DELETE FROM job_leases WHERE job_id=?", (jid,))

    def action(self, jid, action, actor, confirm=False):
        with self.storage.transaction() as c:
            require_manager(c, actor)
            row = c.execute("SELECT * FROM message_jobs WHERE id=?", (jid,)).fetchone()
            if not row:
                raise ValueError("任务不存在")
            if action in ("mark_sent", "clone") and confirm is not True:
                raise ValueError("必须明确人工确认微信实际发送状态")
            if action == "retry":
                if row["entered_sending"] or row["state"] not in (
                    "needs_review",
                    "failed",
                    "retry_wait",
                ):
                    raise ValueError("只能重试尚未进入发送阶段的失败任务")
                c.execute(
                    "UPDATE message_jobs SET state='queued',attempts=0,next_attempt_at=0,created_at=?,updated_at=? WHERE id=?",
                    (time.time(), time.time(), jid),
                )
                c.execute(
                    "UPDATE messages SET status='pending' WHERE id=?",
                    (row["inbound_message_id"],),
                )
            elif action == "cancel":
                if row["entered_sending"] or row["state"] in (
                    "sent",
                    "unknown",
                    "sending",
                ):
                    raise ValueError("已进入发送阶段，不能作为未发送取消")
                c.execute(
                    "UPDATE message_jobs SET state='cancelled',updated_at=? WHERE id=?",
                    (time.time(), jid),
                )
                c.execute(
                    "UPDATE messages SET status='failed',error_message='cancelled' WHERE id=?",
                    (row["inbound_message_id"],),
                )
            elif action in ("clone", "mark_sent"):
                if row["state"] != "unknown":
                    raise ValueError("该恢复操作仅用于 unknown 任务")
                if action == "clone":
                    c.execute(
                        "UPDATE message_jobs SET state='cancelled',updated_at=? WHERE id=?",
                        (time.time(), jid),
                    )
                    c.execute(
                        "UPDATE messages SET status='failed',error_message='confirmed_not_sent' WHERE id=?",
                        (row["inbound_message_id"],),
                    )
            else:
                raise ValueError("操作无效")
            record_audit(
                c,
                "queue." + action,
                source=actor.source,
                actor_id=actor.principal_id,
                target_type="job",
                target_id=jid,
            )
        if action == "mark_sent":
            self.finish(jid, "sent")
        if action == "clone":
            data = json.loads(row["payload"])
            return self.enqueue(
                row["chat_id"],
                row["principal_id"],
                row["scope_id"],
                data,
                data.get("question", ""),
                uuid.uuid4().hex,
                answer=row["generated_reply"],
                job_type=row["job_type"],
            )
        return jid
