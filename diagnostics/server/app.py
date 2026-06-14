from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import AsyncIterator
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from langchain_core.messages import AIMessage, HumanMessage

from diagnostics.agent import build_agent
from diagnostics.agent.prompt import make_system_prompt
from diagnostics.agent.streaming import stream_agent_events
from diagnostics.config import STATIC_DIR, Settings
from diagnostics.server.schemas import ChatRequest
from diagnostics.server.sessions import SessionStore
from diagnostics.server.skills_db import get_all_skills, init_db, sync_skills_from_disk
from diagnostics.server.sse import sse
from diagnostics.server.step_tracker import TreeBuilder

logger = logging.getLogger(__name__)


def create_app(settings: Settings | None = None, agent: Any | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    agent = agent or build_agent(settings)
    sessions = SessionStore()

    # Initialize skills database on startup
    init_db()
    sync_skills_from_disk()

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

    @app.get("/api/skills")
    async def list_skills() -> list[dict[str, str]]:
        """Return all diagnostic skills for the frontend / command picker."""
        return get_all_skills()

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
            _chat_event_stream(request, session_id, state, settings, build_agent),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    return app


def _make_report_path(entity_type: str, entity_name: str) -> str:
    """Generate a UUID-based report path from entity info."""
    import uuid as _uuid
    from datetime import datetime as _dt

    safe_entity = entity_name.replace("/", "-").replace(" ", "-").strip()
    if not safe_entity:
        return ""
    ts = _dt.now().strftime("%Y-%m-%d-%H%M%S")
    uid = _uuid.uuid4().hex[:8]
    return f"/agent_data/reports/{entity_type}/{safe_entity}/{ts}-{uid}.md"


async def _chat_event_stream(
    request: ChatRequest,
    session_id: str,
    state: Any,
    settings: Settings,
    agent_factory: Any,
) -> AsyncIterator[str]:
    t_start = time.monotonic()
    state.running = True
    state.cancel_event = asyncio.Event()

    # ── Generate report path and build agent with formatted prompt ──
    report_path = _make_report_path(request.entity_type, request.entity_name)
    agent = agent_factory(settings, system_prompt=make_system_prompt(report_path))

    state.messages.append(HumanMessage(content=request.message))
    state.messages = state.messages[-settings.max_history_messages :]
    assistant_text: list[str] = []
    tree = TreeBuilder()

    logger.info("[session=%s] Chat stream started, message=%r",
                session_id, request.message[:120])
    event_counters: dict[str, int] = {}

    try:
        yield sse("session", {"session_id": session_id})

        # Initialize tree with root node
        for snap in tree.start():
            yield sse("tree_snapshot", snap)

        async for event in stream_agent_events(
            agent=agent,
            messages=state.messages,
            cancel_event=state.cancel_event,
            session_id=session_id,
        ):
            if event.name in ("cancelled", "error"):
                logger.warning("[session=%s] Stream interrupted: %s", session_id, event.name)
                for snap in tree.finalize():
                    if snap["type"] == "tree_snapshot":
                        yield sse("tree_snapshot", snap)
                yield sse(event.name, event.payload)
                return

            event_counters[event.name] = event_counters.get(event.name, 0) + 1

            if event.name == "text_delta":
                yield sse("text_delta", event.payload)
                path_val = event.payload.get("path", [])
                if isinstance(path_val, list) and len(path_val) > 0 and path_val[0] == "coordinator":
                    assistant_text.append(event.payload["text"])
                elif isinstance(path_val, str) and path_val == "coordinator":
                    assistant_text.append(event.payload["text"])
                # Feed text tokens to tree with round number.
                # Round changes detected here drive phase creation — one phase
                # per LLM invocation.
                for tok_evt in tree.handle_token(
                    event.payload.get("text", ""),
                    round_num=event.payload.get("round", 0),
                ):
                    if tok_evt.get("type") == "tree_snapshot":
                        yield sse("tree_snapshot", tok_evt)

            elif event.name == "tool_start":
                description = event.payload.get("description", "")

                # Feed tool call to tree with description
                tool_calls = [{
                    "id": event.payload["id"],
                    "name": event.payload["name"],
                    "args": event.payload.get("args", {}),
                    "description": description,
                }]
                for snap in tree.handle_tool_call(tool_calls):
                    if snap.get("type") == "tree_snapshot":
                        yield sse("tree_snapshot", snap)

                yield sse("tool_start", event.payload)

            elif event.name == "tool_end":
                # Feed tool completion to tree
                for snap in tree.handle_update(
                    {"tool_call_id": event.payload["id"]},
                    namespace=["tools"],
                ):
                    if snap.get("type") == "tree_snapshot":
                        yield sse("tree_snapshot", snap)

                yield sse("tool_end", event.payload)

            elif event.name == "tool_args_available":
                for snap in tree.update_tool_args(
                    event.payload["id"],
                    event.payload.get("name", ""),
                    event.payload.get("args", {}),
                ):
                    if snap.get("type") == "tree_snapshot":
                        yield sse("tree_snapshot", snap)

            elif event.name in ("agent_start", "agent_end"):
                yield sse(event.name, event.payload)

            elif event.name == "structured_response":
                logger.info("[session=%s] Structured response received", session_id)
                yield sse("structured_response", event.payload)

        # Finalize tree
        for snap in tree.finalize():
            if snap.get("type") == "tree_snapshot":
                yield sse("tree_snapshot", snap)
            elif snap.get("type") == "token":
                # Fallback answer text from tree — emit as answering phase
                yield sse("text_delta", {
                    "text": snap["text"],
                    "path": ["coordinator"],
                    "round": 999,
                    "phase": "answering",
                })

        content = "".join(assistant_text).strip()
        if content:
            state.messages.append(AIMessage(content=content))
            state.messages = state.messages[-settings.max_history_messages :]

        elapsed = time.monotonic() - t_start
        logger.info("[session=%s] Stream completed in %.1fs, events: %s, assistant_text=%d chars",
                    session_id, elapsed, dict(event_counters), len(content))
        yield sse("done", {"session_id": session_id})

    except asyncio.CancelledError:
        logger.info("[session=%s] Stream cancelled by server", session_id)
        state.cancel_event.set()
        return
    except Exception as exc:
        elapsed = time.monotonic() - t_start
        logger.exception("[session=%s] Stream error after %.1fs: %s",
                         session_id, elapsed, exc)
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
