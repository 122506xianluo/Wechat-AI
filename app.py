from __future__ import annotations

import atexit
import json
import logging
import os
from dataclasses import asdict, fields
from pathlib import Path
import socket
import subprocess
import sys
import threading
import time
import traceback
import webbrowser

import httpx
from flask import Flask, jsonify, render_template, request

from bot import Config, LLMSettings
from storage import Storage

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
BOT_PID = DATA / "bot.pid"
PANEL_PID = DATA / "panel.pid"
STOP_FILE = ROOT / "STOP"
ENV_FILE = ROOT / ".env"
CONFIG_FILE = ROOT / "config.json"
EXAMPLE_CONFIG = ROOT / "config.example.json"
LOG_FILE = DATA / "bot.log"
PANEL_LOG = DATA / "panel.log"
SETUP_LOG = DATA / "setup.log"
DB_FILE = DATA / "wechat_ai.db"
HOST = "127.0.0.1"
PORT = 18787
URL = f"http://{HOST}:{PORT}"
PANEL_URL_FILE = DATA / "panel.url"
PREFERRED_PORTS = (18787, 17878, 18080, 5000, 5173, 8088)
CREATE_NO_WINDOW = 0x08000000
DETACHED = 0x00000008 | 0x00000200 | CREATE_NO_WINDOW

app = Flask(__name__)
app.json.ensure_ascii = False
_storage = None
_storage_error = None


def get_storage() -> Storage:
    global _storage, _storage_error
    if _storage is None:
        try:
            _storage = Storage(ROOT)
            _storage_error = None
        except Exception as exc:
            _storage_error = str(exc)
            raise
    return _storage


def storage_state() -> dict:
    try:
        return get_storage().stats()
    except Exception as exc:
        return {"ok": False, "path": str(DB_FILE), "error": str(exc)}


def venv_python(windowed: bool = False) -> Path:
    name = "pythonw.exe" if windowed else "python.exe"
    path = ROOT / ".venv" / "Scripts" / name
    if not path.exists():
        raise RuntimeError("请先双击 panel.bat 完成初始化")
    return path


def pid_running(pid: int) -> bool:
    if pid <= 0:
        return False
    result = subprocess.run(
        ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
        capture_output=True, text=True, encoding="utf-8", errors="ignore",
        creationflags=CREATE_NO_WINDOW)
    return str(pid) in result.stdout


def read_pid(path: Path):
    try:
        pid = int(path.read_text(encoding="ascii").strip())
    except (OSError, ValueError):
        return None
    if pid_running(pid):
        return pid
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass
    return None


def bot_running() -> bool:
    return read_pid(BOT_PID) is not None


def load_env_map():
    values = {"LLM_BASE_URL": "", "LLM_API_KEY": "", "LLM_MODEL": ""}
    if not ENV_FILE.exists():
        return values
    for line in ENV_FILE.read_text(encoding="utf-8-sig").splitlines():
        if not line.strip() or line.lstrip().startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()
    return values


def write_env(base_url: str, api_key: str, model: str):
    current = load_env_map()
    current["LLM_BASE_URL"] = base_url.strip()
    current["LLM_MODEL"] = model.strip()
    if api_key.strip():
        current["LLM_API_KEY"] = api_key.strip()
    ENV_FILE.write_text(
        "# OpenAI-compatible Chat Completions endpoint.\n"
        f"LLM_BASE_URL={current.get('LLM_BASE_URL', '')}\n"
        f"LLM_API_KEY={current.get('LLM_API_KEY', '')}\n"
        f"LLM_MODEL={current.get('LLM_MODEL', '')}\n",
        encoding="utf-8")


def mask_secret(value: str):
    if not value:
        return ""
    if len(value) <= 4:
        return "*" * len(value)
    return "*" * (len(value) - 4) + value[-4:]


def fail(message: str, code: int = 400):
    return jsonify({"ok": False, "error": message}), code


