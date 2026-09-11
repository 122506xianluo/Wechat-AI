import subprocess

from tools.check_tracked_files import forbidden


def test_runtime_ignore_rules():
    private = [".env", ".env.local", "config.json", "data/backups/pre-v2-test.db", "data/bot.log",
               "data/wechat_ai.db-wal", "data/attachments/a.png", "data/knowledge/test.pdf", "STOP", ".coverage"]
    result = subprocess.run(["git", "check-ignore", "--stdin", "-z"], input=("\0".join(private)+"\0").encode(),
                            capture_output=True, check=True)
    assert set(result.stdout.decode().strip("\0").split("\0")) == set(private)
    for name in private[:-1]:
        assert forbidden(name)
    for name in ["tests/test_permissions.py", ".env.example", "data/.gitkeep", "requirements-dev.txt"]:
        assert not forbidden(name)
