from __future__ import annotations

import datetime as dt
import os
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
SETUP_LOG = DATA / "setup.log"
VENV_PYTHON = ROOT / ".venv" / "Scripts" / "python.exe"


def write_log(message: str):
    DATA.mkdir(exist_ok=True)
    line = f"{dt.datetime.now():%Y-%m-%d %H:%M:%S} {message}"
    with SETUP_LOG.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")
    print(line, flush=True)


def run(command: list[str], label: str) -> int:
    write_log(f"开始：{label}")
    write_log("命令：" + " ".join(command))
    try:
        process = subprocess.Popen(
            command,
            cwd=str(ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except OSError as exc:
        write_log(f"启动失败：{type(exc).__name__}: {exc}")
        return 1
    assert process.stdout is not None
    for line in process.stdout:
        text = line.rstrip()
        if text:
            write_log(text)
    code = process.wait()
    write_log(f"完成：{label}，退出码={code}")
    return code


def python_launcher() -> list[str] | None:
    if sys.version_info[:2] == (3, 10):
        return [sys.executable]
    py = shutil.which("py")
    if py:
        probe = subprocess.run(
            [py, "-3.10", "-c", "import sys; print(sys.executable)"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if probe.returncode == 0:
            return [py, "-3.10"]
    python = shutil.which("python")
    if python:
        probe = subprocess.run(
            [python, "-c", "import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 10) else 1)"],
            capture_output=True,
        )
        if probe.returncode == 0:
            return [python]
    return None


def ensure_file(source: Path, target: Path):
    if target.exists():
        return
    target.write_bytes(source.read_bytes())
    write_log(f"已创建：{target.name}")


def main() -> int:
    DATA.mkdir(exist_ok=True)
    write_log("========== 控制台启动 ==========")
    launcher = python_launcher()
    if launcher is None and not VENV_PYTHON.exists():
        write_log("错误：未找到 Python 3.10 x64，请先安装 Python 3.10.11")
        return 1

    if not VENV_PYTHON.exists():
        if run(launcher + ["-m", "venv", str(ROOT / ".venv")], "创建虚拟环境") != 0:
            return 1
    else:
        write_log("虚拟环境已存在，跳过创建")

    if run([str(VENV_PYTHON), "-m", "pip", "install", "-r", str(ROOT / "requirements.txt")], "安装或检查依赖") != 0:
        return 1

    ensure_file(ROOT / ".env.example", ROOT / ".env")
    ensure_file(ROOT / "config.example.json", ROOT / "config.json")
    write_log("安装准备完成，正在打开 Web 控制台")
    return subprocess.call([str(VENV_PYTHON), str(ROOT / "app.py")], cwd=str(ROOT))


if __name__ == "__main__":
    raise SystemExit(main())
