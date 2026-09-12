from __future__ import annotations
from migrations import SCHEMA_VERSION

import json
from pathlib import Path

import pytest
from werkzeug.test import EnvironBuilder, run_wsgi_app

import app as panel

BASE = "http://127.0.0.1:18787"


@pytest.fixture
def client(tmp_path, permissions, monkeypatch):
    monkeypatch.setattr(panel, "_storage", permissions.storage)
    monkeypatch.setattr(panel, "ROOT", tmp_path)
    for name, relative in {
        "ENV_FILE": ".env",
        "CONFIG_FILE": "config.json",
        "STOP_FILE": "STOP",
        "BOT_PID": "data/bot.pid",
        "LOG_FILE": "data/bot.log",
        "PANEL_LOG": "data/panel.log",
        "SETUP_LOG": "data/setup.log",
    }.items():
        monkeypatch.setattr(panel, name, tmp_path / relative)
    monkeypatch.setattr(panel, "bot_running", lambda: False)
    monkeypatch.setattr(panel, "read_pid", lambda *_: None)
    panel.app.config.update(TESTING=True, LOCAL_CSRF_TOKEN="synthetic-csrf-token")
    from auth import Auth, digest

    auth = Auth(permissions.storage)
    auth.create("synthetic-owner", "synthetic-password")
    token, _ = auth.login("synthetic-owner", "synthetic-password", "test")
    with permissions.storage.transaction() as c:
        c.execute(
            "UPDATE web_sessions SET csrf_token=? WHERE token_hash=?",
            ("synthetic-csrf-token", digest(token)),
        )
    client = panel.app.test_client()
    client.set_cookie("wechat_ai_session", token, domain="127.0.0.1")
    return client


def get(client, route, **kwargs):
    return client.get(route, base_url=BASE, **kwargs)


def post(client, route, data, **kwargs):
    return client.post(
        route,
        base_url=BASE,
        json=data,
        headers={"X-CSRF-Token": "synthetic-csrf-token", **kwargs.pop("headers", {})},
        **kwargs,
    )


def test_page_renders_controls_csrf_and_safe_headers(client):
    response = get(client, "/")
    assert response.status_code == 200
    assert b"synthetic-csrf-token" in response.data
    assert "身份与权限" in response.get_data(as_text=True)
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["X-Frame-Options"] == "DENY"


@pytest.mark.parametrize(
    "route",
    [
        "/",
        "/api/state",
        "/api/log",
        "/api/storage/chats",
        "/api/v1/principals",
        "/api/v1/audit",
    ],
)
def test_all_reads_reject_remote_and_rebinding(client, route):
    assert (
        client.get(
            route, base_url=BASE, environ_overrides={"REMOTE_ADDR": "192.0.2.9"}
        ).status_code
        == 403
    )
    assert client.get(route, base_url="http://evil.example:18787").status_code == 403
    assert (
        get(client, route, headers={"Origin": "https://evil.example"}).status_code
        == 403
    )
    assert (
        get(client, route, headers={"X-Forwarded-For": "127.0.0.1"}).status_code == 403
    )


@pytest.mark.parametrize(
    "route",
    [
        "/api/start",
        "/api/stop",
        "/api/save",
        "/api/test",
        "/api/storage/clear-chat",
        "/api/storage/clear-all",
        "/api/v1/permissions",
        "/api/v1/principals/1/status",
    ],
)
def test_all_mutations_require_csrf_including_old_aliases(client, route):
    assert client.post(route, base_url=BASE, json={}).status_code == 403
    assert (
        post(client, route, {}, headers={"Origin": "https://evil.example"}).status_code
        == 403
    )
    assert (
        post(client, route, {}, headers={"Sec-Fetch-Site": "cross-site"}).status_code
        == 403
    )
    assert post(client, route, [], headers={"Origin": BASE}).status_code == 400
    assert (
        client.post(
            route,
            base_url=BASE,
            data="form=bad",
            headers={"X-CSRF-Token": "synthetic-csrf-token"},
        ).status_code
        == 400
    )


