from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from langchain_core.messages import AIMessage, HumanMessage

from diagnostics.agent import build_agent
from diagnostics.agent.prompt import make_system_prompt
from diagnostics.agent.streaming import stream_agent_events
from diagnostics.config import ROOT_DIR, STATIC_DIR, Settings
from diagnostics.server.schemas import ChatRequest
from diagnostics.server.sessions import SessionStore
from diagnostics.server.skills_db import get_all_skills, init_db, sync_skills_from_disk
from diagnostics.server.sse import sse
from diagnostics.server.step_tracker import StepStatus, TreeBuilder
from diagnostics.server.trace_writer import TraceWriter

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

    @app.middleware("http")
    async def no_cache_static(request, call_next):
        # Force revalidation on every load for the HTML shell and static
        # assets (ETag/Last-Modified make unchanged files a cheap 304).
        # Prevents stale JS after frontend changes when the page is kept open.
        response = await call_next(request)
        if request.url.path == "/" or request.url.path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-cache"
        return response

    @app.get("/favicon.ico")
    async def favicon():
        return FileResponse(STATIC_DIR / "favicon.svg", media_type="image/svg+xml")

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

    @app.get("/api/history")
    async def list_history() -> list[dict[str, Any]]:
        """List completed diagnostic sessions with metadata."""
        return _list_history()

    @app.get("/api/history/{result_file}")
    async def get_history(result_file: str) -> dict[str, Any]:
        """Return a saved diagnostic result (graph + metadata)."""
        reports_root = ROOT_DIR / "agent_data" / "reports"
        for f in reports_root.rglob(result_file):
            if f.name == result_file and f.suffix == ".result.json":
                return json.loads(f.read_text(encoding="utf-8"))
        raise HTTPException(status_code=404, detail="result not found")

    @app.get("/api/report/{report_file:path}")
    async def get_report(report_file: str) -> dict[str, str]:
        """Return the raw report markdown content."""
        p = ROOT_DIR / "agent_data" / "reports" / report_file
        if not p.is_file():
            raise HTTPException(status_code=404, detail="report not found")
        return {"content": p.read_text(encoding="utf-8")}

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


def _auto_set_scenario(message: str) -> None:
    """Match user message to mock scenario using declarative routing.

    Delegates to scenarios.match_scenario() which auto-sorts keyword rules
    by specificity (more keywords = higher priority).  Adding or removing
    scenarios only requires editing scenarios.py — no changes here.
    """
    from diagnostics.tools.mock.scenarios import match_scenario, set_scenario

    matched = match_scenario(message)
    if matched:
        try:
            set_scenario(matched)
            logger.info("Mock scenario auto-set to %r", matched)
        except Exception:
            pass


def _build_augmented_message(
    user_message: str,
    param_overrides: dict | None,
    entity_type: str,
    entity_name: str,
    hostname: str,
    start_time: str,
    end_time: str,
) -> str:
    """Prepend diagnostic parameters to the user message for the Coordinator.

    The Coordinator needs cluster_name / hostname / time window in its first
    HumanMessage so it can immediately delegate to argus experts without
    wasting rounds guessing parameters or searching non-existent config files.
    """
    if not param_overrides and not hostname and not entity_name:
        return user_message

    lines: list[str] = ["## 诊断参数\n"]

    if entity_name and entity_type == "kubernetes":
        lines.append(f"- 集群: {entity_name}")
    elif entity_name and entity_type == "hosts" and not hostname:
        lines.append(f"- 主机名: {entity_name}")
    if hostname:
        lines.append(f"- 主机名: {hostname}")
    _ns = (param_overrides or {}).get("namespace", "")
    if _ns:
        lines.append(f"- 命名空间: {_ns}")
    _wl_type = (param_overrides or {}).get("workload_type", "")
    _wl_name = (param_overrides or {}).get("workload_name", "")
    if _wl_type and _wl_name:
        lines.append(f"- 工作负载: {_wl_type}/{_wl_name}")
    elif _wl_name:
        lines.append(f"- 工作负载: {_wl_name}")
    _pod = (param_overrides or {}).get("pod_name", "")
    if _pod:
        lines.append(f"- Pod: {_pod}")
    if start_time and end_time:
        lines.append(f"- 故障时间: {start_time} ~ {end_time}")
    elif start_time:
        lines.append(f"- 故障时间起点: {start_time}")

    lines.append("\n---\n")
    lines.append(f"用户问题: {user_message}")
    return "\n".join(lines)


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


