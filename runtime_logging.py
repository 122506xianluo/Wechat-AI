"""Chinese application logs and bounded, read-only console snapshots (stdlib only)."""

from __future__ import annotations

import copy
from datetime import datetime
import logging
import os
from pathlib import Path
import re
import sys
import threading

LEVELS = {
    "DEBUG": "调试",
    "INFO": "信息",
    "WARNING": "警告",
    "ERROR": "错误",
    "CRITICAL": "严重错误",
}
SOURCES = {"bot": "机器人", "setup": "安装", "panel": "控制台"}
STAMP = re.compile(r"^(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2}:\d{2})(?:[.,](\d{1,6}))?")
ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
SECRET_NAME = re.compile(r"(?:KEY|TOKEN|SECRET|PASSWORD)", re.I)
REASONS = {
    "profile_first_text_missing": "资料卡第一项昵称控件未出现",
    "profile_first_text_error": "资料卡第一项昵称读取失败",
    "profile_first_text_empty": "资料卡第一项昵称为空",
    "wrong_chat": "用户不属于当前聊天",
    "invalid_owner": "身份不具备此管理权限",
    "sender_unknown": "未读到发送者昵称",
    "sender_invalid": "发送者昵称无效",
    "sender_ambiguous": "发送者身份有歧义",
    "duplicate_nickname": "群内昵称重名",
    "identity_pending": "身份待确认",
    "pending": "身份待确认",
    "ambiguous": "昵称存在冲突",
    "renamed": "改名待确认",
    "merged": "身份已合并",
    "disabled": "用户已停用",
    "blocked": "用户已被禁止使用",
    "allowed": "允许使用",
    "active": "身份正常",
    "chat_unregistered_or_disabled": "聊天未登记或未启用",
    "chat_disabled": "聊天未启用",
    "not_incoming": "不是收到的消息",
    "reply_quota_exhausted": "可用调用次数已用完",
    "duplicate_or_access_changed": "消息已处理或使用权限已变更",
    "title_unverified": "聊天标题尚未核验",
    "input_unavailable": "输入框暂时不可用",
    "wechat_list_unavailable": "微信列表暂时不可读",
    "activation_failed": "切换聊天未完成",
    "permission_revoked": "使用权限已撤销",
    "context_changed": "上下文已被修改或清空",
    "role_or_knowledge_access_changed": "角色或知识库授权已变更",
    "profile_not_opened": "资料卡未打开",
    "profile_name_missing": "资料卡没有可读昵称",
    "profile_read_error": "资料卡读取失败",
    "profile_still_open": "资料卡未能关闭",
    "chat_changed_before_profile": "读取资料卡前聊天已变化",
    "chat_changed_after_profile": "读取资料卡后聊天已变化",
    "chat_unverified_after_profile": "关闭资料卡后聊天尚未核验",
    "row_geometry_unavailable": "消息行位置不可读",
    "row_too_small": "消息行不可见或尺寸不足",
}


def reason_text(value) -> str:
    value = str(value or "")
    if value.startswith("identity_") and value[9:] in REASONS:
        return REASONS[value[9:]]
    return REASONS.get(value, "未分类原因（" + value[:100] + "）")


def secret_values(root: Path) -> tuple[str, ...]:
    values = {v for k, v in os.environ.items() if SECRET_NAME.search(k) and v}
    try:
        for line in (root / ".env").read_text(encoding="utf-8-sig").splitlines():
            key, sep, value = line.partition("=")
            if sep and not key.lstrip().startswith("#") and SECRET_NAME.search(key):
                value = value.strip().strip("\"'")
                if value:
                    values.add(value)
    except OSError:
        pass
    return tuple(sorted(values, key=len, reverse=True))


def redact(text: str, secrets=()) -> str:
    text = ANSI.sub("", text).replace("\x00", "")
    for value in secrets:
        if value:
            text = text.replace(value, "[已隐藏]")
    text = re.sub(r"(?i)\bBearer\s+[^\s,;\"']+", "Bearer [已隐藏]", text)
    text = re.sub(
        r"(?i)((?:[\w-]*api[_-]?key|access[_-]?token|authorization|secret|password)[\"']?\s*[:=]\s*)[\"']?[^\s,;&\"']+",
        r"\1[已隐藏]",
        text,
    )
    return re.sub(r"\bsk-[A-Za-z0-9_-]{8,}\b", "[已隐藏]", text)


class ChineseFormatter(logging.Formatter):
    def __init__(self, root: Path):
        super().__init__("%(asctime)s %(levelname)s %(message)s")
        self.root = root
        self.secrets = secret_values(root)
        self.env_stamp = None

    def format(self, record):
        try:
            stamp = (self.root / ".env").stat().st_mtime_ns
        except OSError:
            stamp = 0
        if stamp != self.env_stamp:
            self.secrets = secret_values(self.root)
            self.env_stamp = stamp
        local = copy.copy(record)
        local.levelname = LEVELS.get(record.levelname, record.levelname)
        return redact(super().format(local), self.secrets)


