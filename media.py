"""Bounded, local attachment storage and extraction. No paths supplied by LLMs."""

from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
import base64
import io
import re
import time
import uuid
import warnings
import zipfile
import xml.etree.ElementTree as ET

MIB = 1024 * 1024
DOCUMENTS = {".pdf", ".txt", ".md", ".docx"}


class UnsupportedMedia(ValueError):
    pass


def safe_name(name):
    name = re.split(r"[/\\]", str(name))[-1]
    name = re.sub(r'[<>:"|?*\x00-\x1f]', "_", name).strip(" .")[:180]
    return name or "attachment"


def inside(base, path):
    base, path = Path(base).resolve(), Path(path).resolve()
    if path == base or not path.is_relative_to(base):
        raise ValueError("文件路径不在指定数据目录内")
    return path


def image_bytes(raw):
    from PIL import Image, ImageOps

    if len(raw) > 10 * MIB:
        raise UnsupportedMedia("图片超过10MiB")
    with warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        with Image.open(io.BytesIO(raw)) as check:
            if check.format not in ("JPEG", "PNG", "WEBP", "GIF"):
                raise UnsupportedMedia("不支持该图片格式")
            if check.width * check.height > 20_000_000:
                raise UnsupportedMedia("图片超过20MP")
            check.verify()
        with Image.open(io.BytesIO(raw)) as source:
            source.seek(0)
            image = ImageOps.exif_transpose(source).convert("RGB")
            image.thumbnail((2048, 2048))
            out = io.BytesIO()
            image.save(out, "JPEG", quality=85)
            return out.getvalue()


def validate_audio(raw, name):
    import wave
    import mutagen

    if len(raw) > 25 * MIB:
        raise UnsupportedMedia("语音超过25MiB")
    ext = Path(safe_name(name)).suffix.lower()
    if ext not in (".wav", ".mp3", ".m4a", ".mp4", ".ogg", ".flac", ".webm"):
        raise UnsupportedMedia("音频格式无法安全解码")
    try:
        if raw.startswith(b"RIFF") and raw[8:12] == b"WAVE":
            with wave.open(io.BytesIO(raw)) as audio:
                seconds = audio.getnframes() / audio.getframerate()
        else:
            audio = mutagen.File(io.BytesIO(raw))
            if audio is None:
                raise ValueError("unknown_audio")
            seconds = audio.info.length
        if not 0 < seconds <= 600:
            raise UnsupportedMedia("音频为空或超过10分钟")
    except UnsupportedMedia:
        raise
    except Exception as exc:
        raise UnsupportedMedia("无法验证音频时长/格式") from exc


def extract_document(raw, name, *, max_bytes=25 * MIB, max_pages=500):
    """Return page/paragraph segments; reject ZIP bombs, DTDs and non-text PDFs."""
    if not raw or len(raw) > max_bytes:
        raise UnsupportedMedia("文档为空或超过大小限制")
    ext = Path(safe_name(name)).suffix.lower()
    result = []
    if ext in (".txt", ".md"):
        if b"\0" in raw:
            raise UnsupportedMedia("非文本文件")
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            try:
                text = raw.decode("gb18030")
            except UnicodeDecodeError as exc:
                raise UnsupportedMedia("请使用 UTF-8 文本") from exc
        result = [
            {"page": 1, "paragraph": i + 1, "text": p}
            for i, p in enumerate(text.split("\n\n"))
            if p.strip()
        ]
    elif ext == ".pdf":
        if not raw.startswith(b"%PDF-"):
            raise UnsupportedMedia("PDF文件签名不匹配")
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(raw), strict=True)
        if reader.is_encrypted or len(reader.pages) > max_pages:
            raise UnsupportedMedia("PDF加密或页数超过限制")
        total = 0
        for index, page in enumerate(reader.pages):
            # Bound decompressed page streams before passing them to the extractor.
            stream = page.get_contents()
            if stream and len(stream.get_data()) > 8 * MIB:
                raise UnsupportedMedia("PDF页面过于复杂")
            text = page.extract_text() or ""
            total += len(text)
            if total > 2_000_000:
                raise UnsupportedMedia("文档提取文字过多")
            if text.strip():
                result.append({"page": index + 1, "paragraph": 1, "text": text})
    elif ext == ".docx":
        if not raw.startswith(b"PK"):
            raise UnsupportedMedia("DOCX文件签名不匹配")
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            infos = archive.infolist()
            if (
                len(infos) > 2000
                or sum(x.file_size for x in infos) > 100 * MIB
                or any(
                    x.file_size > 16 * MIB
                    or (
                        x.file_size > 1 * MIB
                        and x.file_size > max(x.compress_size, 1) * 200
                    )
                    for x in infos
                )
            ):
                raise UnsupportedMedia("DOCX压缩内容超限")
            if "word/document.xml" not in archive.namelist():
                raise UnsupportedMedia("不是有效DOCX")
            xml = archive.read("word/document.xml")
            if b"<!DOCTYPE" in xml.upper() or b"<!ENTITY" in xml.upper():
                raise UnsupportedMedia("DOCX包含不允许的XML实体")
            tree = ET.fromstring(xml)
            ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
            for index, p in enumerate(tree.findall(".//w:p", ns)):
                text = "".join(t.text or "" for t in p.findall(".//w:t", ns))
                if text.strip():
                    result.append({"page": None, "paragraph": index + 1, "text": text})
    else:
        raise UnsupportedMedia("暂不支持该文件类型，只支持 PDF/TXT/MD/DOCX")
    if not result:
        raise UnsupportedMedia("没有可提取文字；扫描PDF暂不支持OCR")
    if sum(len(p["text"]) for p in result) > 2_000_000:
        raise UnsupportedMedia("提取文字过多")
    return result


