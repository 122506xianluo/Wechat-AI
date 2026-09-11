import sqlite3

import pytest

from permissions import Actor, LOCAL_OWNER, PermissionDenied, Permissions, normalize_name
from storage import utc_now


def private(service, name="Test Friend"):
    return service.resolve_incoming("private", name, "", "incoming")


def member(service, name="Member A", group="Test Group", approve=True):
    chat_id = service.storage.chat_id("group", group)
    principal_id = service.observe_group_sender(chat_id, name)
    if approve:
        service.set_status(principal_id, "active", actor=LOCAL_OWNER)
    return principal_id, chat_id


def test_default_private_and_group_pending(permissions):
    assert private(permissions).access_level == "user"
    item, chat = member(permissions, approve=False)
    assert permissions.resolve(item, chat).reason == "pending"
    permissions.set_grant(item, "admin", chat, actor=LOCAL_OWNER)
    assert not permissions.resolve(item, chat).allowed  # grant cannot approve implicitly
    permissions.set_status(item, "active", actor=LOCAL_OWNER)
    assert permissions.resolve(item, chat).access_level == "admin"


def test_deny_wins_and_scoped_override(permissions):
    item, chat = member(permissions)
    permissions.set_grant(item, "admin", None, actor=LOCAL_OWNER)
    assert permissions.resolve(item, chat).access_level == "admin"
    permissions.set_grant(item, "user", chat, actor=LOCAL_OWNER)
    assert permissions.resolve(item, chat).access_level == "user"
    permissions.set_grant(item, "blocked", None, actor=LOCAL_OWNER)
    permissions.set_grant(item, "admin", chat, actor=LOCAL_OWNER)
    assert not permissions.resolve(item, chat).allowed
    permissions.remove_grant(item, None, actor=LOCAL_OWNER)
    assert permissions.resolve(item, chat).allowed
    permissions.set_grant(item, "blocked", chat, actor=LOCAL_OWNER)
    permissions.set_grant(item, "admin", None, actor=LOCAL_OWNER)
    assert permissions.resolve(item, chat).access_level == "blocked"


def test_wechat_can_never_be_owner_service_and_sql(permissions):
    item = private(permissions).principal_id
    with pytest.raises(PermissionDenied):
        permissions.set_grant(item, "owner", None, actor=LOCAL_OWNER)
    with pytest.raises(sqlite3.IntegrityError):
        with permissions.storage.transaction() as connection:
            connection.execute("INSERT INTO access_grants(principal_id,access_level,created_at,updated_at) VALUES(?,'owner','t','t')", (item,))
    permissions.set_grant(item, "admin", None, actor=LOCAL_OWNER)
    with pytest.raises(sqlite3.IntegrityError):
        with permissions.storage.transaction() as connection:
            connection.execute("UPDATE access_grants SET access_level='owner' WHERE principal_id=?", (item,))
    with pytest.raises(PermissionDenied):
        permissions.set_grant(item, "user", None, actor=Actor(None, "owner", "wechat"))


def test_admin_cannot_escalate_or_manage_outside_chat(permissions):
    admin, chat = member(permissions, "Admin")
    other, _ = member(permissions, "User")
    outside, outside_chat = member(permissions, "Outside", "Other Group")
    permissions.set_grant(admin, "admin", chat, actor=LOCAL_OWNER)
    actor = permissions.resolve(admin, chat).as_actor()
    permissions.set_grant(other, "blocked", chat, actor=actor)
    assert not permissions.resolve(other, chat).allowed
    for target, level, scope in [(other, "admin", chat), (outside, "user", outside_chat),
                                  (other, "user", None), (admin, "user", chat)]:
        with pytest.raises(PermissionDenied):
            permissions.set_grant(target, level, scope, actor=actor)
    permissions.set_grant(admin, "blocked", chat, actor=LOCAL_OWNER)
    with pytest.raises(PermissionDenied):
        permissions.remove_grant(other, chat, actor=actor)  # revalidated, not stale actor


def test_only_active_exact_identity_in_chat(permissions):
    item, chat = member(permissions, "Same")
    other, other_chat = member(permissions, "Same", "Other Group")
    assert item != other
    assert not permissions.resolve(item, other_chat).allowed
    assert permissions.observe_group_sender(chat, " Same\u2005 ") == item
    spaced = permissions.observe_group_sender(chat, "S a m e")
    assert spaced != item and not permissions.resolve(spaced, chat).allowed
    renamed = permissions.observe_group_sender(chat, "New Name")
    assert renamed != item
    assert permissions.resolve(renamed, chat).reason == "pending"
    assert permissions.resolve(other, other_chat).allowed


@pytest.mark.parametrize("status", ["pending", "ambiguous", "renamed", "merged", "disabled"])
def test_inactive_identity_never_authorized(permissions, status):
    item, chat = member(permissions)
    permissions.set_grant(item, "admin", None, actor=LOCAL_OWNER)
    with permissions.storage.transaction() as connection:
        connection.execute("UPDATE principals SET status=? WHERE id=?", (status, item))
    assert not permissions.resolve(item, chat).allowed
    if status in ("ambiguous", "renamed", "merged"):
        with pytest.raises(PermissionDenied):
            permissions.set_status(item, "active", actor=LOCAL_OWNER)


