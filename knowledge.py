"""Explicitly scoped local RAG. Source documents never become instructions."""

from array import array
from hashlib import sha256
import json
import math
import re
import time
import uuid
import httpx
from audit import record_audit
from roles import require_manager
from permissions import Permissions
from media import DOCUMENTS, MIB, extract_document, inside, safe_name
from providers import Capabilities, endpoint, embed


def lexical(text):
    # Unicode61 does not segment Chinese: add Han bigrams as search-only tokens.
    words = re.findall(r"[a-zA-Z0-9_]+", text.lower())
    for sequence in re.findall(r"[\u3400-\u9fff]+", text):
        words.extend(sequence[i : i + 2] for i in range(max(1, len(sequence) - 1)))
    return " ".join(words)


def chunks(segments):
    for part in segments:
        text = part["text"].strip()
        offset = 0
        while offset < len(text):
            yield {
                "content": text[offset : offset + 800],
                "page": part["page"],
                "paragraph": part["paragraph"],
            }
            if offset + 800 >= len(text):
                break
            offset += 680


def cosine(a, b):
    if len(a) != len(b) or not a:
        return 0
    denom = math.sqrt(sum(x * x for x in a) * sum(x * x for x in b))
    return sum(x * y for x, y in zip(a, b)) / denom if denom else 0


