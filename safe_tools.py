"""Small, read-only, policy checked tool registry. No eval, shell or generic I/O."""

import ast
from datetime import datetime, timezone
from hashlib import sha256
import json
import math
import operator
import time
from permissions import Permissions
from roles import Roles, require_manager

TOOL_PROPERTIES = {
    "get_current_time": ({}, []),
    "calculate": ({"expression": {"type": "string", "maxLength": 256}}, ["expression"]),
    "search_knowledge": ({"query": {"type": "string", "maxLength": 2000}}, ["query"]),
    "get_current_conversation_info": ({}, []),
}
DESCRIPTIONS = {
    "get_current_time": "Read the current server local time and UTC time.",
    "calculate": "Evaluate a bounded arithmetic expression; arithmetic operators only.",
    "search_knowledge": "Search only knowledge bases explicitly granted to this conversation and role.",
    "get_current_conversation_info": "Read metadata and counts for this conversation scope, never other chats or message text.",
}


def calculate(expression):
    if not isinstance(expression, str) or not 1 <= len(expression) <= 256:
        raise ValueError("expression_length")
    tree = ast.parse(expression, mode="eval")
    if sum(1 for _ in ast.walk(tree)) > 64:
        raise ValueError("expression_complexity")
    binary = {
        ast.Add: operator.add,
        ast.Sub: operator.sub,
        ast.Mult: operator.mul,
        ast.Div: operator.truediv,
        ast.FloorDiv: operator.floordiv,
        ast.Mod: operator.mod,
        ast.Pow: operator.pow,
    }

    def visit(node, depth=0):
        if depth > 12:
            raise ValueError("expression_depth")
        if isinstance(node, ast.Expression):
            return visit(node.body, depth + 1)
        if isinstance(node, ast.Constant) and type(node.value) in (int, float):
            result = node.value
        elif isinstance(node, ast.UnaryOp) and type(node.op) in (ast.UAdd, ast.USub):
            result = visit(node.operand, depth + 1) * (
                1 if isinstance(node.op, ast.UAdd) else -1
            )
        elif isinstance(node, ast.BinOp) and type(node.op) in binary:
            left, right = visit(node.left, depth + 1), visit(node.right, depth + 1)
            if isinstance(node.op, ast.Pow) and (abs(right) > 10 or abs(left) > 1e10):
                raise ValueError("power_limit")
            result = binary[type(node.op)](left, right)
        else:
            raise ValueError("operator_not_allowed")
        if (
            type(result) not in (int, float)
            or abs(result) > 1e100
            or not math.isfinite(result)
        ):
            raise ValueError("numeric_limit")
        return result

    return visit(tree)