def test_permissions_set_revoke_and_audit(client, permissions):
    p = permissions.resolve_incoming("private", "Test Friend", "", "incoming")
    data = {
        "principal_id": p.principal_id,
        "scope": "chat",
        "chat_id": p.chat_id,
        "access_level": "blocked",
    }
    response = post(client, "/api/v1/permissions", data)
    assert response.status_code == 200
    assert not permissions.resolve(p.principal_id, p.chat_id).allowed
    response = post(client, "/api/v1/permissions", {**data, "operation": "revoke"})
    assert response.status_code == 200
    assert permissions.resolve(p.principal_id, p.chat_id).allowed
    events = get(client, "/api/v1/audit").json["events"]
    assert events[0]["source"] == "web" and events[0]["action"] == "permission.revoke"
    assert (
        get(client, "/api/v1/audit?limit=1").json["events"][0]["id"] == events[0]["id"]
    )
    assert (
        len(get(client, "/api/v1/principals?q=Test%20Friend").json["principals"]) == 1
    )


def test_owner_cannot_be_requested_for_wechat_even_by_payload_actor(
    client, permissions
):
    p = permissions.resolve_incoming("private", "Test Friend", "", "incoming")
    response = post(
        client,
        "/api/v1/permissions",
        {
            "principal_id": p.principal_id,
            "scope": "global",
            "access_level": "owner",
            "actor": {"access_level": "owner"},
        },
    )
    assert response.status_code == 403
    assert permissions.resolve(p.principal_id, p.chat_id).access_level == "user"


def test_approval_confirmation_and_ambiguous_rejection(client, permissions):
    chat = permissions.storage.chat_id("group", "Test Group")
    item = permissions.observe_group_sender(chat, "New Member")
    url = f"/api/v1/principals/{item}/status"
    assert post(client, url, {"status": "active"}).status_code == 400
    assert (
        post(client, url, {"status": "active", "confirm_identity": "true"}).status_code
        == 400
    )
    assert (
        post(client, url, {"status": "active", "confirm_identity": True}).status_code
        == 200
    )
    assert post(client, url, {"status": "disabled"}).status_code == 200
    with permissions.storage.transaction() as connection:
        connection.execute(
            "UPDATE principals SET status='ambiguous' WHERE id=?", (item,)
        )
    assert (
        post(client, url, {"status": "active", "confirm_identity": True}).status_code
        == 403
    )


@pytest.mark.parametrize(
    "data",
    [
        None,
        [],
        {"principal_id": True, "scope": "global", "access_level": "admin"},
        {"principal_id": 1, "scope": "any", "access_level": "user"},
        {"principal_id": 1, "scope": "chat", "chat_id": "1", "access_level": "user"},
        {"principal_id": 1, "scope": "global", "access_level": ["owner"]},
    ],
)
def test_malformed_payloads_rejected(client, data):
    assert post(client, "/api/v1/permissions", data).status_code in (400, 403)
    assert get(client, "/api/v1/audit?limit=bad").status_code == 400


def test_clear_context_audit_confirm_and_running_guard(
    client, permissions, monkeypatch
):
    storage = permissions.storage
    incoming = storage.add_incoming("private", "Test Friend", "synthetic")
    storage.complete_turn(incoming, "private", "Test Friend", "reply")
    monkeypatch.setattr(panel, "bot_running", lambda: True)
    assert (
        post(
            client,
            "/api/storage/clear-chat",
            {"kind": "private", "name": "Test Friend"},
        ).status_code
        == 400
    )
    monkeypatch.setattr(panel, "bot_running", lambda: False)
    assert post(client, "/api/storage/clear-all", {}).status_code == 400
    assert storage.stats()["message_count"] == 2
    assert (
        post(
            client,
            "/api/storage/clear-chat",
            {"kind": "private", "name": "Test Friend"},
        ).status_code
        == 200
    )
    assert get(client, "/api/v1/audit").json["events"][0]["action"] == "context.clear"
    assert (
        post(client, "/api/storage/clear-all", {"confirm": "clear-all"}).status_code
        == 200
    )
    assert (
        get(client, "/api/v1/audit").json["events"][0]["action"] == "context.clear_all"
    )


def test_secrets_never_returned_by_state_or_audit(client, permissions):
    key = "sk-synthetic-secret-never-real"
    panel.ENV_FILE.write_text(
        "LLM_BASE_URL=https://model.invalid/v1\nLLM_API_KEY="
        + key
        + "\nLLM_MODEL=fake\n"
    )
    response = get(client, "/api/state")
    assert response.status_code == 200
    assert key not in response.get_data(as_text=True)
    assert response.json["llm_api_key_masked"].endswith(key[-4:])
    assert key not in get(client, "/api/v1/audit").get_data(as_text=True)
    assert key.encode() not in permissions.storage.path.read_bytes()


