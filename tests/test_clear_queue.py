import pytest
from storage import Storage
from chats import Chats
from contexts import Contexts
from permissions import Permissions
from job_queue import JobQueue


def test_clear_chat_cancels_pre_send_and_rejects_unknown(tmp_path):
    s = Storage(tmp_path)
    Chats(s).import_legacy(["A"], [])
    d = Permissions(s).resolve_incoming("private", "A", "", "incoming")
    scope = Contexts(s).resolve(d.chat_id, d.principal_id)
    q = JobQueue(s)
    jid = q.enqueue(
        d.chat_id, d.principal_id, scope["id"], {"question": "x"}, "x", "unique"
    )
    assert s.clear_chat("private", "A") == 1
    assert q.get(jid) is None  # inbound row deletion cascades its cancelled job
    d = Permissions(s).resolve_incoming("private", "A", "", "incoming")
    scope = Contexts(s).resolve(d.chat_id, d.principal_id)
    jid = q.enqueue(d.chat_id, d.principal_id, scope["id"], {}, "x", "unique2")
    q.claim(jid)
    q.generated(jid, "answer")
    q.sending(jid)
    with pytest.raises(ValueError):
        s.clear_chat("private", "A")
