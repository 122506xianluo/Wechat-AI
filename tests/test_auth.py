import pytest
from storage import Storage
from auth import Auth
from permissions import PermissionDenied


def test_login_lock_recovery_session(tmp_path):
    a = Auth(Storage(tmp_path))
    code = a.create("owner", "synthetic-password")
    token, csrf = a.login("owner", "synthetic-password", "local")
    assert a.session(token)[0].access_level == "owner" and csrf
    a.logout(token)
    assert a.session(token) is None
    for _ in range(5):
        with pytest.raises(PermissionDenied):
            a.login("owner", "bad", "other")
    with pytest.raises(PermissionDenied):
        a.login("owner", "synthetic-password", "other")
    with a.storage.transaction() as c:
        c.execute("DELETE FROM login_attempts")
    fresh = a.recover_password("owner", code, "new-synthetic-password", "local")
    assert fresh != code
    assert a.login("owner", "new-synthetic-password", "local")
