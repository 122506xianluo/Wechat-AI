import time
from storage import Storage
from chats import Chats
from contexts import Contexts
from job_queue import JobQueue
from permissions import Permissions, LOCAL_OWNER


def test_dedup_order_recovery(tmp_path):
    s = Storage(tmp_path)
    Chats(s).import_legacy(["P"], [])
    p = Permissions(s).resolve_incoming("private", "P", "", "incoming")
    scope = Contexts(s).resolve(p.chat_id, p.principal_id)
    q = JobQueue(s)
    args = (p.chat_id, p.principal_id, scope["id"], {"question": "test"}, "test", "key")
    first = q.enqueue(*args)
    assert q.enqueue(*args) is None
    second = q.enqueue(*args[:-1], "key2")
    assert q.claim()["id"] == first
    assert q.claim() is None
    q.generated(first, "answer")
    q.sending(first)
    q.recover()
    assert q.get(first)["state"] == "unknown" and q.claim() is None
    q.action(first, "mark_sent", LOCAL_OWNER, True)
    assert q.claim()["id"] == second
    q.failure(second, TimeoutError())
    assert q.get(second)["state"] == "retry_wait"
    with s.transaction() as c:
        c.execute(
            "UPDATE message_jobs SET created_at=? WHERE id=?",
            (time.time() - 2000, second),
        )
    q.recover()
    assert q.get(second)["state"] == "needs_review"