def _make_ledger_path(report_path: str) -> str:
    """Generate a ledger JSON path matching the report path prefix.

    e.g. /agent_data/reports/.../2026-06-25-103000-a1b2c3d4.md
      → /agent_data/traces/2026-06-25-103000-a1b2c3d4-ledger.json
    """
    if not report_path:
        return ""
    stem = report_path.rsplit("/", 1)[-1].rsplit(".", 1)[0]  # 2026-06-25-103000-a1b2c3d4
    return f"/agent_data/traces/{stem}-ledger.json"


def _save_result(
    report_path: str,
    tree: TreeBuilder,
    entity_type: str,
    entity_name: str,
    duration: float,
    event_counters: dict[str, int],
    assistant_text_chars: int,
    ledger: dict[str, Any] | None = None,
    content: str = "",
) -> None:
    """Save diagnostic result: tree graph + metadata + ledger as JSON, report as .md."""
    stem = report_path.rsplit(".", 1)[0] if report_path.endswith(".md") else report_path
    report_dir = Path(ROOT_DIR / stem.lstrip("/")).parent
    report_dir.mkdir(parents=True, exist_ok=True)

    # Save .result.json (tree + metadata)
    result_path = Path(ROOT_DIR / stem.lstrip("/")).with_suffix(".result.json")
    data: dict[str, Any] = {
        "entity_type": entity_type,
        "entity_name": entity_name,
        "duration_secs": round(duration, 1),
        "assistant_text_chars": assistant_text_chars,
        "event_counts": dict(event_counters),
        "tree": {"steps": _serialize_tree(tree)},
    }
    # ── Performance metrics ──
    if ledger:
        metrics = ledger.get("metrics") or {}
        if metrics:
            data["metrics"] = metrics
    if ledger:
        # The ledger object arriving here is the ledger_snapshot EVENT
        # payload — streaming._sanitize_ledger_for_event injects a derived
        # "current_phase" for the frontend banner.  The persisted result
        # must store the RAW ledger (phase is derived, not persisted;
        # state-machine-v2 §10), so strip the injected key.
        data["ledger"] = {
            k: v for k, v in ledger.items() if k != "current_phase"
        }
    result_path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    logger.info("Result saved to %s", result_path)

    # Save .md (report content)
    # Primary source: text_delta answering-phase stream (``content``).
    # Fallback: ledger["report"] captured by middleware write_file handler.
    # This ensures the .md is always written at the canonical report_path
    # even when the LLM uses write_file (producing no answering text_delta)
    # or alters the filename (e.g. strips HHMMSS).
    md_content = content
    if not md_content and ledger:
        try:
            md_content = ledger.get("report") or ""
        except Exception:
            pass
    if md_content:
        md_path = Path(ROOT_DIR / stem.lstrip("/")).with_suffix(".md")
        md_path.write_text(md_content, encoding="utf-8")
        logger.info("Report saved to %s (%d chars, source=%s)",
                     md_path, len(md_content),
                     "text_delta" if content else "ledger.report")


def _serialize_tree(tree: TreeBuilder) -> list[dict[str, Any]]:
    """Extract tree nodes as a serializable list."""
    import datetime as _dt
    steps: list[dict[str, Any]] = []
    for nid in tree.node_order:
        if nid in tree.nodes:
            node = tree.nodes[nid]
            steps.append({
                "id": node.id,
                "title": node.title,
                "parent_id": node.parent_id,
                "parent_ids": node.parent_ids,
                "status": node.status.value,
                "node_type": node.node_type.value,
                "description": node.description,
                "tool_name": node.tool_name,
                "tool_args": node.tool_args,
            })
    return steps


