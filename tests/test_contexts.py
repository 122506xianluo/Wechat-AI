from storage import Storage
from chats import Chats
from contexts import Contexts
from permissions import Permissions


def test_isolated_complete_turns_restart(tmp_path):
    s = Storage(tmp_path)
    Chats(s).import_legacy([], ["G"])
    p = Permissions(s)
    chat = s.chat_id("group", "G")
    a = p.observe_group_sender(chat, "A")
    b = p.observe_group_sender(chat, "B")
    r = Contexts(s)
    sa = r.resolve(chat, a)
    sb = r.resolve(chat, b)
    for n in range(12):
        msg = s.add_incoming("group", "G", str(n))
        r.attach(msg, sa, a)
        s.complete_turn(msg, "group", "G", "answer " + str(n))
    assert r.history(sb["id"], 8) == []
    assert len(Contexts(Storage(tmp_path)).history(sa["id"], 8)) == 16
    assert r.history(sa["id"], 8)[0]["content"] == "4"