class Tools:
    def __init__(self, storage, knowledge):
        self.storage, self.knowledge = storage, knowledge

    def list(self):
        with self.storage._connection() as c:
            grants = [dict(r) for r in c.execute("SELECT * FROM role_tools")]
        return [
            {
                "name": name,
                "description": DESCRIPTIONS[name],
                "roles": [
                    r["role_id"]
                    for r in grants
                    if r["tool_name"] == name and r["enabled"]
                ],
            }
            for name in TOOL_PROPERTIES
        ]

    def configure(self, role_id, name, enabled, actor):
        if name not in TOOL_PROPERTIES or type(enabled) is not bool:
            raise ValueError("工具名或开关无效")
        from audit import record_audit

        with self.storage.transaction() as c:
            require_manager(c, actor)
            if not c.execute("SELECT 1 FROM roles WHERE id=?", (role_id,)).fetchone():
                raise ValueError("角色不存在")
            c.execute(
                "INSERT INTO role_tools(role_id,tool_name,enabled) VALUES(?,?,?) ON CONFLICT(role_id,tool_name) DO UPDATE SET enabled=excluded.enabled",
                (role_id, name, int(enabled)),
            )
            record_audit(
                c,
                "tool.configure",
                source=actor.source,
                actor_id=actor.principal_id,
                target_type="role",
                target_id=role_id,
                details={"tool": name, "enabled": enabled},
            )

    def schemas(self, job):
        if (
            not Permissions(self.storage)
            .resolve(job["principal_id"], job["chat_id"])
            .allowed
        ):
            return []
        role = Roles(self.storage).resolve(job["chat_id"], job["principal_id"])
        with self.storage._connection() as c:
            names = [
                r[0]
                for r in c.execute(
                    "SELECT tool_name FROM role_tools WHERE role_id=? AND enabled=1",
                    (role["id"],),
                )
            ]
        if role.get("knowledge_mode") == "off" or not self.knowledge.allowed(
            job["chat_id"], job["principal_id"], role["id"]
        ):
            names = [name for name in names if name != "search_knowledge"]
        return [
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": DESCRIPTIONS[name],
                    "parameters": {
                        "type": "object",
                        "properties": TOOL_PROPERTIES[name][0],
                        "required": TOOL_PROPERTIES[name][1],
                        "additionalProperties": False,
                    },
                },
            }
            for name in names
            if name in TOOL_PROPERTIES
        ]

    def execute(self, name, arguments, job):
        start = time.monotonic()
        status = "ok"
        if not isinstance(arguments, str):
            arguments = ""
        summary = json.dumps(
            {
                "bytes": len(arguments.encode()),
                "sha256": sha256(arguments.encode()).hexdigest(),
            }
        )
        try:
            if len(arguments.encode()) > 4096:
                raise ValueError("tool_arguments_over_4KiB")
            names = {t["function"]["name"] for t in self.schemas(job)}
            if name not in names:
                raise PermissionError("tool_not_authorized")
            args = json.loads(arguments)
            properties, required = TOOL_PROPERTIES[name]
            if (
                not isinstance(args, dict)
                or set(args) - set(properties)
                or set(required) - set(args)
            ):
                raise ValueError("invalid_arguments")
            if name == "calculate":
                result = {"value": calculate(args["expression"])}
            elif name == "get_current_time":
                result = {
                    "local": datetime.now().astimezone().isoformat(),
                    "utc": datetime.now(timezone.utc).isoformat(),
                }
            elif name == "search_knowledge":
                query = args["query"]
                if not isinstance(query, str) or len(query) > 2000:
                    raise ValueError("query_length")
                role = Roles(self.storage).resolve(job["chat_id"], job["principal_id"])
                # No network inside tools; bounded local FTS avoids unkillable HTTP threads.
                result = {
                    "untrusted_results": self.knowledge.search(
                        query,
                        job["chat_id"],
                        job["principal_id"],
                        role["id"],
                        remote=False,
                    )
                }
            else:
                with self.storage._connection() as c:
                    c.execute("PRAGMA busy_timeout=200")
                    scope = c.execute(
                        "SELECT mode FROM conversation_scopes WHERE id=? AND chat_id=? AND (principal_id=? OR principal_id IS NULL)",
                        (job["scope_id"], job["chat_id"], job["principal_id"]),
                    ).fetchone()
                    if not scope:
                        raise PermissionError("scope_not_authorized")
                    count = c.execute(
                        "SELECT COUNT(*) FROM messages WHERE scope_id=? AND status IN ('sent','received')",
                        (job["scope_id"],),
                    ).fetchone()[0]
                role = Roles(self.storage).resolve(job["chat_id"], job["principal_id"])
                result = {
                    "scope_id": job["scope_id"],
                    "mode": scope[0],
                    "role": role["name"],
                    "saved_message_count": count,
                }
            encoded = json.dumps(result, ensure_ascii=False)
            if time.monotonic() - start > 5:
                raise TimeoutError("tool_timeout")
            if len(encoded.encode()) > 8192:
                if name == "search_knowledge":
                    while (
                        result["untrusted_results"]
                        and len(json.dumps(result, ensure_ascii=False).encode()) > 8192
                    ):
                        result["untrusted_results"].pop()
                    encoded = json.dumps(result, ensure_ascii=False)
                else:
                    raise ValueError("tool_result_over_8KiB")
            return encoded
        except Exception as exc:
            status = type(exc).__name__
            return json.dumps({"error": status})
        finally:
            with self.storage.transaction() as c:
                c.execute(
                    "INSERT INTO tool_runs(job_id,principal_id,scope_id,tool_name,argument_summary,status,duration_ms) VALUES(?,?,?,?,?,?,?)",
                    (
                        job["id"],
                        job["principal_id"],
                        job["scope_id"],
                        str(name)[:100],
                        summary,
                        status,
                        int((time.monotonic() - start) * 1000),
                    ),
                )

    def runs(self):
        with self.storage._connection() as c:
            return [
                dict(r)
                for r in c.execute("SELECT * FROM tool_runs ORDER BY id DESC LIMIT 300")
            ]
