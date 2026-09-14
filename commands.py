"""Exact, low-risk WeChat commands; never parse natural language as authority."""
from __future__ import annotations

from dataclasses import dataclass
import re

from permissions import AccessDecision, Permissions
from roles import Roles

HELP = "可用命令：/ai help、/ai status、/ai reset、/ai role、/ai role list、/ai role use <角色名>。status 只查看自己的状态；reset 仅清空自己的独立上下文。"


@dataclass(frozen=True)
class Command:
    name: str
    valid: bool = True
    argument: str = ""


def parse_command(text: str, bot_names: list[str] | None = None) -> Command | None:
    text = text.strip()
    for name in bot_names or []:
        text = re.sub(r"^@" + re.escape(name) + r"(?:[\s\u2005]+|$)", "", text).strip()
    if text == "/ai":
        return Command("help")
    # Reserve only known subcommands. '/ai 普通提问' remains a normal prefix
    # trigger; extra arguments or multiline command text never perform a reset.
    if text == "/ai role":
        return Command("role")
    if text == "/ai role list":
        return Command("role_list")
    if text.startswith("/ai role use "):
        return Command("role_use", "\n" not in text, text[len("/ai role use "):].strip())
    parts = text.split()
    if len(parts) >= 2 and parts[0] == "/ai" and parts[1] in ("help", "status", "reset"):
        return Command(parts[1], len(parts) == 2 and "\n" not in text and "\r" not in text)
    return None


class Commands:
    def __init__(self, permissions: Permissions):
        self.permissions = permissions
        self.roles = Roles(permissions.storage)

    def execute(self, command: Command, decision: AccessDecision, kind: str) -> str | None:
        if not decision.allowed or decision.principal_id is None:
            return None
        current = self.permissions.resolve(decision.principal_id, decision.chat_id)
        if not current.allowed:
            return None
        if not command.valid:
            self.permissions.audit_command(current, command.name, "invalid_arguments")
            return "命令格式不正确，请使用 /ai help 查看说明。"
        if command.name == "help":
            answer = HELP
        elif command.name == "status":
            with self.permissions.storage._connection() as c:
                user = c.execute('SELECT reply_quota,request_count FROM principals WHERE id=?', (current.principal_id,)).fetchone()
            quota = "不限" if user['reply_quota'] is None else str(user['reply_quota'])
            answer = f"AI 已启用；剩余调用次数：{quota}；已接收 AI 请求：{user['request_count']}；上下文来源：SQLite。"
        elif command.name == "role":
            answer = "当前角色：" + self.roles.resolve(current.chat_id, current.principal_id)["name"]
        elif command.name == "role_list":
            answer = "可选角色：" + "、".join(r["name"] for r in self.roles.list() if r["enabled"] and r["user_selectable"])
        elif command.name == "role_use":
            role = next((r for r in self.roles.list() if r["name"] == command.argument), None)
            if not role:
                return "角色不存在，请用 /ai role list 查看。"
            target = current.principal_id
            try:
                self.roles.bind(role["id"], current.chat_id, target, current.as_actor(), self_select=True)
            except ValueError:
                return "无权选择此角色。"
            answer = "已切换角色：" + role["name"]
        elif command.name == "reset":
            from contexts import Contexts
            contexts = Contexts(self.permissions.storage)
            scope = contexts.resolve(current.chat_id,current.principal_id)
            try:
                contexts.clear(scope['id'],current.as_actor())
            except ValueError:
                return "共享上下文或当前有未完成任务，请在后台处理。"
            answer = "已清空你在当前会话中的独立上下文。"
        else:
            return None
        self.permissions.audit_command(current, command.name, "executed")
        return answer
