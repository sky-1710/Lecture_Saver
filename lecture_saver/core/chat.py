"""Questions asked about the lecture, and the assistant's answers.

One conversation per lecture, kept in ``chat.json`` so reopening the project
brings it back. A question remembers where it was asked from - the playback
position, the slide on screen and the transcript lines the user pointed at -
so the conversation still reads sensibly after the transcript is edited.

The quoted lines are copies of the text as it was when asked. They are part of
the question, not of the transcript: the transcript stays the single source of
truth (§14) and nothing here is ever written back to it.

The assistant keeps its own copy of the conversation - a Codex session, see
:meth:`lecture_saver.translate.engine.TranslationEngine.converse` - so a
follow-up is a turn of that same conversation rather than a new one. Which
session, and what has been sent in it, is :class:`ChatSession`.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

__all__ = [
    "CHAT_SCHEMA_VERSION",
    "ROLE_ASSISTANT",
    "ROLE_USER",
    "ChatLog",
    "ChatMessage",
    "ChatSession",
    "Quote",
]

CHAT_SCHEMA_VERSION = 1

ROLE_USER = "user"
ROLE_ASSISTANT = "assistant"


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _number(value: object) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


@dataclass(frozen=True)
class Quote:
    """A transcript line (or part of one) a question points at."""

    segment_id: str
    start: float
    text: str

    def to_dict(self) -> dict[str, Any]:
        return {"segment_id": self.segment_id, "start": self.start, "text": self.text}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Quote:
        return cls(
            segment_id=str(data.get("segment_id") or ""),
            start=_number(data.get("start")) or 0.0,
            text=str(data.get("text") or ""),
        )


@dataclass
class ChatMessage:
    #: :data:`ROLE_USER` or :data:`ROLE_ASSISTANT`.
    role: str
    text: str
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:10])
    time: str = field(default_factory=_now)
    #: Playback position when the question was asked, in seconds.
    position: float | None = None
    #: The number of the slide shown when the question was asked ("Slide 8").
    slide: int | None = None
    quotes: tuple[Quote, ...] = ()
    #: How much of the lecture went with the question: "nearby" or "lecture".
    scope: str = ""

    @property
    def is_user(self) -> bool:
        return self.role == ROLE_USER

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"id": self.id, "role": self.role, "text": self.text,
                                "time": self.time}
        if self.position is not None:
            data["position"] = self.position
        if self.slide is not None:
            data["slide"] = self.slide
        if self.quotes:
            data["quotes"] = [q.to_dict() for q in self.quotes]
        if self.scope:
            data["scope"] = self.scope
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ChatMessage:
        slide = data.get("slide")
        return cls(
            role=ROLE_ASSISTANT if data.get("role") == ROLE_ASSISTANT else ROLE_USER,
            text=str(data.get("text") or ""),
            id=str(data.get("id") or uuid.uuid4().hex[:10]),
            time=str(data.get("time") or ""),
            position=_number(data.get("position")),
            slide=slide if isinstance(slide, int) and not isinstance(slide, bool) else None,
            quotes=tuple(
                Quote.from_dict(q) for q in data.get("quotes") or [] if isinstance(q, dict)
            ),
            scope=str(data.get("scope") or ""),
        )


@dataclass
class ChatSession:
    """The assistant's side of the conversation: one session it keeps.

    ``sent`` is what the session holds already - each piece of transcript
    (a slide, or a stretch of time) by key, with a fingerprint of its text -
    so a follow-up sends only what is new, and a line corrected since is
    sent again.
    """

    #: The engine's id for the session.
    id: str
    #: Which engine keeps it (``TranslationEngine.key``).
    engine: str
    sent: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "engine": self.engine, "sent": dict(self.sent)}

    @classmethod
    def from_dict(cls, data: object) -> ChatSession | None:
        if not isinstance(data, dict) or not data.get("id"):
            return None
        sent = data.get("sent")
        return cls(
            id=str(data["id"]),
            engine=str(data.get("engine") or ""),
            sent=(
                {str(k): str(v) for k, v in sent.items()} if isinstance(sent, dict) else {}
            ),
        )


@dataclass
class ChatLog:
    """The lecture's conversation, oldest message first."""

    messages: list[ChatMessage] = field(default_factory=list)
    #: The session the conversation continues in, once there is one.
    session: ChatSession | None = None

    @property
    def is_empty(self) -> bool:
        return not self.messages

    def add(self, message: ChatMessage) -> ChatMessage:
        self.messages.append(message)
        return message

    def clear(self) -> None:
        self.messages = []
        self.session = None

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "schema_version": CHAT_SCHEMA_VERSION,
            "messages": [m.to_dict() for m in self.messages],
        }
        if self.session is not None:
            data["session"] = self.session.to_dict()
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> ChatLog:
        if not isinstance(data, dict):
            return cls()
        return cls(
            messages=[
                ChatMessage.from_dict(m) for m in data.get("messages") or [] if isinstance(m, dict)
            ],
            session=ChatSession.from_dict(data.get("session")),
        )