def test_untrusted_names_rendered_as_text_not_innerhtml(client, permissions):
    item = permissions.observe_group_sender(
        permissions.storage.chat_id("group", "Test Group"),
        "<img src=x onerror=alert(1)>",
    )
    assert item
    result = get(client, "/api/v1/principals").json["principals"]
    assert any(p["display_name"].startswith("<img") for p in result)
    template = Path(panel.app.template_folder) / "index.html"
    source = template.read_text(encoding="utf-8")
    assert "innerHTML" not in source
    assert "option.textContent" in source


def test_lazy_storage_initialization_imports_only_config(tmp_path, monkeypatch):
    monkeypatch.setattr(panel, "ROOT", tmp_path)
    monkeypatch.setattr(panel, "CONFIG_FILE", tmp_path / "config.json")
    monkeypatch.setattr(panel, "_storage", None)
    panel.CONFIG_FILE.write_text(
        json.dumps({"private_chats": ["Synthetic"]}), encoding="utf-8"
    )
    storage = panel.get_storage()
    assert storage.stats()["schema_version"] == SCHEMA_VERSION
    assert panel.Permissions(storage).list_principals()[0]["status"] == "active"


def test_non_ascii_csrf_is_denied_not_server_error(client):
    response = post(
        client, "/api/v1/permissions", {}, headers={"X-CSRF-Token": "伪造令牌"}
    )
    assert response.status_code == 403


def test_invalid_and_userinfo_host_not_owner(client):
    # Client.get reparses invalid ports after the response; exercise the WSGI
    # boundary directly so malformed hosts reach our guard without that parser.
    for host in ("127.0.0.1:99999", "127.0.0.1.evil.example", "user@127.0.0.1:18787"):
        builder = EnvironBuilder(path="/api/v1/principals", base_url=BASE)
        try:
            environ = builder.get_environ()
        finally:
            builder.close()
        environ.update(HTTP_HOST=host, REMOTE_ADDR="127.0.0.1")
        _, status, _ = run_wsgi_app(panel.app.wsgi_app, environ, buffered=True)
        assert status.startswith("403 ")


def test_config_check_uses_utf8_without_starting_bot(client, monkeypatch):
    from types import SimpleNamespace

    calls = []
    monkeypatch.setattr(panel, "venv_python", lambda *_: Path("synthetic-python.exe"))

    def fake_run(args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(returncode=0, stdout="checked", stderr="")

    monkeypatch.setattr(panel.subprocess, "run", fake_run)
    assert panel.run_check() == (True, "checked")
    args, kwargs = calls[0]
    assert args[1:3] == ["-X", "utf8"] and args[-1] == "--check"
    assert kwargs["encoding"] == "utf-8" and kwargs["capture_output"]


def test_owner_initialization_logs_in_and_enters_console(tmp_path, monkeypatch):
    # Use a fresh database so the first-run /init path is exercised.
    monkeypatch.setattr(panel, "ROOT", tmp_path)
    monkeypatch.setattr(panel, "_storage", panel.Storage(tmp_path))
    monkeypatch.setattr(panel, "CONFIG_FILE", tmp_path / "config.json")
    monkeypatch.setattr(panel, "ENV_FILE", tmp_path / ".env")
    monkeypatch.setattr(panel, "BOT_PID", tmp_path / "data" / "bot.pid")
    monkeypatch.setattr(panel, "LOG_FILE", tmp_path / "data" / "bot.log")
    monkeypatch.setattr(panel, "PANEL_LOG", tmp_path / "data" / "panel.log")
    monkeypatch.setattr(panel, "SETUP_LOG", tmp_path / "data" / "setup.log")
    panel.app.config.update(TESTING=True, LOCAL_CSRF_TOKEN="synthetic-csrf-token")

    client = panel.app.test_client()
    response = client.post(
        "/api/v1/auth/init",
        base_url=BASE,
        json={"username": "first-owner", "password": "synthetic-password"},
        headers={"X-CSRF-Token": "synthetic-csrf-token"},
    )

    assert response.status_code == 200
    assert response.json["authenticated"] is True
    assert response.json["csrf_token"]
    assert response.json["recovery_code"]
    assert any(
        "wechat_ai_session=" in value
        for value in response.headers.getlist("Set-Cookie")
    )

    console = client.get("/", base_url=BASE)
    assert console.status_code == 200
    assert "登录管理后台" not in console.get_data(as_text=True)


def test_owner_initialization_page_offers_console_button():
    source = Path(panel.app.template_folder, "login.html").read_text(encoding="utf-8")
    assert "我已保存恢复码，进入控制台" in source
    assert "保存后刷新页面登录" not in source
