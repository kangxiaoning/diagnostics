from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from langchain_core.messages import AIMessage, HumanMessage

from diagnostics.agent import build_agent
from diagnostics.agent.streaming import stream_agent_events
from diagnostics.config import STATIC_DIR, Settings
from diagnostics.server.schemas import ChatRequest
from diagnostics.server.sessions import SessionStore
from diagnostics.server.sse import sse
from diagnostics.server.step_tracker import TreeBuilder


def create_app(settings: Settings | None = None, agent: Any | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    agent = agent or build_agent(settings)
    sessions = SessionStore()

    app = FastAPI(title=settings.app_title)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/api/health")
    async def health() -> dict[str, str]:
        return {
            "status": "ok",
            "model": settings.model,
            "base_url": settings.base_url,
            "api_key_configured": str(settings.api_key_configured).lower(),
        }

    @app.post("/api/sessions/{session_id}/cancel")
    async def cancel_session(session_id: str) -> dict[str, str]:
        state = sessions.get(session_id)
        if state is None:
            raise HTTPException(status_code=404, detail="session not found")
        state.cancel_event.set()
        return {"status": "cancelling", "session_id": session_id}

    @app.post("/api/chat/stream")
    async def stream_chat(request: ChatRequest) -> StreamingResponse:
        session_id = request.session_id or uuid.uuid4().hex
        state = sessions.get_or_create(session_id)
        if state.running:
            raise HTTPException(status_code=409, detail="session is already running")

        return StreamingResponse(
            _chat_event_stream(request, session_id, state, agent, settings),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    return app


async def _chat_event_stream(
    request: ChatRequest,
    session_id: str,
    state: Any,
    agent: Any,
    settings: Settings,
) -> AsyncIterator[str]:
    state.running = True
    state.cancel_event = asyncio.Event()
    state.messages.append(HumanMessage(content=request.message))
    state.messages = state.messages[-settings.max_history_messages :]
    assistant_text: list[str] = []

    tree = TreeBuilder()

    try:
        yield sse("session", {"session_id": session_id})

        # Initial tree (root only)
        for evt in tree.start():
            yield sse(evt["type"], evt)

        yield sse("think_start", {})

        async for event in stream_agent_events(
            agent=agent,
            messages=state.messages,
            cancel_event=state.cancel_event,
            session_id=session_id,
        ):
            if event.name == "cancelled":
                yield sse(event.name, event.payload)
                return

            if event.name == "token":
                for evt in tree.handle_token(event.payload["text"]):
                    if evt["type"] == "think_token":
                        yield sse("think_token", {"text": evt["text"]})
                    elif evt["type"] == "token":
                        assistant_text.append(evt["text"])
                        yield sse("token", {"text": evt["text"]})
                    elif evt["type"] == "tree_snapshot":
                        yield sse("tree_snapshot", evt)

            elif event.name == "tool_call":
                for evt in tree.handle_tool_call(event.payload["tool_calls"]):
                    if evt["type"] == "tree_snapshot":
                        yield sse("tree_snapshot", evt)

            elif event.name == "update":
                for evt in tree.handle_update(
                    event.payload.get("data", {}),
                    event.payload.get("namespace", []),
                ):
                    if evt["type"] == "tree_snapshot":
                        yield sse("tree_snapshot", evt)

        # Finalize
        for evt in tree.finalize():
            if evt["type"] == "token":
                assistant_text.append(evt["text"])
            yield sse(evt["type"], evt)

        content = "".join(assistant_text).strip()
        if content:
            state.messages.append(AIMessage(content=content))
            state.messages = state.messages[-settings.max_history_messages :]
        yield sse("done", {"session_id": session_id})
    except asyncio.CancelledError:
        state.cancel_event.set()
        return
    except Exception as exc:
        yield sse(
            "error",
            {
                "message": str(exc),
                "hint": (
                    "确认 LM Studio 服务已启动，并且 DIAGNOSTICS_BASE_URL 指向 "
                    "OpenAI-compatible 地址；如果启用了认证，请设置 "
                    "DIAGNOSTICS_API_KEY、LM_STUDIO_API_KEY 或 LM_API_TOKEN。"
                ),
            },
        )
    finally:
        state.running = False
