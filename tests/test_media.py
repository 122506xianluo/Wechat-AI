import io
import wave
import zipfile
import pytest
from PIL import Image
from media import (
    Media,
    UnsupportedMedia,
    extract_document,
    image_bytes,
    validate_audio,
    safe_name,
    inside,
)
from storage import Storage


def test_image_and_limits():
    out = io.BytesIO()
    Image.new("RGB", (2500, 10), (1, 2, 3)).save(out, format="PNG")
    with Image.open(io.BytesIO(image_bytes(out.getvalue()))) as image:
        assert image.width == 2048
    with pytest.raises(UnsupportedMedia):
        image_bytes(b"x" * (10 * 1024 * 1024 + 1))


def test_docs_and_paths(tmp_path):
    assert extract_document("你好".encode(), "../x.md")[0]["text"] == "你好"
    assert safe_name("../x.md") == "x.md"
    with pytest.raises(ValueError):
        inside(tmp_path, tmp_path / "../secret")
    with pytest.raises(UnsupportedMedia):
        extract_document(b"not pdf", "x.pdf")
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        z.writestr(
            "word/document.xml",
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:p><w:r><w:t>hello</w:t></w:r></w:p></w:document>',
        )
    assert extract_document(out.getvalue(), "x.docx")[0]["text"] == "hello"


def test_audio_duration():
    out = io.BytesIO()
    with wave.open(out, "wb") as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(8000)
        f.writeframes(b"\x00" * 16000)
    validate_audio(out.getvalue(), "x.wav")
    with pytest.raises(UnsupportedMedia):
        validate_audio(b"garbage", "x.mp3")


def test_cleanup_keeps_metadata(tmp_path):
    store = Storage(tmp_path)
    chat = store.ensure_chat("private", "synthetic")
    media = Media(store)
    aid = media._insert(
        {"inbound_message_id": None, "chat_id": chat, "principal_id": None},
        "document",
        "x.txt",
        raw=b"synthetic",
    )
    with store.transaction() as c:
        c.execute("UPDATE attachments SET expires_at=0")
    assert media.cleanup(force=True) == 1
    assert media.list()[0]["status"] == "expired"
    with store._connection() as c:
        row = c.execute("SELECT * FROM attachments WHERE id=?", (aid,)).fetchone()
        assert row["sha256"] and row["relative_path"] is None


def test_fresh_v9(tmp_path):
    store = Storage(tmp_path)
    with store._connection() as c:
        assert c.execute("PRAGMA user_version").fetchone()[0] >= 9
        assert c.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
