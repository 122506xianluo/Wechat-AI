from storage import Storage
from chats import Chats
from members import Members, extract_sender
from permissions import LOCAL_OWNER, Permissions
from tests.fakes import FakeControl


def test_roster_duplicate_and_group_isolation(tmp_path):
    s = Storage(tmp_path)
    Chats(s).import_legacy([], ["G1", "G2"])
    m = Members(s)
    g1 = s.chat_id("group", "G1")
    g2 = s.chat_id("group", "G2")
    a = m.observe(g1, "Nick")
    b = m.observe(g2, "Nick")
    assert a != b
    m.sync_roster(g1, ["Nick", "Nick"], LOCAL_OWNER)
    assert not Permissions(s).resolve(a, g1).allowed
    assert m.diagnostics()
    assert (
        extract_sender(FakeControl(children=[FakeControl("Nick", kind="Button")]))[0]
        == "Nick"
    )
    assert (
        extract_sender(FakeControl(children=[FakeControl("message", kind="Text")]))[0]
        == ""
    )
