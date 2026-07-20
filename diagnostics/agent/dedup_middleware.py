"""Tool dedup middleware — prevent duplicate tool calls within and across agents.

This middleware:
1. P0: In-memory cache — blocks identical (name, args) calls in same session
2. P1: In-flight dedup — concurrent identical calls wait for the first instead
   of executing in parallel (handles LangGraph ToolNode parallel execution)
3. Circuit breaker — auto-escalates after consecutive failures of same tool
4. Cross-agent dedup — persists results to shared backend so subagents and
   Coordinator can share cached results

Extracted from DiagnosisLedgerMiddleware to keep ledger focused on
hypothesis management.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from collections.abc import Awaitable, Callable

from langchain.agents.middleware.types import AgentMiddleware
from langchain_core.messages import ToolMessage
from langgraph.prebuilt.tool_node import ToolCallRequest
from langgraph.types import Command

from deepagents.backends.protocol import BackendProtocol, ReadResult

logger = logging.getLogger(__name__)

# ── Constants ──────────────────────────────────────────────────────────────

_DEDUP_CACHE_PREFIX = "/_dedup_cache"
"""Shared backend path prefix for cross-agent dedup keys."""

_TOOL_FAILURE_BREAKER = int(os.getenv("DIAGNOSTICS_TOOL_FAILURE_BREAKER", "2"))
"""Consecutive failures of same tool → auto-block retry."""

# ── Pattern: transport/network-level tool failures ─────────────────────────

_TOOL_FAILURE_PATTERNS = re.compile(
    r"timeout|timed out|connection refused|connection reset|"
    r"network (is )?unreachable|no route to host|"
    r"OSP.*(?:fail|error|timeout)|remote.*(?:fail|error|timeout)|"
    r"channel closed|broken pipe",
    re.IGNORECASE,
)

# ── Pattern: empty / nil / error results that should NOT be cached ─────────
# Caching a "not found" or empty result would lock the LLM into a failure
# loop — every subsequent round would hit the cache and see the same useless
# data, preventing recovery.

_EMPTY_PATTERNS = re.compile(
    r"not found|no data|no results|error|空",
    re.IGNORECASE,
)

# ── Tools that should never be deduplicated ────────────────────────────────
# Scaffolding, ledger-management, and delegation tools are intentionally
# excluded from dedup — they are not diagnostic data collection tools.

_SCAFFOLDING_TOOLS = frozenset({
    "write_file", "read_file", "edit_file",
    "write_todos", "read_todos",
    "ls", "glob", "grep",
})

_LEDGER_TOOLS = frozenset({
    "commit_hypotheses", "select_path", "record_finding", "backtrack",
})

_DEDUP_SKIP = _SCAFFOLDING_TOOLS | _LEDGER_TOOLS | {"task"}

# ── Helpers ────────────────────────────────────────────────────────────────

_UNSAFE_PATH_RE = re.compile(r'[^a-zA-Z0-9_.-]+')


def _sanitize_path_component(name: str) -> str:
    """Convert a tool name to a safe filesystem path component."""
    return _UNSAFE_PATH_RE.sub("_", name)[:60]


def _make_cache_key(tool_name: str, args: dict) -> str:
    """Generate a deterministic cache key from tool name + args.

    Args are serialized with sort_keys=True so that dict key order does
    not cause false cache misses.
    """
    canonical = json.dumps(args, sort_keys=True) if args else "{}"
    return f"{tool_name}:{canonical}"


def _is_tool_failure(content: str) -> bool:
    """Return True if tool output indicates a transport/network failure.

    Diagnostic data often contains words like 'timeout' or 'error'
    when describing the fault being investigated — these must NOT be
    mistaken for tool failures.  A genuine tool failure is a short,
    unstructured message; diagnostic output is long, multi-line,
    and contains headers/tables.
    """
    if not content:
        return False
    # Only short content can be a failure message (diagnostic data
    # is typically >500 chars with multiple lines of structured output).
    if len(content) > 500:
        return False
    # Multiple lines with structure → diagnostic data, not a failure
    if content.count('\n') > 5:
        return False
    # Has markdown-style headers or table separators → diagnostic data
    if '##' in content or '|-' in content or '| ' in content:
        return False
    return bool(_TOOL_FAILURE_PATTERNS.search(content))


# ═══════════════════════════════════════════════════════════════════════════
# Middleware
# ═══════════════════════════════════════════════════════════════════════════

class ToolDedupMiddleware(AgentMiddleware):
    """Tool-level deduplication with cross-agent cache sharing.

    Must be placed BEFORE DiagnosisLedgerMiddleware and
    ToolOffloadMiddleware in the pipeline so that cache hits bypass
    unnecessary downstream processing.

    Attributes:
        _tool_call_cache: In-memory cache, keyed by tool_name + sorted
            args JSON.  Value is (ToolMessage, failure_count): 0 = success,
            >0 = consecutive failures.
        _in_flight: Tracks currently executing calls so that concurrent
            identical calls (from single LLM response) wait for the first
            instead of executing in parallel.
    """

    def __init__(self, backend: BackendProtocol | None = None) -> None:
        self._backend = backend
        # In-memory cache shared across agents via for_subagent()
        self._tool_call_cache: dict[str, tuple[ToolMessage, int]] = {}
        # In-flight futures shared across agents via for_subagent()
        self._in_flight: dict[str, asyncio.Future] = {}
        logger.info(
            "ToolDedupMiddleware: backend=%s, breaker=%d",
            "enabled" if backend else "disabled",
            _TOOL_FAILURE_BREAKER,
        )

    @classmethod
    def for_subagent(cls, coordinator: "ToolDedupMiddleware") -> "ToolDedupMiddleware":
        """Create a subagent instance that shares the Coordinator's cache.

        Subagents and the Coordinator share the same in-memory cache
        and in-flight tracker so that a tool call made by the Coordinator
        is deduplicated when a subagent tries the same call, and vice versa.
        """
        instance = cls(backend=coordinator._backend)
        instance._tool_call_cache = coordinator._tool_call_cache
        instance._in_flight = coordinator._in_flight
        return instance

    # ── Tool call interception ─────────────────────────────────────────

    async def _execute_and_cache(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command]],
        cache_key: str,
    ) -> ToolMessage | Command:
        """Execute tool call and populate cache for both memory and backend."""
        result = await handler(request)

        if isinstance(result, ToolMessage):
            content = result.content if isinstance(result.content, str) else str(result.content)

            if not content or (len(content) < 50 and _EMPTY_PATTERNS.search(content)):
                logger.debug("Skipped caching empty/error result: %s", cache_key)
            else:
                fail_count = 1 if _is_tool_failure(content) else 0
                self._tool_call_cache[cache_key] = (result, fail_count)

            # Persist to shared_backend for cross-agent dedup
            if self._backend and content and not (len(content) < 50 and _EMPTY_PATTERNS.search(content)):
                backend_key = f"{_DEDUP_CACHE_PREFIX}/{cache_key}"
                try:
                    await self._backend.awrite(backend_key, content)
                except Exception:
                    pass  # best-effort; memory cache still works

        return result

    def _dedup_hit_result(
        self,
        cache_key: str,
        tool_call_id: str,
        tool_name: str,
        full_content: str,
    ) -> ToolMessage:
        """Build the dedup-hit ToolMessage: short preview + retrieval path.

        The full result stays in the in-memory/backend cache.  Returning
        only a preview keeps duplicate content out of the LLM context.
        The deterministic backend path lets the agent re-read the full
        result when the preview is insufficient — this matters for
        cross-agent hits, where the original result is NOT present in
        this agent's message history (each task() delegation starts a
        fresh subagent context).
        """
        preview = full_content[:300].rstrip()
        if len(full_content) > 300:
            preview += "…"
        backend_path = f"{_DEDUP_CACHE_PREFIX}/{cache_key}"
        return ToolMessage(
            content=(
                f"[系统去重] {tool_name} 与此前一次调用的参数完全相同，"
                "本次未重复执行。结果开头预览：\n"
                f"---\n{preview}\n---\n"
                f"完整结果（{len(full_content)} 字符）与此前一致"
                "（若上方历史消息中已有该结果，直接引用即可）；"
                f"如需全文可 read_file \"{backend_path}\"。"
            ),
            tool_call_id=tool_call_id,
            name=tool_name,
        )

    @staticmethod
    def _emit_dedup_event(tool_call_id: str) -> None:
        """Notify frontend to suppress the duplicate tool call node.

        The frontend creates tree nodes from tool_start events (pre-middleware),
        so deduped calls still get visible nodes.  This custom event tells
        the streaming layer to remove the redundant node.
        """
        try:
            from langgraph.config import get_stream_writer
            writer = get_stream_writer()
            writer({"type": "tool_dedup", "id": tool_call_id})
        except RuntimeError:
            pass  # stream_writer unavailable outside ToolNode context

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command]],
    ) -> ToolMessage | Command:
        tool_name = request.tool_call.get("name", "")
        tool_call_id = request.tool_call.get("id", "")
        tool_args = request.tool_call.get("args", {})

        # Only deduplicate diagnostic tools.
        if tool_name in _DEDUP_SKIP:
            return await handler(request)

        cache_key = _make_cache_key(tool_name, tool_args)

        # ── P0: In-memory cache lookup + circuit breaker ──
        if cache_key in self._tool_call_cache:
            cached_msg, fail_count = self._tool_call_cache[cache_key]
            if fail_count == 0:
                logger.info("去重命中: %s", cache_key)
                self._emit_dedup_event(tool_call_id)
                full = cached_msg.content if isinstance(
                    cached_msg.content, str) else str(cached_msg.content)
                return self._dedup_hit_result(
                    cache_key, tool_call_id, cached_msg.name or tool_name,
                    full,
                )
            # Previous call(s) failed → this is a retry
            if fail_count >= _TOOL_FAILURE_BREAKER:
                logger.warning("熔断: %s (连续失败%d次)", cache_key, fail_count)
                return ToolMessage(
                    content=(
                        f"⛔ [系统熔断] {tool_name} 已连续失败"
                        f"{fail_count}次，网络可能不可达。"
                        "禁止重试此工具，请基于已有证据继续诊断。"
                    ),
                    tool_call_id=tool_call_id,
                    name=cached_msg.name,
                )
            # Allow one retry with a warning, increment counter
            logger.info("失败重试: %s (第%d次)", cache_key, fail_count + 1)
            self._tool_call_cache[cache_key] = (cached_msg, fail_count + 1)
            return ToolMessage(
                content=(
                    f"{cached_msg.content}\n\n"
                    f"⚠ [系统提示] {tool_name} 上次调用返回异常，"
                    f"当前为第{fail_count + 1}次重试。"
                    f"若累计失败{_TOOL_FAILURE_BREAKER}次将被熔断。"
                ),
                tool_call_id=tool_call_id,
                name=cached_msg.name,
            )

        # ── P1: In-flight dedup — concurrent duplicate → skip ──
        # When the LLM generates multiple identical tool calls in a single
        # response, only the first executes.  Duplicates return a minimal
        # skip message so the LLM context is not polluted with repeated data.
        if cache_key in self._in_flight:
            logger.info("去重跳过（并发）: %s", cache_key)
            self._emit_dedup_event(tool_call_id)
            # Still wait for the first call to complete so its result is
            # cached for future cross-round dedup.
            try:
                await self._in_flight[cache_key]
            except Exception:
                pass  # first call failed; its result will be handled by P0
            return ToolMessage(
                content=(
                    f"[系统] {tool_name} 重复调用已跳过，"
                    f"同批次中已有相同参数的调用，结果以首次调用为准。"
                ),
                tool_call_id=tool_call_id,
                name=tool_name,
            )

        # ── Cross-agent dedup via shared_backend ──
        if self._backend:
            backend_key = f"{_DEDUP_CACHE_PREFIX}/{cache_key}"
            try:
                cached_content = await self._backend.aread(backend_key)
            except Exception:
                cached_content = None
            if cached_content is not None and not (
                isinstance(cached_content, ReadResult) and cached_content.error
            ):
                logger.info(
                    "跨Agent去重命中: %s (from shared_backend)", cache_key,
                )
                self._emit_dedup_event(tool_call_id)
                restored = ToolMessage(
                    content=cached_content,
                    tool_call_id=tool_call_id,
                    name=tool_name,
                )
                self._tool_call_cache[cache_key] = (restored, 0)
                full = cached_content if isinstance(
                    cached_content, str) else str(cached_content)
                return self._dedup_hit_result(
                    cache_key, tool_call_id, tool_name, full,
                )

        # ── Register in-flight → execute → cache → signal waiters ──
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        self._in_flight[cache_key] = future
        try:
            result = await self._execute_and_cache(request, handler, cache_key)
            # On success, resolve the future so waiters can proceed.
            # Use a local reference because a nested subagent call may
            # have already cleaned up the _in_flight entry.
            if not future.done():
                future.set_result(None)
            return result
        except Exception:
            # On failure, resolve with exception so waiters get unblocked
            # and can re-execute (cache was not populated for failures).
            if not future.done():
                future.set_exception(
                    RuntimeError("In-flight call failed")
                )
            raise
        finally:
            self._in_flight.pop(cache_key, None)
