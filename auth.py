"""Local password login, scrypt, one-time recovery and persistent server sessions."""

import hashlib
import hmac
import secrets
import time
from permissions import Actor, _decision, PermissionDenied
from audit import record_audit
from roles import require_manager
from storage import utc_now


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def password_hash(password, salt=None):
    if not isinstance(password, str) or not 12 <= len(password) <= 1024:
        raise ValueError("密码必须为12至1024个字符")
    salt = salt or secrets.token_hex(16)
    hashed = hashlib.scrypt(
        password.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1, dklen=32
    ).hex()
    return salt + ":" + hashed


def verify(password, encoded):
    try:
        salt, _ = encoded.split(":")
        return hmac.compare_digest(password_hash(password, salt), encoded)
    except (ValueError, TypeError):
        return False


class Auth:
    def __init__(self, storage):
        self.storage = storage

    def initialized(self):
        with self.storage._connection() as c:
            return bool(c.execute("SELECT 1 FROM web_accounts LIMIT 1").fetchone())

    def create(self, username, password, actor=None):
        if (
            not isinstance(username, str)
            or not username.strip()
            or len(username) > 80
            or any(ch.isspace() for ch in username)
        ):
            raise ValueError("账户名必须为1至80个非空白字符")
        encoded = password_hash(password)
        recovery = secrets.token_urlsafe(32)
        with self.storage.transaction() as c:
            exists = bool(c.execute("SELECT 1 FROM web_accounts").fetchone())
            if exists:
                if actor is None:
                    raise PermissionDenied("owner 已初始化")
                require_manager(c, actor, owner=True)
            level = "admin" if exists else "owner"
            now = utc_now()
            if c.execute(
                "SELECT 1 FROM web_accounts WHERE username=?", (username,)
            ).fetchone():
                raise ValueError("账户名已存在")
            pid = c.execute(
                "INSERT INTO principals(kind,display_name,normalized_name,status,created_at,updated_at,first_seen_at,last_seen_at) VALUES('web_account',?,?,'active',?,?,?,?)",
                (username, username, now, now, now, now),
            ).lastrowid
            c.execute(
                "INSERT INTO access_grants(principal_id,access_level,created_at,updated_at) VALUES(?,?,?,?)",
                (pid, level, now, now),
            )
            aid = c.execute(
                "INSERT INTO web_accounts(principal_id,username,password_hash,recovery_hash) VALUES(?,?,?,?)",
                (
                    pid,
                    username,
                    encoded,
                    digest(recovery) if level == "owner" else None,
                ),
            ).lastrowid
            record_audit(
                c,
                "auth.create",
                source="web",
                actor_id=actor.principal_id if actor else pid,
                target_type="account",
                target_id=aid,
                details={"level": level},
            )
        return recovery if level == "owner" else None

    def _locked(self, c, username, source, now):
        for column, value in [("username", username), ("source", source)]:
            n = c.execute(
                "SELECT COUNT(*) FROM login_attempts WHERE "
                + column
                + "=? AND success=0 AND at>?",
                (value, now - 900),
            ).fetchone()[0]
            if n >= 5:
                return True
        return False

    def login(self, username, password, source, recovery=False):
        username = str(username)[:80]
        now = time.time()
        with self.storage.transaction() as c:
            if self._locked(c, username, source, now):
                raise PermissionDenied("失败次数过多，请15分钟后重试")
            row = c.execute(
                "SELECT * FROM web_accounts WHERE username=? AND enabled=1", (username,)
            ).fetchone()
            valid = False
            if row:
                valid = (
                    (
                        bool(row["recovery_hash"])
                        and hmac.compare_digest(
                            digest(str(password)), row["recovery_hash"]
                        )
                    )
                    if recovery
                    else verify(password, row["password_hash"])
                )
            if not row:
                # comparable cost for nonexistent accounts
                hashlib.scrypt(
                    b"not-a-real-password",
                    salt=b"fixed-dummy-salt",
                    n=16384,
                    r=8,
                    p=1,
                    dklen=32,
                )
            c.execute(
                "INSERT INTO login_attempts(username,source,at,success) VALUES(?,?,?,?)",
                (username, source, now, int(valid)),
            )
            c.execute("DELETE FROM login_attempts WHERE at<?", (now - 86400,))
            if valid:
                decision = _decision(c, row["principal_id"], None)
                valid = decision.allowed and decision.access_level in ("owner", "admin")
            if valid:
                if recovery:
                    c.execute(
                        "UPDATE web_accounts SET recovery_hash=NULL WHERE id=?",
                        (row["id"],),
                    )
                    c.execute(
                        "DELETE FROM web_sessions WHERE account_id=?", (row["id"],)
                    )
                c.execute(
                    "DELETE FROM login_attempts WHERE username=? OR source=?",
                    (username, source),
                )
                token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
                c.execute("DELETE FROM web_sessions WHERE expires_at<?", (now,))
                c.execute(
                    "INSERT INTO web_sessions VALUES(?,?,?,?,?)",
                    (digest(token), row["id"], csrf, now + 43200, now),
                )
                record_audit(
                    c,
                    "auth.recovery" if recovery else "auth.login",
                    source="web",
                    actor_id=row["principal_id"],
                )
                return token, csrf
        # Raise outside the transaction so failed attempts are retained.
        raise PermissionDenied("账户或凭据错误")

    def session(self, token):
        if not token:
            return None
        with self.storage._connection() as c:
            row = c.execute(
                "SELECT s.*,a.principal_id,a.enabled FROM web_sessions s JOIN web_accounts a ON a.id=s.account_id WHERE token_hash=? AND expires_at>?",
                (digest(token), time.time()),
            ).fetchone()
            if not row or not row["enabled"]:
                return None
            d = _decision(c, row["principal_id"], None)
            if not d.allowed or d.access_level not in ("owner", "admin"):
                return None
            return (
                Actor(d.principal_id, d.access_level, "web"),
                row["csrf_token"],
                row["account_id"],
            )

    def logout(self, token):
        with self.storage.transaction() as c:
            c.execute(
                "DELETE FROM web_sessions WHERE token_hash=?", (digest(token or ""),)
            )

    def change_password(self, account_id, current, new_password, actor):
        encoded = password_hash(new_password)
        with self.storage.transaction() as c:
            row = c.execute(
                "SELECT * FROM web_accounts WHERE id=?", (account_id,)
            ).fetchone()
            if (
                not row
                or row["principal_id"] != actor.principal_id
                or not verify(current, row["password_hash"])
            ):
                raise PermissionDenied("当前密码错误")
            c.execute(
                "UPDATE web_accounts SET password_hash=? WHERE id=?",
                (encoded, account_id),
            )
            c.execute("DELETE FROM web_sessions WHERE account_id=?", (account_id,))
            record_audit(c, "auth.password", source="web", actor_id=actor.principal_id)

    def recover_password(self, username, code, new_password, source):
        # Consume recovery first, then replace password and rotate recovery exactly once.
        encoded = password_hash(new_password)
        token, _ = self.login(username, code, source, recovery=True)
        session = self.session(token)
        if not session:
            raise PermissionDenied("恢复失败")
        actor, _, aid = session
        fresh = secrets.token_urlsafe(32)
        with self.storage.transaction() as c:
            c.execute(
                "UPDATE web_accounts SET password_hash=?,recovery_hash=? WHERE id=?",
                (encoded, digest(fresh), aid),
            )
            c.execute("DELETE FROM web_sessions WHERE account_id=?", (aid,))
            record_audit(
                c, "auth.password_recovered", source="web", actor_id=actor.principal_id
            )
        return fresh
