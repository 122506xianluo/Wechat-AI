import json
import pytest
import httpx
from storage import Storage
from chats import Chats
from contexts import Contexts
from permissions import Permissions, LOCAL_OWNER
from roles import Roles
from knowledge import Knowledge, lexical, chunks
from safe_tools import Tools, calculate
from job_queue import JobQueue
from bot import ChatLLM, Config, LLMSettings


@pytest.fixture
def setup(tmp_path):
    s = Storage(tmp_path)
    Chats(s).import_legacy(["A", "B"], [])
    a = Permissions(s).resolve_incoming("private", "A", "", "incoming")
    b = Permissions(s).resolve_incoming("private", "B", "", "incoming")
    role = Roles(s).resolve(a.chat_id, a.principal_id)
    return s, a, b, role


def test_explicit_scope_chinese_version_delete(setup):
    s, a, b, r = setup
    k = Knowledge(s)
    kid = k.create("Synthetic", "", LOCAL_OWNER)
    did = k.import_document(
        kid, "note.txt", "恐龙喜欢苹果，机器人不能更改权限。".encode(), LOCAL_OWNER
    )
    assert (
        k.search("恐龙喜欢什么", a.chat_id, a.principal_id, r["id"], remote=False) == []
    )
    k.bind(kid, {"chat_id": a.chat_id}, LOCAL_OWNER)
    result = k.search("恐龙", a.chat_id, a.principal_id, r["id"], remote=False)
    assert result and "苹果" in result[0]["content"]
    assert not k.search("恐龙", b.chat_id, b.principal_id, r["id"], remote=False)
    assert (
        k.import_document(
            kid, "note.txt", "恐龙喜欢苹果，机器人不能更改权限。".encode(), LOCAL_OWNER
        )
        == did
    )
    second = k.import_document(kid, "note.txt", "恐龙喜欢香蕉".encode(), LOCAL_OWNER)
    assert k.documents(kid)[0]["version"] == 2 and second != did
    assert (
        "香蕉"
        in k.search("恐龙", a.chat_id, a.principal_id, r["id"], remote=False)[0][
            "content"
        ]
    )
    k.unbind(kid, k.bindings(kid)[0]["id"], LOCAL_OWNER)
    assert not k.search("恐龙", a.chat_id, a.principal_id, r["id"], remote=False)
    with pytest.raises(ValueError):
        k.delete(kid, LOCAL_OWNER)
    k.delete(kid, LOCAL_OWNER, True)
    with s._connection() as c:
        assert c.execute("SELECT COUNT(*) FROM knowledge_fts").fetchone()[0] == 0


@pytest.mark.parametrize(
    "bad",
    [
        "__import__('os')",
        "open('x')",
        "1<<9999999",
        "10**9999",
        "2**(9**9)",
        "[1,2]",
        "True",
        "1e300",
        "abs(2)",
        "(1).__class__",
    ],
)
def test_calculator_rejects(bad):
    with pytest.raises((ValueError, SyntaxError, OverflowError)):
        calculate(bad)


def test_tools_authorization_and_audit(setup):
    s, a, b, r = setup
    k = Knowledge(s)
    t = Tools(s, k)
    scope = Contexts(s).resolve(a.chat_id, a.principal_id)
    q = JobQueue(s)
    jid = q.enqueue(a.chat_id, a.principal_id, scope["id"], {}, "math", "unique")
    job = q.get(jid)
    assert not t.schemas(job)
    t.configure(r["id"], "calculate", True, LOCAL_OWNER)
    assert (
        json.loads(t.execute("calculate", '{"expression":"(2+3)*4"}', job))["value"]
        == 20
    )
    assert "error" in json.loads(
        t.execute("calculate", '{"expression":"2","path":"secret"}', job)
    )
    assert "error" in json.loads(t.execute("shell", "{}", job))
    assert len(t.runs()) == 3
    t.configure(r["id"], "calculate", False, LOCAL_OWNER)
    assert not t.schemas(job)


def test_chunks_overlap_and_lexical():
    rows = list(chunks([{"text": "x" * 2000, "page": 1, "paragraph": 2}]))
    assert [len(r["content"]) for r in rows] == [800, 800, 640]
    assert "中文" in lexical("中文资料")


def test_native_tool_loop():
    llm = ChatLLM(LLMSettings("http://localhost/v1", "", "fake"), Config())
    requests = []

    def handle(request):
        body = json.loads(request.content)
        requests.append(body)
        if len(requests) == 1:
            msg = {
                "content": None,
                "tool_calls": [
                    {
                        "type": "function",
                        "id": "call1",
                        "function": {
                            "name": "calculate",
                            "arguments": '{"expression":"2+2"}',
                        },
                    }
                ],
            }
        else:
            msg = {"content": "4"}
        return httpx.Response(200, json={"choices": [{"message": msg}]})

    llm.client.close()
    llm.client = httpx.Client(transport=httpx.MockTransport(handle))
    assert (
        llm.reply(
            "hi",
            [],
            tools=[{"type": "function", "function": {"name": "calculate"}}],
            execute_tool=lambda name, args: '{"value":4}',
        )
        == "4"
    )
    assert requests[1]["messages"][-1]["role"] == "tool"
    llm.close()


def test_embeddings_index_and_fallback(setup, monkeypatch):
    from providers import Capabilities, Endpoint

    s, a, b, r = setup
    k = Knowledge(s)
    kid = k.create("KB", "", LOCAL_OWNER)
    did = k.import_document(kid, "x.txt", b"apples are red", LOCAL_OWNER)
    k.bind(kid, {"chat_id": a.chat_id}, LOCAL_OWNER)
    monkeypatch.setattr(Capabilities, "enabled", lambda *args, **kwargs: True)
    monkeypatch.setattr(
        "knowledge.endpoint",
        lambda *args, **kwargs: Endpoint("http://localhost/v1", "", "fake"),
    )
    monkeypatch.setattr(
        "knowledge.embed", lambda target, texts, client: [[1.0, 0.0] for t in texts]
    )
    assert k.index_one()
    with s._connection() as c:
        assert c.execute("SELECT COUNT(*) FROM knowledge_embeddings").fetchone()[0] == 1
    assert k.search("fruit", a.chat_id, a.principal_id, r["id"])

    def fail(*args, **kwargs):
        raise TimeoutError()

    monkeypatch.setattr("knowledge.embed", fail)
    assert k.search("apples", a.chat_id, a.principal_id, r["id"])
    assert not k.search("apples", b.chat_id, b.principal_id, r["id"])
    assert k.documents(kid)[0]["id"] == did
