from types import SimpleNamespace
import sys

import pytest

from bot import (Config, Message, Policy, SendUncertain, Session, SnapshotTracker,
                 WeChatDesktop, group_sender_name, row_direction, row_token, uia_row_direction)
from tests.fakes import FakeControl


def row(name="Member", side="left", text="/ai status"):
    rect = (8, 10, 48, 50) if side == "left" else (352, 10, 392, 50)
    avatar = FakeControl(name, "Button", rect=rect)
    return FakeControl(text, children=[FakeControl(children=[avatar])])


def test_avatar_nested_name_and_geometry():
    assert group_sender_name(row()) == "Member"
    assert uia_row_direction(row()) == "incoming"
    assert uia_row_direction(row(side="right")) == "outgoing"
    assert row_direction(row(name="Bot"), kind="group", bot_names=["Bot"]) == "outgoing"
    assert row_direction(row(side="right"), kind="group", bot_names=["Bot"]) == "outgoing"
    assert row_direction(FakeControl(), kind="group", bot_names=["Bot"]) == "unknown"


def test_name_without_direction_never_proves_incoming():
    # Name exists but rectangle is unusable: do not turn nickname into authority.
    avatar = FakeControl("Member", "Button", rect=(0, 0, 0, 0))
    control = FakeControl("/ai reset", children=[avatar])
    assert group_sender_name(control) == "Member"
    assert row_direction(control, kind="group", bot_names=["Bot"]) == "unknown"


def test_snapshot_rebaseline_not_replay():
    tracker = SnapshotTracker()
    assert tracker.update("one", ["a", "b"]) == []
    assert tracker.update("one", ["a", "b", "c"]) == [2]
    assert tracker.update("one", ["b", "c"]) == []
    assert tracker.update("one", ["different history"]) == []
    assert tracker.update("other", ["different history", "d"]) == []


@pytest.mark.parametrize("control,accepted", [(FakeControl("/ai reset"), False),
                                             (row(side="right"), False), (row(), True)])
def test_poll_does_not_accept_unknown_by_group_trigger(control, accepted):
    desktop = WeChatDesktop.__new__(WeChatDesktop)
    desktop.cfg = Config(groups=["Test Group"], bot_names=["Bot"], group_mode="prefix")
    desktop.policy, desktop.scale, desktop.ready = Policy(desktop.cfg), 1.0, True
    desktop.tracker = SnapshotTracker()
    desktop.require_foreground = lambda: None
    desktop.sessions = lambda: [Session("key", "Test Group")]
    desktop.activate = lambda *_: "group"
    seed = FakeControl("baseline")
    desktop.rows = lambda: [seed, control]
    desktop.tracker.update("key", [row_token(seed.class_name(), seed.window_text())])
    events = desktop.poll()
    assert bool(events) == accepted
    if accepted:
        assert events[0].direction_verified and events[0].sender_name == "Member"


@pytest.mark.parametrize("failure", ["set_text", "hotkey"])
def test_after_input_exceptions_always_unknown(monkeypatch, failure):
    desktop = WeChatDesktop.__new__(WeChatDesktop)
    desktop.require_foreground = lambda: None
    desktop.sessions = lambda: [Session("key", "Friend")]
    desktop.activate = lambda *_: "private"
    desktop.current = lambda: ("Friend", "private")
    desktop.is_foreground = lambda: True
    edit = SimpleNamespace(text="", window_text=lambda: edit.text, click_input=lambda: None)
    def insert(text):
        edit.text = text  # It may have succeeded even though the call raises.
        if failure == "set_text":
            raise RuntimeError("synthetic setter failure")
    edit.set_text = insert
    desktop._edit = lambda: edit
    def hotkey(*_):
        raise RuntimeError("synthetic send failure")
    monkeypatch.setitem(sys.modules, "pyautogui", SimpleNamespace(hotkey=hotkey))
    with pytest.raises(SendUncertain):
        desktop.send(Message("id", "Friend", "private", "hi"), "reply")
    assert edit.text == "reply"
