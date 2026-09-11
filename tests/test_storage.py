import pytest

from permissions import Permissions


def turn(storage, kind, name, text="hello"):
    incoming = storage.add_incoming(kind, name, text)
    return storage.complete_turn(incoming, kind, name, "answer " + text)


def test_chat_kind_isolation_and_bounded_history(storage):
    for i in range(20):
        turn(storage, "private", "same", str(i))
        turn(storage, "group", "same", "group " + str(i))
    history = storage.history("private", "same", 8)
    assert len(history) == 16
    assert history[0]["content"] == "12"
    assert all("group" not in row["content"] for row in history)
    assert storage.stats()["message_count"] == 80
    assert len(storage.list_chats()) == 2


def test_failed_unknown_not_context_and_recovery_not_retry(storage):
    turn(storage, "private", "one")
    item = storage.add_incoming("private", "one", "unfinished")
    storage.add_assistant("private", "one", "unsure", status="unknown")
    storage.add_assistant("private", "one", "bad", status="failed")
    assert storage.recover_incomplete() == 1
    assert storage.recover_incomplete() == 0
    assert len(storage.history("private", "one", 8)) == 2
    with storage._connection() as connection:
        row = connection.execute("SELECT status,error_message FROM messages WHERE id=?", (item,)).fetchone()
        assert tuple(row) == ("failed", "previous_run_interrupted")
    assert storage.stats()["unknown_count"] == 1
    assert storage.stats()["failed_count"] == 2


def test_clear_isolated_history_and_audited(storage):
    turn(storage, "private", "one")
    turn(storage, "group", "one")
    assert storage.clear_chat("private", "one", source="web") == 2
    assert len(storage.history("group", "one", 8)) == 2
    assert Permissions(storage).list_audit()[0]["action"] == "context.clear"
    assert storage.clear_all_history(source="web") == 2
    assert storage.stats()["chat_count"] == 2
    assert Permissions(storage).list_audit()[0]["action"] == "context.clear_all"
    with pytest.raises(ValueError):
        storage.clear_chat("private", "missing")


def test_invalid_chat_and_message_status(storage):
    with pytest.raises(ValueError):
        storage.ensure_chat("broadcast", "test")
    with pytest.raises(ValueError):
        storage.ensure_chat("private", "")
    with pytest.raises(ValueError):
        storage.mark_message("missing", "unsafe")
    with pytest.raises(ValueError):
        storage.add_assistant("private", "test", "x", status="unsafe")


def test_transactions_rollback_even_if_audit_fails(storage, monkeypatch):
    turn(storage, "private", "one")
    def failure(*args, **kwargs):
        raise RuntimeError("audit failed")
    monkeypatch.setattr("storage.record_audit", failure)
    with pytest.raises(RuntimeError):
        storage.clear_chat("private", "one")
    assert len(storage.history("private", "one", 8)) == 2


def test_complete_turn_cannot_duplicate_or_cross_chat(storage):
    incoming = storage.add_incoming("private", "one", "question")
    storage.ensure_chat("group", "one")
    with pytest.raises(ValueError):
        storage.complete_turn(incoming, "group", "one", "wrong chat")
    assert storage.stats()["message_count"] == 1
    storage.complete_turn(incoming, "private", "one", "right chat")
    with pytest.raises(ValueError):
        storage.complete_turn(incoming, "private", "one", "duplicate")
    assert storage.stats()["message_count"] == 2
