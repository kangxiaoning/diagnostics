from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any


@dataclass
class SessionState:
    messages: list[Any] = field(default_factory=list)
    cancel_event: asyncio.Event = field(default_factory=asyncio.Event)
    running: bool = False


class SessionStore:
    def __init__(self) -> None:
        self._sessions: dict[str, SessionState] = {}

    def get_or_create(self, session_id: str) -> SessionState:
        return self._sessions.setdefault(session_id, SessionState())

    def get(self, session_id: str) -> SessionState | None:
        return self._sessions.get(session_id)
