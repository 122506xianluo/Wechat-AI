from chats import Chats
from storage import Storage
from permissions import LOCAL_OWNER


def test_import_once_and_pending(tmp_path):
    s = Storage(tmp_path)
    r = Chats(s)
    r.import_legacy(["same"], ["same"])
    assert r.allowed("private", "same") and r.allowed("group", "same")
    r.import_legacy(["new"], [])
    assert not r.allowed("private", "new")
    found = r.discover("private", "new", "synthetic")
    assert found["approval"] == "pending" and not r.allowed("private", "new")
    r.update(found["id"], {"approval": "approved"}, LOCAL_OWNER)
    assert r.allowed("private", "new")
    r.set_visibility([("private", "new")])
    assert r.get("group", "same")["visibility"] == "not_visible"