class LoggedStream:
    """Capture third-party print/traceback output without re-capturing logging."""

    encoding = "utf-8"
    errors = "replace"

    def __init__(self, level):
        self.level = level
        self.buffer = ""
        self.lock = threading.RLock()
        self.local = threading.local()

    def _emit(self, line):
        # If logging itself fails (e.g. disk full), never recurse through stderr.
        if getattr(self.local, "emitting", False):
            return
        self.local.emitting = True
        try:
            logging.getLogger("console.output").log(self.level, "%s", line)
        finally:
            self.local.emitting = False

    def write(self, text):
        lines = []
        with self.lock:
            self.buffer += str(text).replace("\r", "\n")
            while "\n" in self.buffer or len(self.buffer) > 8192:
                if "\n" in self.buffer:
                    line, self.buffer = self.buffer.split("\n", 1)
                else:
                    line, self.buffer = self.buffer[:8192], self.buffer[8192:]
                if line.strip():
                    lines.append(line)
        # Do not hold the stream lock while acquiring a logging handler lock.
        for line in lines:
            if line.lstrip().startswith("* Serving Flask app"):
                line = "Web 服务初始化完成"
            elif line.strip() == "* Debug mode: off":
                line = "Web 调试模式：关闭"
            self._emit(line)
        return len(text)

    def flush(self):
        with self.lock:
            line, self.buffer = self.buffer, ""
        if line:
            self._emit(line)

    def isatty(self):
        return False


def configure_logging(
    root: Path, filename: str, *, stdio_only=False, capture_stdio=False
):
    (root / "data").mkdir(exist_ok=True)
    logger = logging.getLogger()
    for handler in logger.handlers[:]:
        logger.removeHandler(handler)
        handler.close()
    logger.setLevel(logging.INFO)
    formatter = ChineseFormatter(root)
    if not stdio_only:
        handler = logging.FileHandler(root / "data" / filename, encoding="utf-8")
        handler.setFormatter(formatter)
        logger.addHandler(handler)
    # Keep the original streams: captured print() goes through the logger once.
    stream = sys.stderr
    if isinstance(stream, LoggedStream):
        stream = sys.__stderr__
    if stream is not None:
        handler = logging.StreamHandler(stream)
        handler.setFormatter(formatter)
        logger.addHandler(handler)
    if capture_stdio:
        sys.stdout = LoggedStream(logging.INFO)
        sys.stderr = LoggedStream(logging.ERROR)
    for name in ("httpx", "httpcore", "werkzeug"):
        logging.getLogger(name).setLevel(logging.WARNING)


def tail_file(path: Path, max_lines=500, max_bytes=96 * 1024) -> str:
    """Seek to a bounded suffix; never read an ever-growing log in full."""
    if max_lines <= 0 or max_bytes <= 0:
        return ""
    try:
        with path.open("rb") as handle:
            size = handle.seek(0, 2)
            start = max(0, size - max_bytes)
            handle.seek(max(0, start - 1))
            data = handle.read(max_bytes + (1 if start else 0))
        if start:
            if data[:1] == b"\n":
                data = data[1:]
            else:
                # Skip a partial UTF-8 line, not just a partial code point.
                data = data.partition(b"\n")[2]
        return "\n".join(
            data.decode("utf-8", errors="replace").splitlines()[-max_lines:]
        )
    except FileNotFoundError:
        return ""
    except OSError:
        return "警告 日志文件暂时无法读取，稍后重试"


def line_level(text, default="info"):
    if re.search(r"\b(ERROR|CRITICAL|fatal|Traceback)\b|错误|失败|异常", text, re.I):
        return "error"
    if re.search(r"\b(WARNING|WARN)\b|警告|待人工确认|结果不明", text, re.I):
        return "warn"
    return default


def merge_console(
    snapshots: dict[str, str], paths: dict[str, Path], *, secrets=(), limit=500
):
    """Sort timestamped blocks, keeping multiline exceptions beside their event."""
    blocks = []
    for source, text in snapshots.items():
        try:
            fallback = datetime.fromtimestamp(paths[source].stat().st_mtime).strftime(
                "%Y-%m-%d %H:%M:%S.%f"
            )
        except OSError:
            fallback = "0000-00-00 00:00:00.000000"
        lines = redact(text, secrets).splitlines()
        first = next((STAMP.match(line) for line in lines if STAMP.match(line)), None)
        stamp = (
            (first[1] + " " + first[2] + "." + (first[3] or "").ljust(6, "0"))
            if first
            else fallback
        )
        block = None
        for line in lines:
            match = STAMP.match(line)
            if match:
                stamp = match[1] + " " + match[2] + "." + (match[3] or "").ljust(6, "0")
                block = None
                # Display old level names in Chinese too, without rewriting files.
                line = re.sub(
                    r"^(\d{4}-\d{2}-\d{2}[ T]\S+\s+)(DEBUG|INFO|WARNING|ERROR|CRITICAL)\b",
                    lambda m: m[1] + LEVELS[m[2]],
                    line,
                )
            if block is None:
                block = {"stamp": stamp, "rows": [], "level": line_level(line)}
                blocks.append(block)
            block["rows"].append(
                {
                    "source": SOURCES.get(source, source),
                    "text": line[:8192],
                    "level": line_level(line, block["level"]),
                }
            )
    blocks.sort(key=lambda block: block["stamp"])
    return [row for block in blocks for row in block["rows"]][-limit:]