def _list_history() -> list[dict[str, Any]]:
    """Scan reports/ for completed sessions, return sorted by time descending."""
    reports_root = ROOT_DIR / "agent_data" / "reports"
    results: list[dict[str, Any]] = []
    seen_stems: set[str] = set()
    if not reports_root.is_dir():
        return results

    # Scan result.json files first (full metadata + graph)
    for rf in sorted(reports_root.rglob("*.result.json"), reverse=True):
        try:
            data = json.loads(rf.read_text(encoding="utf-8"))
            rel = rf.relative_to(reports_root)
            report_rel = str(rel).replace(".result.json", ".md")
            data["file"] = rf.name
            data["report_file"] = report_rel
            stem = rf.name.replace(".result.json", "")
            seen_stems.add(stem)
            results.append(data)
        except (json.JSONDecodeError, KeyError):
            continue

    # Also include report .md files without result.json (fallback — no graph)
    for mf in sorted(reports_root.rglob("*.md"), reverse=True):
        stem = mf.name.replace(".md", "")
        if stem in seen_stems:
            continue
        try:
            rel = mf.relative_to(reports_root)
            parts = rel.parts
            entity_type = parts[0] if len(parts) > 1 else "?"
            entity_name = parts[1] if len(parts) > 2 else "?"
            mtime = mf.stat().st_mtime
            results.append({
                "entity_type": entity_type,
                "entity_name": entity_name,
                "duration_secs": None,
                "assistant_text_chars": None,
                "event_counts": {},
                "tree": None,
                "file": "",
                "report_file": str(rel),
                "_mtime": mtime,
            })
        except Exception:
            continue

    # Sort: result.json entries first (they have duration), then by mtime
    results.sort(key=lambda r: (
        0 if r.get("duration_secs") is not None else 1,
        -(r.get("duration_secs") or r.get("_mtime") or 0),
    ))
    for r in results:
        r.pop("_mtime", None)
    return results[:20]


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

    # ── Auto-match mock scenario from user message (dev mode only) ──
    if os.getenv("DIAGNOSTICS_MODE", "mock") == "mock":
        _auto_set_scenario(request.message)

    # ── Pre-resolve hostname for container scenarios ──
    # host-argus-expert needs a concrete hostname to query host-level
    # Argus metrics (CPU/memory/disk/network).  In container scenarios,
    # the frontend only provides K8s-level params — resolve the underlying
    # host node name from the workload info and pass it to the agent.
    hostname = ""
    start_time = ""
    end_time = ""
    if request.param_overrides:
        from diagnostics.agent.hostname_resolver import resolve_hostnames
        request.param_overrides = resolve_hostnames(request.param_overrides)
        hostname = (request.param_overrides or {}).get("hostname", "")
        time_range = (request.param_overrides or {}).get("fault_time_range", {}) or {}
        start_time = time_range.get("start_time", "")
        end_time = time_range.get("end_time", "")

    # ── Build the full user message with injected diagnostic parameters ──
    # The Coordinator must see cluster_name / hostname / time window in its
    # first HumanMessage, otherwise it will waste rounds guessing or searching
    # for config files (e.g. glob /agent_data/configs/**).
    augmented_message = _build_augmented_message(
        request.message,
        request.param_overrides,
        request.entity_type,
        request.entity_name,
        hostname,
        start_time,
        end_time,
    )

    # ── Generate report/ledger paths and build agent with formatted prompt ──
    report_path = _make_report_path(request.entity_type, request.entity_name)
    ledger_path = _make_ledger_path(report_path)
    agent = agent_factory(
        settings,
        system_prompt=make_system_prompt(report_path, ledger_path),
        report_path=report_path,
        ledger_path=ledger_path,
        entity_type=request.entity_type,
        hostname=hostname,
        entity_name=request.entity_name,
        start_time=start_time,
        end_time=end_time,
        param_overrides=request.param_overrides,
    )

    state.messages.append(HumanMessage(content=augmented_message))
    state.messages = state.messages[-settings.max_history_messages :]
    assistant_text: list[str] = []
    report_text: list[str] = []   # only answering-phase text → .md file
    tree = TreeBuilder()
    latest_ledger: dict[str, Any] | None = None  # tracks latest ledger snapshot
    final_root_cause: str | None = None           # authoritative from structured_response

    logger.info("[session=%s] Chat stream started, message=%r",
                session_id, request.message[:120])
    event_counters: dict[str, int] = {}

    # ── Trace writer: save tool calls, LLM text, results per round ──
    trace = TraceWriter(
        report_path,
        entity_type=request.entity_type,
        entity_name=request.entity_name,
    )
    trace.start(user_message=request.message)

    def _tree_sse(snap: dict[str, Any]) -> str:
        """Dispatch tree event with correct SSE type (snapshot or delta)."""
        return sse(snap.get("type", "tree_snapshot"), snap)

    try:
        yield sse("session", {"session_id": session_id})

        # Initialize tree with root node
        for snap in tree.start():
            yield _tree_sse(snap)

        async for event in stream_agent_events(
            agent=agent,
            messages=state.messages,
            cancel_event=state.cancel_event,
            session_id=session_id,
        ):
            if event.name in ("cancelled", "error"):
                logger.warning("[session=%s] Stream interrupted: %s", session_id, event.name)
                for snap in tree.finalize():
                    yield _tree_sse(snap)
                trace.finalize()
                yield sse(event.name, event.payload)
                return

            event_counters[event.name] = event_counters.get(event.name, 0) + 1

            if event.name == "text_delta":
                yield sse("text_delta", event.payload)
                path_val = event.payload.get("path", [])
                is_coordinator = (
                    (isinstance(path_val, list) and len(path_val) > 0 and path_val[0] == "coordinator")
                    or (isinstance(path_val, str) and path_val == "coordinator")
                )
                if is_coordinator:
                    assistant_text.append(event.payload["text"])
                    trace.llm_text(event.payload.get("round", 0), event.payload["text"])
                    # Separate report content (answering phase) for .md file
                    if event.payload.get("phase") == "answering":
                        report_text.append(event.payload["text"])
                # Feed text tokens to tree for think/answer buffer.
                for tok_evt in tree.handle_token(
                    event.payload.get("text", ""),
                ):
                    if tok_evt.get("type") in ("tree_snapshot", "tree_delta"):
                        yield _tree_sse(tok_evt)

            elif event.name == "round_start":
                # New LLM round → create a new Phase node in the tree
                for snap in tree.handle_round_start(event.payload.get("round", 0)):
                    yield _tree_sse(snap)

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
                    yield _tree_sse(snap)

                # Trace: tool start
                from diagnostics.server.step_tracker import format_tool_args
                trace.tool_start(
                    event.payload.get("round", 0),
                    event.payload["name"],
                    format_tool_args(event.payload.get("args", {}), event.payload["name"]),
                    description,
                    tool_call_id=event.payload.get("id", ""),
                )

                yield sse("tool_start", event.payload)

            elif event.name == "tool_end":
                # Deduped nodes are KEPT in the tree with a "dedup" badge:
                # the dedup middleware served a cached preview, and removing
                # the node after it appeared caused visible flicker.
                update = {"tool_call_id": event.payload["id"]}
                if event.payload.get("_deduped"):
                    update["dedup"] = True
                # Feed tool completion to tree (now emits delta, not full snapshot)
                for snap in tree.handle_update(update, namespace=["tools"]):
                    yield _tree_sse(snap)

                # Trace: tool result
                trace.tool_end(
                    event.payload.get("round", 0),
                    event.payload.get("name", ""),
                    event.payload.get("output_full", ""),
                    tool_call_id=event.payload.get("id", ""),
                )

                yield sse("tool_end", event.payload)

            elif event.name == "tool_args_available":
                for snap in tree.update_tool_args(
                    event.payload["id"],
                    event.payload.get("name", ""),
                    event.payload.get("args", {}),
                ):
                    yield _tree_sse(snap)

            elif event.name in ("agent_start", "agent_end"):
                yield sse(event.name, event.payload)

            elif event.name == "structured_response":
                logger.info("[session=%s] Structured response received", session_id)
                final_root_cause = event.payload.get("root_cause") or final_root_cause
                yield sse("structured_response", event.payload)

            elif event.name == "ledger_snapshot":
                latest_ledger = event.payload.get("ledger")
                trace.ledger_update(latest_ledger)
                yield sse("ledger_snapshot", event.payload)

            # Custom events from middleware hooks (before_model / awrap_tool_call)
            elif event.name == "round_transition":
                # before_model hook: new LLM round detected by middleware
                for snap in tree.handle_round_start(event.payload.get("round", 0)):
                    yield _tree_sse(snap)

            elif event.name == "tool_executing":
                # awrap_tool_call: tool about to run (precise timing)
                yield sse("tool_executing", event.payload)

            elif event.name == "tool_completed":
                # awrap_tool_call: tool just finished (precise timing)
                yield sse("tool_completed", event.payload)

            elif event.name == "error":
                # Producer-side graph failure (e.g. node exception) —
                # forward so the frontend can surface it instead of
                # silently waiting for a stream that will never produce
                # further events.
                logger.error(
                    "[session=%s] Stream error: %s",
                    session_id, event.payload.get("error", ""),
                )
                yield sse("error", event.payload)

        # Finalize tree
        for snap in tree.finalize():
            if snap.get("type") in ("tree_snapshot", "tree_delta"):
                yield _tree_sse(snap)
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

        # Report .md file: only answering-phase text (captured from write_file)
        report_content = "".join(report_text).strip()

        # If structured_response arrived with a root_cause that differs from
        # the ledger's eagerly-snapshotted value, overwrite it.  The LLM may
        # change its conclusion between hypothesis confirmation and final report.
        if final_root_cause and latest_ledger and latest_ledger.get("root_cause") != final_root_cause:
            logger.info("[session=%s] Overwriting ledger root_cause: %r -> %r",
                        session_id, latest_ledger.get("root_cause"), final_root_cause)
            latest_ledger["root_cause"] = final_root_cause
            yield sse("ledger_snapshot", {"ledger": latest_ledger})

        elapsed = time.monotonic() - t_start
        logger.info("[session=%s] Stream completed in %.1fs, events: %s, assistant_text=%d chars, report=%d chars",
                    session_id, elapsed, dict(event_counters), len(content), len(report_content))
        _save_result(report_path, tree,
                     request.entity_type, request.entity_name,
                     elapsed, event_counters, len(content),
                     ledger=latest_ledger, content=report_content)
        trace.finalize()
        yield sse("done", {"session_id": session_id})

    except asyncio.CancelledError:
        logger.info("[session=%s] Stream cancelled by server", session_id)
        state.cancel_event.set()
        for snap in tree.finalize():
            yield _tree_sse(snap)
        trace.finalize()
        return
    except Exception as exc:
        elapsed = time.monotonic() - t_start
        logger.exception("[session=%s] Stream error after %.1fs: %s",
                         session_id, elapsed, exc)
        for snap in tree.finalize():
            yield _tree_sse(snap)
        # ── Fallback: save whatever we have ──
        # The graph may crash after the timeout handler fires (e.g.
        # KeyError on orphaned tool_call_ids during END routing).
        # Save .result.json with whatever ledger/tree data exists so
        # the frontend can still render a partial diagnosis tree and
        # the user doesn't lose all evidence.
        try:
            _save_result(report_path, tree,
                         request.entity_type, request.entity_name,
                         elapsed, event_counters, len("".join(assistant_text)),
                         ledger=latest_ledger, content="".join(report_text))
        except Exception:
            pass  # best-effort; don't mask the original error
        trace.finalize()
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
