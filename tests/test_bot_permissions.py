from __future__ import annotations

from dataclasses import replace
import uuid

import pytest

from bot import Bot, BotError, Config, FocusLost, LLMSettings, Message, SendUncertain
from permissions import LOCAL_OWNER
from tests.fakes import FakeDesktop, FakeLLM


def incoming(text="hello", kind="private", chat="Test Friend", sender="Member A", **kwargs):
    return Message(uuid.uuid4().hex, chat, kind, text, sender_name=sender,
                   direction=kwargs.pop("direction", "incoming"),
                   direction_verified=kwargs.pop("direction_verified", True), **kwargs)


@pytest.fixture
def bot_env(tmp_path, storage):
    cfg = Config(private_chats=["Test Friend", "Other Friend"], groups=["Test Group"],
                 bot_names=["Test Bot"], group_mode="all")
    desktop, llm = FakeDesktop(), FakeLLM()
    bot = Bot(tmp_path, cfg, LLMSettings("http://localhost/v1", "", "fake"),
              desktop=desktop, llm=llm, storage=storage)
    yield bot, desktop, llm
    bot.close()


def allow_group(bot, level="user"):
    chat = bot.storage.chat_id("group", "Test Group")
    principal = bot.permissions.observe_group_sender(chat, "Member A")
    bot.permissions.set_status(principal, "active", actor=LOCAL_OWNER)
    bot.permissions.set_grant(principal, level, chat, actor=LOCAL_OWNER)
    return principal, chat


def private_decision(bot):
    return bot.permissions.resolve_incoming("private", "Test Friend", "", "incoming")


def test_private_reply_and_bounded_persisted_context(bot_env):
    bot, desktop, llm = bot_env
    bot.cfg.context_turns = 2
    for i in range(5):
        assert bot.process_message(incoming(str(i)))
    assert len(desktop.sent) == 5
    assert len(llm.calls[-1][1]) == 4
    assert bot.storage.stats()["message_count"] == 10
    cfg = bot.cfg
    other_llm = FakeLLM()
    restarted = Bot(bot.root, cfg, LLMSettings("http://localhost", "", "fake"),
                    desktop=desktop, llm=other_llm, storage=bot.storage)
    restarted.process_message(incoming("after restart"))
    assert len(other_llm.calls[0][1]) == 4
    restarted.close()


@pytest.mark.parametrize("text", ["hello", "/ai help", "/ai reset", "/ai status"])
def test_blocked_no_llm_no_command_no_send(bot_env, text):
    bot, desktop, llm = bot_env
    p = private_decision(bot)
    bot.permissions.set_grant(p.principal_id, "blocked", None, actor=LOCAL_OWNER)
    before = len(bot.permissions.list_audit())
    bot.process_message(incoming(text))
    assert not llm.calls and not desktop.sent
    assert bot.storage.stats()["message_count"] == 0
    assert len(bot.permissions.list_audit()) == before


@pytest.mark.parametrize("direction", ["outgoing", "unknown"])
def test_own_outgoing_never_a_command(bot_env, direction):
    bot, desktop, llm = bot_env
    bot.process_message(incoming())
    count = bot.storage.stats()["message_count"]
    bot.process_message(incoming("/ai reset", direction=direction))
    assert bot.storage.stats()["message_count"] == count
    assert len(desktop.sent) == len(llm.calls) == 1


def test_unverified_direction_command_skipped_not_passed_to_model(bot_env):
    bot, desktop, llm = bot_env
    bot.process_message(incoming("/ai reset", direction_verified=False))
    assert not llm.calls and not desktop.sent
    assert bot.permissions.list_audit()[0]["details"]["outcome"] == "direction_unverified"


def test_group_pending_unknown_not_authorized(bot_env):
    bot, desktop, llm = bot_env
    for text, sender in [("hello", "Member A"), ("/ai status", "Member A"),
                         ("@Test Bot /ai status", "")]:
        bot.process_message(incoming(text, kind="group", chat="Test Group", sender=sender))
    assert not llm.calls and not desktop.sent
    assert bot.permissions.list_principals("Member A")[0]["status"] == "pending"


def test_group_status_only_resolved_admin(bot_env):
    bot, desktop, llm = bot_env
    item, chat = allow_group(bot)
    event = incoming("/ai status", kind="group", chat="Test Group")
    bot.process_message(event)
    assert not desktop.sent
    bot.permissions.set_grant(item, "admin", chat, actor=LOCAL_OWNER)
    bot.process_message(replace(event, id=uuid.uuid4().hex))
    assert "admin" in desktop.sent[0][1]
    assert not llm.calls and bot.storage.stats()["message_count"] == 0


def test_private_commands_and_reset_only_self(bot_env):
    bot, desktop, llm = bot_env
    bot.process_message(incoming())
    bot.process_message(incoming(chat="Other Friend"))
    for text in ("/ai help", "/ai status", "/ai reset"):
        bot.process_message(incoming(text))
    assert len(llm.calls) == 2  # No command is a model call or model-history turn.
    assert not bot.storage.history("private", "Test Friend", 8)
    assert len(bot.storage.history("private", "Other Friend", 8)) == 2
    assert len(desktop.sent) == 5
    assert "context.clear_scope" in [e["action"] for e in bot.permissions.list_audit()]


