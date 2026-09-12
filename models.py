"""Transport-neutral values; never store UIA controls in worker jobs."""

from dataclasses import dataclass, field


@dataclass(frozen=True)
class InboundEvent:
    event_id: str
    source_key: str
    chat_id: int
    chat_kind: str
    sender_principal_id: int
    content_type: str = "text"
    text: str = ""
    attachments: list = field(default_factory=list)
    observed_at: float = 0


@dataclass(frozen=True)
class ConversationScope:
    scope_id: int
    chat_id: int
    principal_id: int | None
    mode: str


@dataclass(frozen=True)
class ResolvedPolicy:
    access_level: str
    role_id: int
    context_scope_id: int
    allowed_knowledge_bases: tuple = ()
    allowed_tools: tuple = ()


@dataclass(frozen=True)
class QueueJob:
    job_id: str
    inbound_message_id: str
    scope_id: int
    state: str
    attempts: int
    next_attempt_at: float
    lease_until: float | None
    generated_reply: str | None
    last_error: str | None
