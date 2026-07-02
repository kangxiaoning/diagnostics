"""Tool dedup middleware — prevent duplicate tool calls within and across agents.

This middleware:
1. P0: In-memory cache — blocks identical (name, args) calls in same session
2. Circuit breaker — auto-escalates after consecutive failures of same tool
3. Cross-agent dedup — persists results to shared backend so subagents and
   Coordinator can share cached results

Extracted from DiagnosisLedgerMiddleware to keep ledger focused on
hypothesis management.
"""

from __future__ import annotations

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
    """Return True if tool output indicates a transport/network failure."""
    if not content:
        return False
    # Only match if the failure signal dominates: a short error message
    # is a failure; a long diagnostic output that happens to mention
    # "timeout" in passing is not.
    if len(content) > 2000:
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
    """

    def __init__(self, backend: BackendProtocol | None = None) -> None:
        self._backend = backend
        # In-memory cache shared across agents via for_subagent()
        self._tool_call_cache: dict[str, tuple[ToolMessage, int]] = {}
        logger.info(
            "ToolDedupMiddleware: backend=%s, breaker=%d",
            "enabled" if backend else "disabled",
            _TOOL_FAILURE_BREAKER,
        )

    @classmethod
    def for_subagent(cls, coordinator: "ToolDedupMiddleware") -> "ToolDedupMiddleware":
        """Create a subagent instance that shares the Coordinator's cache.

        Subagents and the Coordinator share the same in-memory cache so
        that a tool call made by the Coordinator is deduplicated when a
        subagent tries the same call, and vice versa.
        """
        instance = cls(backend=coordinator._backend)
        instance._tool_call_cache = coordinator._tool_call_cache
        return instance

    # ── Tool call interception ─────────────────────────────────────────

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
                return ToolMessage(
                    content=cached_msg.content,
                    tool_call_id=tool_call_id,
                    name=cached_msg.name,
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

        # ── Cross-agent dedup via shared_backend ──
        if self._backend:
            backend_key = f"{_DEDUP_CACHE_PREFIX}/{cache_key}"
            try:
                cached_content = await self._backend.aread(backend_key)
            except Exception:
                cached_content = None
            # StateBackend.aread() returns ReadResult(error=...) for
            # missing keys — NOT None.  Treat error responses as a
            # cache miss so the tool actually executes.
            if cached_content is not None and not (
                isinstance(cached_content, ReadResult) and cached_content.error
            ):
                logger.info(
                    "跨Agent去重命中: %s (from shared_backend)", cache_key,
                )
                restored = ToolMessage(
                    content=cached_content,
                    tool_call_id=tool_call_id,
                    name=tool_name,
                )
                self._tool_call_cache[cache_key] = (restored, 0)
                return restored

        # ── Execute tool ──
        result = await handler(request)

        # ── Cache result with failure tracking ──
        if isinstance(result, ToolMessage):
            content = result.content if isinstance(result.content, str) else str(result.content)

            if not content or (len(content) < 50 and _EMPTY_PATTERNS.search(content)):
                logger.debug("Skipped caching empty/error result: %s", cache_key)
            else:
                fail_count = 1 if _is_tool_failure(content) else 0
                self._tool_call_cache[cache_key] = (result, fail_count)

            # ── Persist to shared_backend for cross-agent dedup ──
            if self._backend and content and not (len(content) < 50 and _EMPTY_PATTERNS.search(content)):
                backend_key = f"{_DEDUP_CACHE_PREFIX}/{cache_key}"
                try:
                    await self._backend.awrite(backend_key, content)
                except Exception:
                    pass  # best-effort; memory cache still works

        return result