class Media:
    def __init__(self, storage, llm=None):
        self.storage, self.llm = storage, llm
        self.base = storage.root / "data" / "attachments"
        self.last_cleanup = 0

    def _insert(self, job, kind, name, *, raw=None, transcript=None, error=None):
        now, aid = time.time(), uuid.uuid4().hex
        rel = None
        if raw is not None:
            ext = Path(safe_name(name)).suffix.lower()
            rel = datetime.now(timezone.utc).strftime("%Y/%m/") + aid + ext
            path = inside(self.base, self.base / rel)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(raw)
        try:
            with self.storage.transaction() as c:
                c.execute(
                    "INSERT INTO attachments(id,message_id,chat_id,principal_id,content_type,original_name,relative_path,sha256,size_bytes,status,error,created_at,expires_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        aid,
                        job["inbound_message_id"],
                        job["chat_id"],
                        job["principal_id"],
                        kind,
                        safe_name(name),
                        rel,
                        sha256(raw).hexdigest() if raw is not None else None,
                        len(raw or b""),
                        "unsupported" if error else "ready",
                        error,
                        now,
                        now + 7 * 86400,
                    ),
                )
                if transcript:
                    c.execute(
                        "INSERT INTO attachment_extractions VALUES(?,?,?,?,?)",
                        (aid, "wechat_native", transcript[:32000], "ready", now),
                    )
        except Exception:
            if rel:
                inside(self.base, self.base / rel).unlink(missing_ok=True)
            raise
        return aid

    def capture(self, job, message, desktop):
        """Called only by the UI thread AFTER identity, approval and trigger checks."""
        ids, total = [], 0
        if len(message.attachments) > 3:
            return [
                self._insert(
                    job, "unsupported", "attachments", error="单条消息最多3个附件"
                )
            ]
        for descriptor in message.attachments:
            kind = descriptor.get("content_type", "unsupported")
            name = descriptor.get("name") or "attachment"
            try:
                data = desktop.capture_attachment(message, descriptor)
                raw, transcript = data.get("raw"), data.get("transcript")
                name = data.get("name", name)
                total += len(raw or b"")
                if total > 40 * MIB:
                    raise UnsupportedMedia("附件总大小超过40MiB")
                if raw is not None:
                    if kind == "image":
                        raw = image_bytes(raw)
                        name = "image.jpg"
                    elif kind == "audio":
                        validate_audio(raw, name)
                    elif len(raw) > 25 * MIB:
                        raise UnsupportedMedia("文档超过25MiB")
                    elif Path(name).suffix.lower() not in DOCUMENTS:
                        raise UnsupportedMedia("暂不支持该文件类型")
                elif not transcript:
                    raise UnsupportedMedia("无法安全取得该附件")
                ids.append(
                    self._insert(job, kind, name, raw=raw, transcript=transcript)
                )
            except Exception as exc:
                from bot import FocusLost

                if isinstance(exc, FocusLost):
                    raise
                reason = (
                    str(exc)
                    if isinstance(exc, UnsupportedMedia)
                    else "附件提取失败：" + type(exc).__name__
                )
                ids.append(self._insert(job, kind, name, error=reason))
        return ids

    def prepare(self, ids, job, model):
        from providers import Capabilities, endpoint, transcribe
        import httpx

        text, images, notices = [], [], []
        for aid in ids:
            with self.storage._connection() as c:
                row = c.execute(
                    "SELECT * FROM attachments WHERE id=? AND message_id=? AND chat_id=? AND principal_id IS ?",
                    (
                        aid,
                        job["inbound_message_id"],
                        job["chat_id"],
                        job["principal_id"],
                    ),
                ).fetchone()
                cached = c.execute(
                    "SELECT * FROM attachment_extractions WHERE attachment_id=? AND status='ready'",
                    (aid,),
                ).fetchone()
            if not row:
                raise ValueError("附件不属于当前任务")
            row = dict(row)
            if row["expires_at"] <= time.time() or row["status"] == "expired":
                notices.append("附件已过期，请重新发送。")
                continue
            if row["status"] == "unsupported":
                notices.append(row["error"])
                continue
            if cached:
                text.append(cached["content"])
                continue
            try:
                raw = inside(self.base, self.base / row["relative_path"]).read_bytes()
                if row["content_type"] == "image":
                    if not Capabilities(self.storage, self.llm).enabled(
                        "vision", model
                    ):
                        notices.append("当前模型尚未通过vision能力检查，无法理解图片。")
                    else:
                        images.append(
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": "data:image/jpeg;base64,"
                                    + base64.b64encode(raw).decode()
                                },
                            }
                        )
                    continue
                if row["content_type"] == "audio":
                    if not Capabilities(self.storage, self.llm).enabled(
                        "transcription"
                    ):
                        raise UnsupportedMedia("请先配置并检查语音转写能力")
                    with httpx.Client(timeout=90, follow_redirects=False) as client:
                        extracted = transcribe(
                            endpoint("transcription", self.llm, root=self.storage.root),
                            raw,
                            row["original_name"],
                            client=client,
                        )
                    method = "transcription"
                else:
                    parts = extract_document(raw, row["original_name"])
                    extracted = "\n".join(
                        "["
                        + row["original_name"]
                        + " / 页"
                        + str(p["page"] or "-")
                        + " 段"
                        + str(p["paragraph"])
                        + "] "
                        + p["text"]
                        for p in parts
                    )[:24000]
                    method = "document"
                with self.storage.transaction() as c:
                    c.execute(
                        "INSERT OR REPLACE INTO attachment_extractions VALUES(?,?,?,?,?)",
                        (aid, method, extracted, "ready", time.time()),
                    )
                text.append(extracted)
            except (UnsupportedMedia, FileNotFoundError) as exc:
                notices.append(
                    str(exc)
                    if isinstance(exc, UnsupportedMedia)
                    else "附件文件已不可用"
                )
        return "\n".join(text)[:32000], images, notices

    def list(self):
        with self.storage._connection() as c:
            return [
                dict(r)
                for r in c.execute(
                    "SELECT id,chat_id,principal_id,content_type,original_name,size_bytes,status,error,created_at,expires_at FROM attachments ORDER BY created_at DESC LIMIT 300"
                )
            ]

    def cleanup(self, force=False):
        now = time.time()
        if not force and now - self.last_cleanup < 86400:
            return 0
        count = 0
        with self.storage.transaction() as c:
            rows = c.execute(
                "SELECT * FROM attachments WHERE expires_at<=? AND status!='expired'",
                (now,),
            ).fetchall()
            for row in rows:
                if row["relative_path"]:
                    inside(self.base, self.base / row["relative_path"]).unlink(
                        missing_ok=True
                    )
                c.execute(
                    "UPDATE attachments SET relative_path=NULL,status='expired' WHERE id=?",
                    (row["id"],),
                )
                c.execute(
                    "DELETE FROM attachment_extractions WHERE attachment_id=?",
                    (row["id"],),
                )
                count += 1
        self.last_cleanup = now
        return count
