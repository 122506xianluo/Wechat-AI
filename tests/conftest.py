from __future__ import annotations

import socket

import pytest

from permissions import Permissions
from storage import Storage


def pytest_addoption(parser):
    parser.addoption("--run-live", action="store_true", default=False,
                     help="Explicitly enable marked live tests; never used by CI")


def pytest_collection_modifyitems(config, items):
    if not config.getoption("--run-live"):
        for item in items:
            if "live" in item.keywords:
                item.add_marker(pytest.mark.skip(reason="live tests require --run-live"))


@pytest.fixture(autouse=True)
def no_live_effects(monkeypatch, request):
    if "live" in request.keywords and request.config.getoption("--run-live"):
        return
    def denied(*args, **kwargs):
        raise AssertionError("Offline test attempted a real network or WeChat connection")
    monkeypatch.setattr(socket.socket, "connect", denied)
    # Imports are lazy in bot.py; no pyweixin/pywinauto import occurs in CI.
    monkeypatch.setattr("bot.WeChatDesktop.__init__", denied)


@pytest.fixture
def storage(tmp_path):
    repository = Storage(tmp_path)
    yield repository
    repository.close()


@pytest.fixture
def permissions(storage):
    storage.register_targets(["Test Friend", "Other Friend"], ["Test Group", "Other Group"])
    service = Permissions(storage)
    service.register_private_targets(["Test Friend", "Other Friend"])
    return service