class Knowledge:
    def __init__(self, storage, llm=None):
        self.storage, self.llm = storage, llm
        self.base = storage.root / "data" / "knowledge"

    def create(self, name, description, actor):
        if (
            not isinstance(name, str)
            or not 1 <= len(name.strip()) <= 100
            or not isinstance(description, str)
            or len(description) > 2000
        ):
            raise ValueError("知识库名称/说明无效")
        with self.storage.transaction() as c:
            require_manager(c, actor)
            kid = c.execute(
                "INSERT INTO knowledge_bases(name,description) VALUES(?,?)",
                (name.strip(), description),
            ).lastrowid
            record_audit(
                c,
                "knowledge.create",
                source=actor.source,
                actor_id=actor.principal_id,
                target_type="knowledge",
                target_id=kid,
            )
        return kid

    def list(self):
        with self.storage._connection() as c:
            return [
                dict(r)
                for r in c.execute(
                    "SELECT k.*,(SELECT COUNT(*) FROM knowledge_documents d WHERE d.kb_id=k.id AND d.active=1) document_count FROM knowledge_bases k ORDER BY k.id"
                )
            ]

    def documents(self, kid):
        with self.storage._connection() as c:
            return [
                dict(r)
                for r in c.execute(
                    "SELECT d.id,d.name,d.version,d.size_bytes,d.active,d.status,d.created_at,j.state index_state,j.last_error FROM knowledge_documents d LEFT JOIN knowledge_index_jobs j ON j.document_id=d.id WHERE d.kb_id=? ORDER BY d.id DESC",
                    (kid,),
                )
            ]

    def bindings(self, kid):
        with self.storage._connection() as c:
            return [
                dict(r)
                for r in c.execute(
                    "SELECT * FROM knowledge_bindings WHERE kb_id=?", (kid,)
                )
            ]

    def bind(self, kid, data, actor):
        fields = [data.get(k) for k in ("role_id", "chat_id", "principal_id")]
        if not any(x is not None for x in fields) or any(
            x is not None and (type(x) is not int or x < 1) for x in fields
        ):
            raise ValueError("必须显式授权角色、聊天或用户；组合条件为 AND")
        with self.storage.transaction() as c:
            require_manager(c, actor)
            for table, ident in zip(("roles", "chats", "principals"), fields):
                if (
                    ident is not None
                    and not c.execute(
                        "SELECT 1 FROM " + table + " WHERE id=?", (ident,)
                    ).fetchone()
                ):
                    raise ValueError("授权对象不存在")
            if fields[1] and fields[2]:
                if not c.execute(
                    "SELECT 1 FROM principals WHERE id=? AND chat_id=?",
                    (fields[2], fields[1]),
                ).fetchone():
                    raise ValueError("用户不属于该聊天")
            c.execute(
                "INSERT OR IGNORE INTO knowledge_bindings(kb_id,role_id,chat_id,principal_id) VALUES(?,?,?,?)",
                (kid, *fields),
            )
            c.execute(
                "UPDATE knowledge_bases SET revision=revision+1 WHERE id=?", (kid,)
            )
            record_audit(
                c,
                "knowledge.bind",
                source=actor.source,
                actor_id=actor.principal_id,
                target_type="knowledge",
                target_id=kid,
                details=dict(zip(("role_id", "chat_id", "principal_id"), fields)),
            )

    def unbind(self, kid, bid, actor):
        with self.storage.transaction() as c:
            require_manager(c, actor)
            c.execute(
                "DELETE FROM knowledge_bindings WHERE id=? AND kb_id=?", (bid, kid)
            )
            c.execute(
                "UPDATE knowledge_bases SET revision=revision+1 WHERE id=?", (kid,)
            )
            record_audit(
                c,
                "knowledge.unbind",
                source=actor.source,
                actor_id=actor.principal_id,
                target_type="knowledge",
                target_id=kid,
            )

    def import_document(self, kid, name, raw, actor):
        from pathlib import Path

        name = safe_name(name)
        if Path(name).suffix.lower() not in DOCUMENTS:
            raise ValueError("只支持 PDF/TXT/MD/DOCX")
        with self.storage._connection() as c:
            require_manager(c, actor)
            if not c.execute(
                "SELECT 1 FROM knowledge_bases WHERE id=?", (kid,)
            ).fetchone():
                raise ValueError("知识库不存在")
        if len(raw) > 50 * MIB:
            raise ValueError("知识库文档最大50MiB")
        parts = list(
            chunks(extract_document(raw, name, max_bytes=50 * MIB, max_pages=500))
        )
        digest = sha256(raw).hexdigest()
        rel = str(kid) + "/" + uuid.uuid4().hex + Path(name).suffix.lower()
        path = inside(self.base, self.base / rel)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
        try:
            with self.storage.transaction() as c:
                require_manager(c, actor)
                old = c.execute(
                    "SELECT id FROM knowledge_documents WHERE kb_id=? AND sha256=? AND active=1",
                    (kid, digest),
                ).fetchone()
                if old:
                    path.unlink(missing_ok=True)
                    return old[0]
                version = c.execute(
                    "SELECT COALESCE(MAX(version),0)+1 FROM knowledge_documents WHERE kb_id=? AND name=?",
                    (kid, name),
                ).fetchone()[0]
                c.execute(
                    "UPDATE knowledge_documents SET active=0 WHERE kb_id=? AND name=?",
                    (kid, name),
                )
                did = c.execute(
                    "INSERT INTO knowledge_documents(kb_id,name,version,sha256,relative_path,size_bytes,status) VALUES(?,?,?,?,?,?,'fts_ready')",
                    (kid, name, version, digest, rel, len(raw)),
                ).lastrowid
                for index, part in enumerate(parts):
                    cid = c.execute(
                        "INSERT INTO knowledge_chunks(document_id,ordinal,page,paragraph,content) VALUES(?,?,?,?,?)",
                        (did, index, part["page"], part["paragraph"], part["content"]),
                    ).lastrowid
                    c.execute(
                        "INSERT INTO knowledge_fts(rowid,content) VALUES(?,?)",
                        (cid, lexical(part["content"])),
                    )
                c.execute(
                    "INSERT INTO knowledge_index_jobs(document_id,updated_at) VALUES(?,?)",
                    (did, time.time()),
                )
                c.execute(
                    "UPDATE knowledge_bases SET revision=revision+1 WHERE id=?", (kid,)
                )
                record_audit(
                    c,
                    "knowledge.import",
                    source=actor.source,
                    actor_id=actor.principal_id,
                    target_type="document",
                    target_id=did,
                    details={"kb_id": kid, "version": version, "chunks": len(parts)},
                )
            return did
        except Exception:
            path.unlink(missing_ok=True)
            raise

    def promote(self, kid, aid, actor):
        from media import Media

        with self.storage._connection() as c:
            require_manager(c, actor)
            row = c.execute("SELECT * FROM attachments WHERE id=?", (aid,)).fetchone()
            if (
                not row
                or row["content_type"] != "document"
                or row["status"] != "ready"
                or row["expires_at"] <= time.time()
            ):
                raise ValueError("附件不是可提升的有效文档")
        base = Media(self.storage).base
        return self.import_document(
            kid,
            row["original_name"],
            inside(base, base / row["relative_path"]).read_bytes(),
            actor,
        )

    def delete(self, kid, actor, confirm=False, document_id=None):
        if confirm is not True:
            raise ValueError("删除知识内容需要二次确认")
        with self.storage.transaction() as c:
            require_manager(c, actor)
            rows = c.execute(
                "SELECT id,relative_path FROM knowledge_documents WHERE kb_id=? AND (? IS NULL OR id=?)",
                (kid, document_id, document_id),
            ).fetchall()
            for row in rows:
                c.execute("DELETE FROM knowledge_documents WHERE id=?", (row["id"],))
            if document_id is None:
                c.execute("DELETE FROM knowledge_bases WHERE id=?", (kid,))
            else:
                c.execute(
                    "UPDATE knowledge_bases SET revision=revision+1 WHERE id=?", (kid,)
                )
            record_audit(
                c,
                "knowledge.delete",
                source=actor.source,
                actor_id=actor.principal_id,
                target_type="knowledge",
                target_id=kid,
                details={"document_id": document_id},
            )
        for row in rows:
            inside(self.base, self.base / row["relative_path"]).unlink(missing_ok=True)

    def allowed(self, chat_id, pid, role_id):
        if not Permissions(self.storage).resolve(pid, chat_id).allowed:
            return []
        with self.storage._connection() as c:
            return [
                r[0]
                for r in c.execute(
                    "SELECT DISTINCT k.id FROM knowledge_bases k JOIN knowledge_bindings b ON b.kb_id=k.id WHERE k.enabled=1 AND (b.role_id IS NULL OR b.role_id=?) AND (b.chat_id IS NULL OR b.chat_id=?) AND (b.principal_id IS NULL OR b.principal_id=?) ORDER BY k.id",
                    (role_id, chat_id, pid),
                )
            ]

    def access_stamp(self, chat_id, pid, role_id):
        allowed = self.allowed(chat_id, pid, role_id)
        with self.storage._connection() as c:
            revisions = [
                (
                    kid,
                    c.execute(
                        "SELECT revision FROM knowledge_bases WHERE id=?", (kid,)
                    ).fetchone()[0],
                )
                for kid in allowed
            ]
        return sha256(json.dumps(revisions).encode()).hexdigest()

    def search(self, query, chat_id, pid, role_id, *, remote=True):
        allowed = self.allowed(chat_id, pid, role_id)
        if not allowed or not isinstance(query, str) or not query.strip():
            return []
        slots = ",".join("?" for _ in allowed)
        terms = list(dict.fromkeys(lexical(query[:4000]).split()))[:40]
        query_fts = " OR ".join('"' + word + '"' for word in terms)
        ranked, meta = [], {}
        with self.storage._connection() as c:
            if query_fts:
                rows = c.execute(
                    "SELECT k.id,k.content,k.page,k.paragraph,d.name,d.version,d.kb_id,bm25(knowledge_fts) score FROM knowledge_fts JOIN knowledge_chunks k ON k.id=knowledge_fts.rowid JOIN knowledge_documents d ON d.id=k.document_id WHERE knowledge_fts MATCH ? AND d.active=1 AND d.kb_id IN ("
                    + slots
                    + ") ORDER BY score LIMIT 30",
                    (query_fts, *allowed),
                ).fetchall()
                ranked.append([r["id"] for r in rows])
                meta.update({r["id"]: dict(r) for r in rows})
        if remote and Capabilities(self.storage, self.llm).enabled("embeddings"):
            try:
                target = endpoint("embeddings", self.llm, root=self.storage.root)
                with httpx.Client(timeout=4, follow_redirects=False) as client:
                    vector = embed(target, [query[:4000]], client=client)[0]
                scores = []
                with self.storage._connection() as c:
                    c.execute("PRAGMA busy_timeout=200")
                    # SQL scope filter precedes vector decoding; never rank unauthorized text.
                    rows = c.execute(
                        "SELECT k.id,k.content,k.page,k.paragraph,d.name,d.version,d.kb_id,e.vector FROM knowledge_embeddings e JOIN knowledge_chunks k ON k.id=e.chunk_id JOIN knowledge_documents d ON d.id=k.document_id WHERE e.model_fingerprint=? AND d.active=1 AND d.kb_id IN ("
                        + slots
                        + ")",
                        (target.fingerprint, *allowed),
                    )
                    for row in rows:
                        values = array("f")
                        values.frombytes(row["vector"])
                        score = cosine(vector, values)
                        if score > 0:
                            scores.append((score, row["id"]))
                            meta[row["id"]] = {
                                k: row[k] for k in row.keys() if k != "vector"
                            }
                ranked.append([cid for _, cid in sorted(scores, reverse=True)[:30]])
            except Exception:
                # Explicit fallback; FTS remains available without provider support.
                pass
        fused = {}
        for ranking in ranked:
            for index, cid in enumerate(ranking):
                fused[cid] = fused.get(cid, 0) + 1 / (60 + index + 1)
        result = []
        remaining = 6000
        # Re-check grants after network work, before releasing any retrieved content.
        still = set(self.allowed(chat_id, pid, role_id))
        for cid in sorted(fused, key=fused.get, reverse=True)[:5]:
            row = meta[cid]
            if row["kb_id"] not in still:
                continue
            item = {
                k: row[k]
                for k in (
                    "id",
                    "content",
                    "page",
                    "paragraph",
                    "name",
                    "version",
                    "kb_id",
                )
            }
            item["content"] = item["content"][:remaining]
            remaining -= len(item["content"])
            result.append(item)
        return result

    def index_one(self):
        """A single short batch on the existing two-worker pool; persisted retries."""
        if not Capabilities(self.storage, self.llm).enabled("embeddings"):
            return False
        now, owner = time.time(), uuid.uuid4().hex
        target = endpoint("embeddings", self.llm, root=self.storage.root)
        with self.storage.transaction() as c:
            c.execute(
                "UPDATE knowledge_index_jobs SET state=CASE WHEN attempts>=3 THEN 'needs_review' ELSE 'queued' END WHERE state='processing' AND lease_until<?",
                (now,),
            )
            job = c.execute(
                "SELECT j.* FROM knowledge_index_jobs j JOIN knowledge_documents d ON d.id=j.document_id WHERE j.state IN ('queued','retry_wait') AND j.next_attempt_at<=? AND d.active=1 ORDER BY j.updated_at LIMIT 1",
                (now,),
            ).fetchone()
            if not job:
                return False
            did = job["document_id"]
            c.execute(
                "UPDATE knowledge_index_jobs SET state='processing',attempts=attempts+1,lease_owner=?,lease_until=? WHERE document_id=?",
                (owner, now + 120, did),
            )
            rows = c.execute(
                "SELECT k.id,k.content FROM knowledge_chunks k LEFT JOIN knowledge_embeddings e ON e.chunk_id=k.id AND e.model_fingerprint=? WHERE k.document_id=? AND e.chunk_id IS NULL ORDER BY k.ordinal LIMIT 16",
                (target.fingerprint, did),
            ).fetchall()
        try:
            if rows:
                with httpx.Client(timeout=60, follow_redirects=False) as client:
                    vectors = embed(target, [r["content"] for r in rows], client=client)
            with self.storage.transaction() as c:
                valid = c.execute(
                    "SELECT 1 FROM knowledge_index_jobs WHERE document_id=? AND state='processing' AND lease_owner=?",
                    (did, owner),
                ).fetchone()
                if not valid:
                    return False
                for row, vector in zip(rows, vectors if rows else []):
                    c.execute(
                        "INSERT OR REPLACE INTO knowledge_embeddings VALUES(?,?,?,?)",
                        (
                            row["id"],
                            target.fingerprint,
                            len(vector),
                            array("f", vector).tobytes(),
                        ),
                    )
                state = "queued" if len(rows) == 16 else "completed"
                c.execute(
                    "UPDATE knowledge_index_jobs SET state=?,attempts=0,lease_owner=NULL,lease_until=NULL,last_error=NULL,updated_at=? WHERE document_id=?",
                    (state, time.time(), did),
                )
                if state == "completed":
                    c.execute(
                        "UPDATE knowledge_documents SET status='vector_ready' WHERE id=?",
                        (did,),
                    )
            return True
        except Exception as exc:
            attempts = job["attempts"] + 1
            status = getattr(getattr(exc, "response", None), "status_code", None)
            permanent = isinstance(exc, ValueError) or status in (
                400,
                401,
                403,
                404,
                422,
            )
            state = "needs_review" if attempts >= 3 or permanent else "retry_wait"
            with self.storage.transaction() as c:
                c.execute(
                    "UPDATE knowledge_index_jobs SET state=?,next_attempt_at=?,last_error=?,lease_owner=NULL,lease_until=NULL,updated_at=? WHERE document_id=? AND lease_owner=?",
                    (
                        state,
                        time.time() + (5 if attempts == 1 else 30),
                        type(exc).__name__,
                        time.time(),
                        did,
                        owner,
                    ),
                )
            return False

    def reindex(self, did, actor):
        with self.storage.transaction() as c:
            require_manager(c, actor)
            c.execute(
                "UPDATE knowledge_index_jobs SET state='queued',attempts=0,next_attempt_at=0,last_error=NULL WHERE document_id=? AND state!='processing'",
                (did,),
            )
            record_audit(
                c,
                "knowledge.reindex",
                source=actor.source,
                actor_id=actor.principal_id,
                target_type="document",
                target_id=did,
            )