def test_group_reset_never_erases_others(bot_env):
    bot, desktop, llm = bot_env
    allow_group(bot, "admin")
    bot.process_message(incoming("hello", kind="group", chat="Test Group"))
    bot.process_message(incoming("/ai reset", kind="group", chat="Test Group"))
    assert len(bot.storage.history("group", "Test Group", 8)) == 0
    assert len(llm.calls) == 1 and "独立上下文" in desktop.sent[-1][1]


def test_group_normal_trigger_checked_after_permissions(bot_env):
    bot, desktop, llm = bot_env
    allow_group(bot)
    bot.cfg.group_mode = "mention"
    bot.process_message(incoming("plain", kind="group", chat="Test Group"))
    bot.process_message(incoming("@Test Bot hello", kind="group", chat="Test Group"))
    assert len(llm.calls) == 1 and llm.calls[0][0] == "hello"
    bot.cfg.group_mode = "prefix"
    bot.process_message(incoming("/ai normal question", kind="group", chat="Test Group"))
    assert llm.calls[-1][0] == "normal question"
    assert len(desktop.sent) == 2


def test_llm_failure_and_focus_loss_preserve_completed_history(bot_env):
    bot, desktop, llm = bot_env
    bot.process_message(incoming())
    def fail():
        raise BotError("synthetic upstream failure")
    llm.effect = fail
    assert bot.process_message(incoming("failure"))
    assert len(bot.storage.history("private", "Test Friend", 8)) == 2
    llm.effect = lambda: setattr(desktop, "foreground", False)
    assert not bot.process_message(incoming("focus"))
    assert len(desktop.sent) == 1
    assert len(bot.storage.history("private", "Test Friend", 8)) == 2
    assert bot.storage.stats()["failed_count"] == 2


def test_block_or_stop_during_model_call_prevents_send(bot_env):
    bot, desktop, llm = bot_env
    p = private_decision(bot)
    llm.effect = lambda: bot.permissions.set_grant(p.principal_id, "blocked", None, actor=LOCAL_OWNER)
    bot.process_message(incoming())
    assert not desktop.sent
    assert bot.storage.stats()["failed_count"] == 1
    bot.permissions.remove_grant(p.principal_id, None, actor=LOCAL_OWNER)
    llm.effect = lambda: (bot.root / "STOP").write_text("stop")
    assert not bot.process_message(incoming("stop now"))
    assert not desktop.sent


@pytest.mark.parametrize("error,status", [(SendUncertain("unknown"), "unknown"),
                                         (FocusLost("focus"), "failed"), (BotError("send"), "failed")])
def test_send_exceptions_saved_but_never_in_history(bot_env, error, status):
    bot, desktop, llm = bot_env
    desktop.error = error
    with pytest.raises(type(error)):
        bot.process_message(incoming())
    assert not bot.storage.history("private", "Test Friend", 8)
    assert bot.storage.stats()[status + "_count"] == 2
    assert bot.storage.recover_incomplete() == 0
    assert len(llm.calls) == 1  # no automatic resend/re-generation on recovery


def test_command_send_unknown_audited(bot_env):
    bot, desktop, llm = bot_env
    desktop.error = SendUncertain("unknown")
    with pytest.raises(SendUncertain):
        bot.process_message(incoming("/ai help"))
    assert bot.permissions.list_audit()[0]["details"]["outcome"] == "reply_unknown"
    assert not llm.calls


def test_run_uses_one_adapter_and_stop_file(bot_env, monkeypatch):
    bot, desktop, llm = bot_env
    desktop.pending = [incoming("from poll")]
    clock = iter(range(1000))
    monkeypatch.setattr("bot.time.monotonic", lambda: next(clock))
    monkeypatch.setattr("bot.time.sleep", lambda *_: None)
    count = 0
    def poll():
        nonlocal count
        count += 1
        if count > 1:
            (bot.root / "STOP").write_text("stop")
    desktop.on_poll = poll
    bot.run()
    assert desktop.warmed == 1
    assert len(llm.calls) == len(desktop.sent) == 1


def test_malformed_sender_and_overlong_command_safe_skip(bot_env):
    bot, desktop, llm = bot_env
    bot.process_message(incoming("/ai status", kind="group", chat="Test Group", sender="x" * 257))
    bot.process_message(incoming("/ai help " + "x" * 4000))
    assert not llm.calls and not desktop.sent


def test_administrator_demotion_before_status_reply(bot_env):
    bot, desktop, llm = bot_env
    item, chat = allow_group(bot, "admin")
    original = bot.commands.execute
    def execute(*args):
        answer = original(*args)
        bot.permissions.set_grant(item, "user", chat, actor=LOCAL_OWNER)
        return answer
    bot.commands.execute = execute
    bot.process_message(incoming("/ai status", kind="group", chat="Test Group"))
    assert not desktop.sent and not llm.calls
    assert bot.permissions.list_audit()[0]["details"]["outcome"] == "reply_revoked"