def as_lines(value):
    if isinstance(value, str):
        return [line.strip() for line in value.splitlines() if line.strip()]
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    raise ValueError("列表配置格式不正确")


def config_from_payload(data: dict) -> Config:
    raw = {}
    defaults = asdict(Config())
    for name in (item.name for item in fields(Config)):
        raw[name] = data[name] if name in data else defaults[name]
    for key in ("private_chats", "groups", "bot_names"):
        raw[key] = as_lines(raw[key])
    raw["group_mode"] = str(raw["group_mode"]).strip()
    raw["group_prefix"] = str(raw["group_prefix"])
    raw["system_prompt"] = str(raw["system_prompt"])
    raw["poll_seconds"] = float(raw["poll_seconds"])
    raw["timeout_seconds"] = float(raw["timeout_seconds"])
    for key in ("context_turns", "max_input_chars", "max_reply_chars", "max_tokens"):
        raw[key] = int(raw[key])
    cfg = Config(**raw)
    cfg.validate()
    return cfg


def save_config(cfg: Config):
    CONFIG_FILE.write_text(
        json.dumps(asdict(cfg), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8")


def read_config_state():
    path = CONFIG_FILE if CONFIG_FILE.exists() else EXAMPLE_CONFIG
    if not path.exists():
        return asdict(Config()), "缺少 config.json"
    try:
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(raw, dict):
            raise ValueError("config.json 必须是 JSON 对象")
        cfg = config_from_payload(raw)
        return asdict(cfg), None
    except Exception as exc:
        try:
            raw = json.loads(path.read_text(encoding="utf-8-sig"))
            if isinstance(raw, dict):
                merged = asdict(Config())
                merged.update({k: v for k, v in raw.items() if k in merged})
                return merged, str(exc)
        except Exception:
            pass
        return asdict(Config()), str(exc)


def settings_from_payload(data: dict) -> LLMSettings:
    env = load_env_map()
    base_url = str(data.get("llm_base_url", env.get("LLM_BASE_URL", ""))).strip()
    model = str(data.get("llm_model", env.get("LLM_MODEL", ""))).strip()
    api_key = str(data.get("llm_api_key", "")).strip()
    if not api_key:
        api_key = env.get("LLM_API_KEY", "")
    settings = LLMSettings(base_url, api_key, model)
    settings.validate()
    return settings


def tail_file(path: Path, max_lines: int = 160) -> str:
    if not path.exists():
        return ""
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    return "\n".join(lines[-max_lines:])


def tail_log(max_lines: int = 160) -> str:
    return tail_file(LOG_FILE, max_lines)


def setup_log(max_lines: int = 160) -> str:
    return tail_file(SETUP_LOG, max_lines)


def panel_log(max_lines: int = 160) -> str:
    return tail_file(PANEL_LOG, max_lines)


def run_check():
    result = subprocess.run(
        [str(venv_python(False)), str(ROOT / "bot.py"), "--check"],
        cwd=str(ROOT), capture_output=True, text=True,
        encoding="utf-8", errors="replace", creationflags=CREATE_NO_WINDOW)
    output = (result.stderr or result.stdout or "").strip()
    return result.returncode == 0, output


@app.get("/")
def index():
    return render_template("index.html")


@app.get("/api/state")
def api_state():
    cfg, cfg_error = read_config_state()
    env = load_env_map()
    return jsonify({
        "ok": True,
        "running": bot_running(),
        "pid": read_pid(BOT_PID),
        "stop_requested": STOP_FILE.exists(),
        "config": cfg,
        "config_error": cfg_error,
        "llm_base_url": env.get("LLM_BASE_URL", ""),
        "llm_model": env.get("LLM_MODEL", ""),
        "llm_api_key_set": bool(env.get("LLM_API_KEY", "")),
        "llm_api_key_masked": mask_secret(env.get("LLM_API_KEY", "")),
        "log": tail_log(),
        "setup_log": setup_log(),
        "panel_log": panel_log(),
        "url": URL,
        "storage": storage_state(),
    })


@app.get("/api/log")
def api_log():
    return jsonify({
        "ok": True,
        "running": bot_running(),
        "pid": read_pid(BOT_PID),
        "stop_requested": STOP_FILE.exists(),
        "log": tail_log(),
        "setup_log": setup_log(),
        "panel_log": panel_log(),
        "storage": storage_state(),
    })


@app.get("/api/storage/chats")
def api_storage_chats():
    try:
        storage = get_storage()
        return jsonify({"ok": True, "chats": storage.list_chats(), "stats": storage.stats()})
    except Exception as exc:
        return fail(str(exc), 500)


@app.post("/api/storage/clear-chat")
def api_storage_clear_chat():
    if bot_running():
        return fail("请先停止机器人，再清空会话上下文")
    data = request.get_json(silent=True) or {}
    kind = str(data.get("kind", "")).strip()
    name = str(data.get("name", "")).strip()
    if kind not in ("private", "group") or not name:
        return fail("请选择有效的好友或群聊")
    try:
        deleted = get_storage().clear_chat(kind, name)
        return jsonify({"ok": True, "message": f"已清空 {deleted} 条记录", "storage": storage_state()})
    except Exception as exc:
        return fail(str(exc), 500)


@app.post("/api/storage/clear-all")
def api_storage_clear_all():
    if bot_running():
        return fail("请先停止机器人，再清空会话上下文")
    try:
        deleted = get_storage().clear_all_history()
        return jsonify({"ok": True, "message": f"已清空 {deleted} 条记录", "storage": storage_state()})
    except Exception as exc:
        return fail(str(exc), 500)


@app.post("/api/save")
def api_save():
    if bot_running():
        return fail("请先停止，再保存配置")
    data = request.get_json(silent=True) or {}
    try:
        cfg = config_from_payload(data)
        settings = settings_from_payload(data)
        write_env(settings.base_url, str(data.get("llm_api_key", "")).strip(), settings.model)
        save_config(cfg)
        ok, output = run_check()
        if not ok:
            return fail(output or "配置检查未通过")
        return jsonify({"ok": True, "message": "已保存"})
    except Exception as exc:
        return fail(str(exc))


@app.post("/api/test")
def api_test():
    data = request.get_json(silent=True) or {}
    try:
        settings = settings_from_payload(data)
        headers = {"Content-Type": "application/json"}
        if settings.api_key:
            headers["Authorization"] = f"Bearer {settings.api_key}"
        with httpx.Client(timeout=20.0) as client:
            response = client.post(
                settings.endpoint, headers=headers,
                json={
                    "model": settings.model,
                    "messages": [{"role": "user", "content": "只回复ok"}],
                    "max_tokens": 16,
                    "temperature": 0,
                })
        if response.status_code >= 400:
            return fail("模型接口 HTTP %s: %s" % (response.status_code, response.text[:300]))
        payload = response.json()
        text = (((payload.get("choices") or [{}])[0].get("message") or {}).get("content") or "").strip()
        return jsonify({"ok": True, "message": "模型可用", "reply": text[:200] or "(空回复)"})
    except Exception as exc:
        return fail(str(exc))


@app.post("/api/start")
def api_start():
    if bot_running():
        return fail("已经在运行")
    data = request.get_json(silent=True) or {}
    try:
        cfg = config_from_payload(data)
        settings = settings_from_payload(data)
        write_env(settings.base_url, str(data.get("llm_api_key", "")).strip(), settings.model)
        save_config(cfg)
    except Exception as exc:
        return fail(str(exc))
    try:
        STOP_FILE.unlink(missing_ok=True)
    except OSError:
        pass
    ok, output = run_check()
    if not ok:
        return fail(output or "配置检查未通过")
    subprocess.Popen(
        [str(venv_python(True)), str(ROOT / "bot.py")],
        cwd=str(ROOT), close_fds=True, creationflags=DETACHED,
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        if bot_running():
            return jsonify({
                "ok": True,
                "message": "已启动",
                "pid": read_pid(BOT_PID),
                "log": tail_log(),
            })
        time.sleep(0.1)
    hint = tail_log() or output or "启动失败，请看日志"
    return fail(hint[-500:])


@app.post("/api/stop")
def api_stop():
    if not bot_running():
        try:
            STOP_FILE.unlink(missing_ok=True)
        except OSError:
            pass
        return jsonify({"ok": True, "message": "当前未运行"})
    STOP_FILE.write_text("stop\n", encoding="ascii")
    deadline = time.monotonic() + 20.0
    while time.monotonic() < deadline:
        if not bot_running():
            return jsonify({"ok": True, "message": "已停止", "log": tail_log()})
        time.sleep(0.2)
    return jsonify({
        "ok": True,
        "message": "已请求停止，可能在等当前回复结束",
        "log": tail_log(),
    })


def set_bind(port: int):
    global PORT, URL
    PORT = port
    URL = f"http://{HOST}:{PORT}"


def port_free(port: int) -> bool:
    sock = socket.socket()
    try:
        sock.bind((HOST, port))
        return True
    except OSError:
        return False
    finally:
        sock.close()


def pick_port() -> int:
    seen = []
    for port in list(PREFERRED_PORTS) + list(range(18788, 18850)):
        if port in seen:
            continue
        seen.append(port)
        if port_free(port):
            return port
    raise RuntimeError("找不到可用的本机端口")


def port_open(url: str | None = None) -> bool:
    target = url or URL
    try:
        host, port_s = target.rsplit("://", 1)[-1].rsplit(":", 1)
        with socket.create_connection((host, int(port_s)), timeout=0.3):
            return True
    except OSError:
        return False


def live_panel_url():
    if not PANEL_URL_FILE.exists():
        return None
    url = PANEL_URL_FILE.read_text(encoding="ascii", errors="ignore").strip()
    if url.startswith("http://127.0.0.1:") and port_open(url):
        return url
    return None


def wait_and_open():
    for _ in range(80):
        if port_open(URL):
            webbrowser.open(URL)
            return
        time.sleep(0.1)


def dump_error(exc=None):
    DATA.mkdir(exist_ok=True)
    text_err = traceback.format_exc() if exc is None else "".join(
        traceback.format_exception(type(exc), exc, exc.__traceback__))
    (DATA / "panel_error.log").write_text(text_err, encoding="utf-8")
    try:
        print(text_err)
    except Exception:
        pass


def main() -> int:
    DATA.mkdir(exist_ok=True)
    if sys.stdout is None:
        sys.stdout = open(PANEL_LOG, "a", encoding="utf-8", buffering=1)
    if sys.stderr is None:
        sys.stderr = sys.stdout
    logging.basicConfig(
        filename=PANEL_LOG, level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s", encoding="utf-8")
    existing = live_panel_url()
    if existing:
        set_bind(int(existing.rsplit(":", 1)[-1]))
        print("控制台已在运行：" + existing)
        webbrowser.open(existing)
        return 0
    set_bind(pick_port())
    PANEL_PID.write_text(str(os.getpid()) + "\n", encoding="ascii")
    PANEL_URL_FILE.write_text(URL + "\n", encoding="ascii")

    def cleanup():
        try:
            if PANEL_PID.exists() and PANEL_PID.read_text(encoding="ascii").strip() == str(os.getpid()):
                PANEL_PID.unlink()
            if PANEL_URL_FILE.exists() and PANEL_URL_FILE.read_text(encoding="ascii").strip() == URL:
                PANEL_URL_FILE.unlink()
        except OSError:
            pass

    atexit.register(cleanup)
    print("控制台：" + URL)
    print("关闭本窗口会停止控制台，不会自动停止微信回复。")
    threading.Thread(target=wait_and_open, daemon=True).start()
    app.run(host=HOST, port=PORT, debug=False, use_reloader=False, threaded=True)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception as exc:
        dump_error(exc)
        try:
            input("启动失败，按回车关闭")
        except Exception:
            pass
        raise