def test_own_outgoing_unknown_or_disabled_chat(permissions):
    for direction in ("outgoing", "unknown", ""):
        assert not permissions.resolve_incoming("private", "Test Friend", "", direction).allowed
    assert not permissions.resolve_incoming("private", "Unknown", "", "incoming").allowed
    assert permissions.resolve_incoming("group", "Test Group", "", "incoming").reason == "sender_unknown"
    assert permissions.resolve_incoming("group", "Test Group", "\u2005", "incoming").reason == "sender_unknown"
    with permissions.storage.transaction() as connection:
        connection.execute("UPDATE chats SET enabled=0 WHERE name='Test Friend'")
    assert not private(permissions).allowed


def test_every_mutation_audited_and_revision_persistent(permissions):
    item, chat = member(permissions)
    original = permissions.revision()
    permissions.set_grant(item, "admin", chat, actor=LOCAL_OWNER)
    permissions.set_grant(item, "blocked", chat, actor=LOCAL_OWNER)
    permissions.remove_grant(item, chat, actor=LOCAL_OWNER)
    permissions.set_status(item, "disabled", actor=LOCAL_OWNER)
    assert permissions.revision() == original + 4
    reloaded = Permissions(permissions.storage)
    assert reloaded.revision() == original + 4
    events = reloaded.list_audit()
    assert {e["action"] for e in events} >= {"identity.discovered", "identity.status", "permission.set", "permission.revoke"}
    assert len(reloaded.list_audit(events[0]["id"], 2)) == 2
    assert len(permissions.list_principals("Member")) == 1
    assert permissions.list_principals("not-found") == []
    # Repeat registration must NOT undo a block/disable after a restart.
    p = private(permissions)
    permissions.set_status(p.principal_id, "disabled", actor=LOCAL_OWNER)
    permissions.register_private_targets(["Test Friend"])
    assert not private(permissions).allowed


def test_invalid_operations_rollback_without_audit(permissions):
    item, chat = member(permissions)
    initial = len(permissions.list_audit())
    invalid = [
        lambda: permissions.set_grant(item, "root", chat, actor=LOCAL_OWNER),
        lambda: permissions.set_grant(item, "user", 98765, actor=LOCAL_OWNER),
        lambda: permissions.set_grant(99999, "user", chat, actor=LOCAL_OWNER),
        lambda: permissions.set_grant(item, "user", permissions.storage.chat_id("group", "Other Group"), actor=LOCAL_OWNER),
        lambda: permissions.set_status(item, "ambiguous", actor=LOCAL_OWNER),
        lambda: permissions.remove_grant(item, None, actor=LOCAL_OWNER),
        lambda: permissions.observe_group_sender(chat, ""),
        lambda: permissions.observe_group_sender(permissions.storage.chat_id("private", "Test Friend"), "bad"),
    ]
    for operation in invalid:
        with pytest.raises(ValueError):
            operation()
    assert len(permissions.list_audit()) == initial
    with pytest.raises(ValueError):
        normalize_name("x" * 257)
    assert normalize_name("张 三\u2005") == "张 三"
    assert normalize_name("AbＣ") == "AbＣ"


def test_web_owner_and_global_admin_boundaries(permissions):
    now = utc_now()
    with permissions.storage.transaction() as connection:
        owner = connection.execute(
            "INSERT INTO principals(kind,display_name,normalized_name,status,created_at,updated_at,first_seen_at,last_seen_at) "
            "VALUES('web_account','Owner','Owner','active',?,?,?,?)", (now,now,now,now)).lastrowid
        admin = connection.execute(
            "INSERT INTO principals(kind,display_name,normalized_name,status,created_at,updated_at,first_seen_at,last_seen_at) "
            "VALUES('web_account','Admin','Admin','active',?,?,?,?)", (now,now,now,now)).lastrowid
    permissions.set_grant(owner, "owner", None, actor=LOCAL_OWNER)
    owner_actor = Actor(owner, "owner", "web")
    permissions.set_grant(admin, "admin", None, actor=owner_actor)
    admin_actor = Actor(admin, "admin", "web")
    p = private(permissions)
    permissions.set_grant(p.principal_id, "blocked", p.chat_id, actor=admin_actor)
    with pytest.raises(PermissionDenied):
        permissions.set_grant(owner, "user", None, actor=admin_actor)
    with pytest.raises(PermissionDenied):
        permissions.remove_grant(owner, None, actor=owner_actor)
    with pytest.raises(PermissionDenied):
        permissions.set_status(owner, "disabled", actor=LOCAL_OWNER)
    with pytest.raises(PermissionDenied):
        permissions.set_grant(p.principal_id, "user", p.chat_id, actor=Actor(None, "admin", "web"))
