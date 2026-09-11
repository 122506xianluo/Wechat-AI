"""Exact, low-risk WeChat commands; never parse natural language as authority."""
from __future__ import annotations

from dataclasses import dataclass
import re

from permissions import AccessDecision, Permissions

HELP = "可用命令：/ai help、/ai status、/ai reset。群内 status 仅管理员可用；群成员独立清空将在步骤 7 开放。"


@dataclass(frozen=True)
class Command:
    name: str
    valid: bool = True


def parse_command(text: str, bot_names: list[str] | None = None) -> Command | None:
    text = text.strip()
    for name in bot_names or []:
        text = re.sub(r"^@" + re.escape(name) + r"(?:[\s\u2005]+|$)", "", text).strip()
    if text == "/ai":
        return Command("help")
    # Reserve only known subcommands. '/ai 普通提问' remains a normal prefix
    # trigger; extra arguments or multiline command text never perform a reset.
    parts = text.split()
    if len(parts) >= 2 and parts[0] == "/ai" and parts[1] in ("help", "status", "reset"):
        return Command(parts[1], len(parts) == 2 and "\n" not in text and "\r" not in text)
    return None


class Commands:
    def __init__(self, permissions: Permissions):
        self.permissions = permissions

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
            if kind == "group" and current.access_level != "admin":
                self.permissions.audit_command(current, command.name, "denied")
                return None
            answer = f"当前权限：{current.access_level}；本会话已启用；上下文来源：SQLite。"
        elif command.name == "reset":
            if kind != "private":
                self.permissions.audit_command(current, command.name, "group_scope_not_available")
                return "当前版本不通过微信清空群共享历史，避免影响其他成员。请在本机后台管理；成员独立清空将在步骤 7 开放。"
            self.permissions.reset_private_context(current)
            return "已清空你在本私聊中的上下文，其他会话不受影响。"
        else:
            return None
        self.permissions.audit_command(current, command.name, "executed")
        return answer
