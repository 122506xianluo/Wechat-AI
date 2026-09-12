"""Configured OpenAI-compatible endpoints. No arbitrary URLs from model/user tools."""

from dataclasses import dataclass
from hashlib import sha256
import json
import os
import time
import httpx


@dataclass(frozen=True)
class Endpoint:
    base: str
    key: str
    model: str

    @property
    def headers(self):
        return {"Authorization": "Bearer " + self.key} if self.key else {}

    def url(self, suffix):
        return self.base.rstrip("/") + "/" + suffix

    @property
    def fingerprint(self):
        # Never persist keys, even their hashes.
        return sha256((self.base + "|" + self.model).encode()).hexdigest()


def endpoint(kind, llm=None, model=None, root=None):
    from bot import LLMSettings, ROOT
    from dotenv import dotenv_values

    values = dotenv_values((root or ROOT) / ".env")

    def value(key):
        return str(values.get(key) or os.getenv(key, "")).strip()

    llm = llm or LLMSettings(
        *(value("LLM_" + key) for key in ("BASE_URL", "API_KEY", "MODEL"))
    )
    prefix = {"embeddings": "EMBEDDING", "transcription": "TRANSCRIPTION"}.get(
        kind, "LLM"
    )
    base = (value(prefix + "_BASE_URL") if prefix != "LLM" else "") or llm.base_url
    base = base.removesuffix("/chat/completions").rstrip("/")
    key = (value(prefix + "_API_KEY") if prefix != "LLM" else "") or llm.api_key
    chosen = model or (value(prefix + "_MODEL") if prefix != "LLM" else llm.model)
    LLMSettings(base, key, chosen).validate()
    return Endpoint(base, key, chosen)


class Capabilities:
    def __init__(self, storage, llm=None):
        self.storage, self.llm = storage, llm

    def enabled(self, name, model=None):
        try:
            target = endpoint(name, self.llm, model, self.storage.root)
        except ValueError:
            return False
        with self.storage._connection() as c:
            row = c.execute(
                "SELECT value FROM runtime_settings WHERE key=?",
                ("capability:" + name,),
            ).fetchone()
        if not row:
            return False
        data = json.loads(row[0])
        return (
            data.get("supported") is True
            and data.get("fingerprint") == target.fingerprint
        )

    def list(self):
        result = []
        for name in ("vision", "tools", "embeddings", "transcription"):
            with self.storage._connection() as c:
                row = c.execute(
                    "SELECT value FROM runtime_settings WHERE key=?",
                    ("capability:" + name,),
                ).fetchone()
            data = json.loads(row[0]) if row else {}
            result.append(
                {
                    "name": name,
                    "enabled": self.enabled(name),
                    "checked_at": data.get("checked_at"),
                    "error": data.get("error"),
                }
            )
        return result

    def probe(self, name, actor, *, sample=None, filename=None):
        from roles import require_manager
        from audit import record_audit

        if name not in ("vision", "tools", "embeddings", "transcription"):
            raise ValueError("未知能力")
        with self.storage._connection() as c:
            require_manager(c, actor, owner=True)
        target = endpoint(name, self.llm, root=self.storage.root)
        error = None
        try:
            with httpx.Client(timeout=45, follow_redirects=False) as client:
                if name == "embeddings":
                    vectors = embed(target, ["capability check"], client=client)
                    if not vectors:
                        raise ValueError("empty_embedding")
                elif name == "transcription":
                    if not sample or not filename:
                        raise ValueError("请上传一段真实短语音作为能力检查样本")
                    from media import validate_audio

                    validate_audio(sample, filename)
                    transcribe(target, sample, filename, client=client)
                else:
                    content = (
                        "Please call calculate with expression 1+1."
                        if name == "tools"
                        else "Describe the color of the image."
                    )
                    body = {
                        "model": target.model,
                        "messages": [{"role": "user", "content": content}],
                        "max_tokens": 100,
                    }
                    if name == "vision":
                        from PIL import Image
                        import io
                        import base64

                        out = io.BytesIO()
                        Image.new("RGB", (32, 32), (230, 20, 20)).save(
                            out, format="PNG"
                        )
                        body["messages"][0]["content"] = [
                            {"type": "text", "text": content},
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": "data:image/png;base64,"
                                    + base64.b64encode(out.getvalue()).decode()
                                },
                            },
                        ]
                    else:
                        body["tools"] = [
                            {
                                "type": "function",
                                "function": {
                                    "name": "calculate",
                                    "description": "Calculate a number",
                                    "parameters": {
                                        "type": "object",
                                        "properties": {
                                            "expression": {"type": "string"}
                                        },
                                        "required": ["expression"],
                                        "additionalProperties": False,
                                    },
                                },
                            }
                        ]
                        body["tool_choice"] = {
                            "type": "function",
                            "function": {"name": "calculate"},
                        }
                    r = client.post(
                        target.url("chat/completions"),
                        headers=target.headers,
                        json=body,
                    )
                    r.raise_for_status()
                    msg = r.json()["choices"][0]["message"]
                    if name == "tools":
                        calls = msg.get("tool_calls") or []
                        if not any(
                            x.get("function", {}).get("name") == "calculate"
                            for x in calls
                        ):
                            raise ValueError("native_tool_calls_missing")
                    elif not msg.get("content"):
                        raise ValueError("vision_response_missing")
        except Exception as exc:
            code = getattr(getattr(exc, "response", None), "status_code", None)
            error = type(exc).__name__ + (" HTTP " + str(code) if code else "")
        value = {
            "supported": error is None,
            "fingerprint": target.fingerprint,
            "checked_at": time.time(),
            "error": error,
        }
        with self.storage.transaction() as c:
            c.execute(
                "INSERT INTO runtime_settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value,revision=revision+1",
                ("capability:" + name, json.dumps(value)),
            )
            record_audit(
                c,
                "capability.probe",
                source=actor.source,
                actor_id=actor.principal_id,
                target_type="capability",
                target_id=name,
                details={"supported": error is None, "error": error},
            )
        return {"name": name, "supported": error is None, "error": error}


def transcribe(target, raw, filename, *, client):
    response = client.post(
        target.url("audio/transcriptions"),
        headers=target.headers,
        files={"file": (filename, raw, "application/octet-stream")},
        data={"model": target.model, "response_format": "json"},
    )
    response.raise_for_status()
    text = response.json().get("text")
    if not isinstance(text, str) or not text.strip():
        raise ValueError("转写接口未返回文字")
    return text.strip()[:32000]


def embed(target, texts, *, client):
    import math

    response = client.post(
        target.url("embeddings"),
        headers=target.headers,
        json={"model": target.model, "input": texts, "encoding_format": "float"},
    )
    response.raise_for_status()
    data = sorted(response.json()["data"], key=lambda item: item["index"])
    vectors = [item["embedding"] for item in data]
    if (
        len(vectors) != len(texts)
        or not vectors
        or len(vectors[0]) > 16384
        or not vectors[0]
    ):
        raise ValueError("invalid_embedding_shape")
    size = len(vectors[0])
    if any(
        len(v) != size
        or not all(type(x) in (int, float) and math.isfinite(x) for x in v)
        or not any(v)
        for v in vectors
    ):
        raise ValueError("invalid_embedding_values")
    return vectors
