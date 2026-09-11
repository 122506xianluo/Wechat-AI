"""Fail CI/pre-commit checks if runtime/private artifacts entered Git's index."""
from __future__ import annotations

from pathlib import PurePosixPath
import subprocess
import sys


def forbidden(name: str) -> bool:
    path = PurePosixPath(name)
    base = path.name.lower()
    if name in (".env.example", "data/.gitkeep"):
        return False
    return (name.startswith(("data/", ".venv/", ".cache/"))
            or name == "config.json" or base == "stop"
            or base == ".env" or base.startswith(".env.")
            or base.endswith((".db", ".db-wal", ".db-shm", ".sqlite", ".sqlite3", ".log", ".pyc"))
            or "__pycache__" in path.parts)


def main() -> int:
    result = subprocess.run(["git", "ls-files", "-z"], check=True, capture_output=True)
    bad = [name for name in result.stdout.decode("utf-8").split("\0") if name and forbidden(name)]
    if bad:
        print("ERROR: private/runtime paths in index:", *bad, sep="\n")
        return 1
    print("Tracked-file safety check passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
