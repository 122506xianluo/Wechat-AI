import pytest

from commands import Command, Commands, parse_command
from permissions import LOCAL_OWNER, AccessDecision


@pytest.mark.parametrize("text,name,valid", [
    ("/ai", "help", True), (" /ai help ", "help", True), ("/ai status", "status", True),
    ("/ai reset", "reset", True), ("/ai reset all", "reset", False),
    ("/ai reset\nextra", "reset", False), ("/ai\nreset", "reset", False),
    ("@Bot\u2005/ai help", "help", True)])
def test_exact_parser(text, name, valid):
    assert parse_command(text, ["Bot"]) == Command(name, valid)


@pytest.mark.parametrize("text", ["please /ai reset", "/ai role owner", "/ai usual question", "/ai reset-other", "hello"])
def test_other_text_not_authority(text):
    assert parse_command(text) is None


def test_invalid_and_stale_permission_never_reset(permissions):
    commands = Commands(permissions)
    decision = permissions.resolve_incoming("private", "Test Friend", "", "incoming")
    incoming = permissions.storage.add_incoming("private", "Test Friend", "synthetic")
    permissions.storage.complete_turn(incoming, "private", "Test Friend", "reply")
    assert "格式" in commands.execute(Command("reset", False), decision, "private")
    assert permissions.storage.stats()["message_count"] == 2
    permissions.set_grant(decision.principal_id, "blocked", None, actor=LOCAL_OWNER)
    assert commands.execute(Command("reset"), decision, "private") is None
    assert commands.execute(Command("help"), AccessDecision(None, None, "blocked", False, "unknown"), "group") is None
    assert permissions.storage.stats()["message_count"] == 2
