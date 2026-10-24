"""Tool dedup middleware — prevent duplicate tool calls within and across agents.

This middleware:
1. P0: In-memory cache — blocks identical (name, args) calls in same session
2. P1: In-flight dedup — concurrent identical calls wait for the first instead
   of executing in parallel (handles LangGraph ToolNode parallel execution)
3. Circuit breaker — auto-escalates after consecutive failures of same tool
4. Cross-agent dedup — persists results to shared backend so subagents and
   Coordinator can share cached results
5. Repeat-hit escalation (design document §8 G24): identical (name, args)
   hits escalate deterministically in EVERY agent context — 1st hit returns
   data + three-way guidance, 2nd hit is hard-blocked; counters are shared
   across instances but isolated per context (Coordinator session / expert
   delegation by the delegation's first HumanMessage — shared derivation
   with G11/G19/G26 and the guidance injector)
6. Proactive reminder (G24 L1): once a hit occurred in this context, a
   one-line system reminder is injected into subsequent model calls

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

from langchain.agents.middleware.types import (
    AgentMiddleware,
    ModelRequest,
    ModelResponse,
)
from langchain_core.messages import ToolMessage
from langgraph.prebuilt.tool_node import ToolCallRequest
from langgraph.types import Command

from deepagents.backends.protocol import BackendProtocol, ReadResult
from deepagents.middleware._utils import append_to_system_message

from diagnostics.agent.delegation_key import delegation_key_from_request
from diagnostics.agent.tool_result_contract import (
    failure_reason,
    is_deterministic_failure,
    is_structural_failure,
)

logger = logging.getLogger(__name__)

# ── Constants ──────────────────────────────────────────────────────────────

_DEDUP_CACHE_PREFIX = "/_dedup_cache"
"""Shared backend path prefix for cross-agent dedup keys."""

_TOOL_FAILURE_BREAKER = int(os.getenv("DIAGNOSTICS_TOOL_FAILURE_BREAKER", "2"))
"""Consecutive failures of same tool → auto-block retry."""

_FULL_CONTENT_MAX = int(os.getenv("DIAGNOSTICS_DEDUP_FULL_CONTENT_MAX", "30000"))

# Repeat-hit escalation (design document §8 G24, v3.22.1 — supersedes the
# subagent-only F2 ladder of 2026-07-30).  Rationale: a dedup hit costs no
# tool execution but DOES cost a full LLM turn (measured 1-14 min/call on
# the local backend) — an identical-args re-issue is the most expensive
# zero-information operation in the system, so its tolerance must be LOWER
# than zero-yield executions (G19), not higher.  1st hit in a context
# returns data + three-way guidance (satisfy + redirect); the 2nd hit is
# hard-blocked (G18-shaped 1/2 ladder — an identical re-issue carries even
# less information than a ceremonial verdict restatement, which G18 blocks
# on the second offense).
_DEDUP_HIT_BLOCK = int(os.getenv("DIAGNOSTICS_DEDUP_HIT_BLOCK", "2"))

# Termination tier (v3.43.0, G24-T).  Measured 2026-10-21 in the production
# environment: an identical-args call blocked 167 times and the expert still
# kept re-issuing it — a receipt-level ladder cannot stop a model that keeps
# regenerating the same call, because every receipt still costs one full
# model turn and leaves the delegation running.  This tier converts the
# PATTERN into a deterministic end: after _DEDUP_REPEAT_TERMINATE hits on the
# same cache key, the whole context is terminated (every further tool call is
# refused with a fixed receipt) while the conclusion tools and write_file stay
# exempt so the agent can still deliver a report / structured conclusion.
_DEDUP_REPEAT_TERMINATE = int(
    os.getenv("DIAGNOSTICS_DEDUP_REPEAT_TERMINATE", "5"))

_REPEAT_TERMINATE_RECEIPT = (
    "[系统] 本上下文的重复工具调用已触发系统终止：相同参数的重发不会获得新数据，"
    "系统不再执行任何工具调用。请立即基于已采集的结果输出结论；"
    "未覆盖项在结论中如实声明数据缺口。"
)

# Per-turn fanout cap (v3.43.0, G24-W).  LangGraph's ToolNode dispatches an
# unbounded number of tool calls from a single assistant message (measured by
# third parties at 1/100/1000/5000 calls with no cap, no warning, no error),
# and `recursion_limit` counts superstep DEPTH — a fan-out of any width costs
# one superstep.  One production turn emitted 886 tool calls (869 deduped),
# which the execution-side dedup cannot prevent: it blocks execution, not
# generation, and every blocked call still injects a ToolMessage into the
# context.  The cap protects the CONTEXT, not the tool runtime.
_MAX_TOOL_CALLS_PER_TURN = int(
    os.getenv("DIAGNOSTICS_MAX_TOOL_CALLS_PER_TURN", "20"))

# Observation floor: log the shape of every non-trivial turn so a runaway
# fan-out is visible in the log BEFORE the cap fires (the production incident
# was found only after 167 blocks; the width was never logged).
_FANOUT_OBSERVE_MIN = int(
    os.getenv("DIAGNOSTICS_FANOUT_OBSERVE_MIN", "5"))

_FANOUT_RECEIPT = (
    "[系统] 本轮工具调用数量超过单批上限（{cap} 个），该调用未执行。"
    "请重新规划：只保留必要的取证动作，其余分批进行；"
    "相同参数的重发不会获得新数据。"
)

# Repeat receipt (v3.37.4, P0/P1 — context-pollution fix).  Deliberately
# IDENTICAL on every repeat: the result already lives in this conversation,
# so re-injecting it adds nothing but duplicate content.  Measured
# 2026-09-11 (scenario 34, captured request 23): the pre-fix behaviour
# duplicated 28% of the tool-result characters in one delegation (5/21 tool
# messages; one result appeared 3x) and appended one escalating
# "this is repeat #N" note per attempt — and the model's reasoning then
# echoed that pattern ("I notice something odd…" x69) until it consumed the
# whole 16384-token output budget.  Literature: duplicate/redundant content
# is the textbook definition of context pollution, and self-reinforcement
# of a repeated context is a documented root cause of repetition loops
# (arXiv:2512.04419).  Brevity is the point: one fixed line, no counter, no
# content, no retrieval hint (the data is one turn above).
_DEDUP_REPEAT_RECEIPT = (
    "[系统去重] 与上次相同参数的调用已跳过：结果与上次一致，已在上文返回。"
    "请直接引用已有结果；需要新数据时调整参数（时间窗/维度/过滤）。"
)
"""Subagent dedup hits return full content (not preview) up to this size.

A subagent starts with a fresh context — the original result is NOT in
its history, so a 300-char preview forces a read_file round-trip (often
several, paginated).  Returning the full content directly below this
threshold eliminates that round-trip; deepagents' eviction (~80k chars)
does not re-trigger.  Larger results fall back to a head+tail structured
summary (G24 L2 — the old 300-char preview left the LLM's data need
entirely unmet and motivated re-issuing the same call)."""

# ── Pattern: transport/network-level tool failures ─────────────────────────

_TOOL_FAILURE_PATTERNS = re.compile(
    r"timeout|timed out|connection refused|connection reset|"
    r"network (is )?unreachable|no route to host|"
    r"OSP.*(?:fail|error|timeout)|remote.*(?:fail|error|timeout)|"
    r"channel closed|broken pipe|"
    # Production tool-contract failure markers (v3.22.2): Argus service
    # errors ("Argus查询失败"), OSP script dispatch errors ("脚本执行失败"),
    # kube-apiserver call errors ("k8s脚本执行失败").  These are
    # closed-vocabulary service-contract markers, not open-ended keyword
    # semantics (the S1 rejection does not apply to closed contracts).
    r"查询失败|执行失败",
    re.IGNORECASE,
)

# ── Pattern: empty / nil / error results that should NOT be cached ─────────
# Caching a "not found" or empty result would lock the LLM into a failure
# loop — every subsequent round would hit the cache and see the same useless
# data, preventing recovery.

_EMPTY_PATTERNS = re.compile(
    # The anchored "^\\[\\]$" matches a bare empty JSON array — the Argus
    # "service healthy, no data" contract (v3.22.2).  Anchors keep real
    # payloads that merely contain "[]" from matching.
    r"not found|no data|no results|error|空|^\s*\[\s*\]\s*$",
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
    "propose_hypotheses", "select_path", "record_finding", "backtrack",
})

# Structured-return conclusion tools (DeepExpertFindings /
# ArgusExpertFindings) are the wrap-up path, not evidence gathering —
# exempt exactly like G19 (2026-09-08, commit 822497a): blocking them can
# deadlock the agent on backends where the structured-output binding
# forces a tool call on EVERY model turn (langchain has no
# reset-tool-choice mechanism), with the conclusion tool being the only
# call that ends the loop.
_DEDUP_SKIP = (
    _SCAFFOLDING_TOOLS | _LEDGER_TOOLS
    | {"task", "DeepExpertFindings", "ArgusExpertFindings"}
)

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
    """Return True if tool output indicates a failed tool execution.

    Two branches, in this order:

    1. **Closed structural contract** (v3.43.0): `STATUS: failed`, an OSP
       result whose succeed list is empty while the failed list is not, or a
       non-zero `exit status` accompanied by `stderr:`.  These are MECHANICAL
       verdicts and must be length-independent — measured 2026-10-21 that the
       same failure sat at 395 characters (classified as a failure) and would
       flip to "diagnostic data" as soon as the payload crossed the
       500-character guard below.  A verdict must not depend on the size of a
       vm_name.
    2. **Prose heuristic** (unchanged): diagnostic data often contains words
       like 'timeout' or 'error' while describing the fault being investigated
       — these must NOT be mistaken for tool failures, so long/multi-line/
       table-shaped content keeps the conservative guards.
    """
    if not content:
        return False
    if is_structural_failure(content):
        return True
    # Only short content can be a prose-style failure message (diagnostic data
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

    Must be placed BEFORE DiagnosisLedgerMiddleware in the pipeline so
    that cache hits bypass unnecessary downstream processing.

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
        # True only on instances created via for_subagent() — enables
        # full-content dedup hits for fresh-context experts (B1).
        self._is_subagent = False
        # In-memory cache shared across agents via for_subagent()
        self._tool_call_cache: dict[str, tuple[ToolMessage, int]] = {}
        # In-flight futures shared across agents via for_subagent()
        self._in_flight: dict[str, asyncio.Future] = {}
        # Repeat-hit counters per context (G24): context_key → cache_key →
        # count.  Coordinator uses a fixed key (its context IS the whole
        # session); expert instances key by first-HumanMessage hash (same
        # derivation as G19/G11) because subagent middleware instances are
        # SHARED across all delegations (factory wires one instance into
        # every subagent) — without per-context isolation, a cache key
        # touched by two delegations would hard-block the third
        # delegation's FIRST lookup (latent F2 bug that the tightened
        # 1/2 ladder would have amplified into systematic false blocks).
        self._dedup_hit_counts: dict[str, dict[str, int]] = {}
        # Contexts whose repeat budget is exhausted (G24-T): every further
        # tool call is refused deterministically.  Shared with subagents so a
        # terminated delegation cannot restart through a sibling instance.
        self._terminated_contexts: set[str] = set()
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
        instance._is_subagent = True
        instance._tool_call_cache = coordinator._tool_call_cache
        instance._in_flight = coordinator._in_flight
        # G24: share hit counters too — context isolation (Coordinator
        # fixed key vs per-delegation hash) keeps them from interfering.
        instance._dedup_hit_counts = coordinator._dedup_hit_counts
        # G24-T: terminations are per context, so sharing the set is safe and
        # keeps a terminated delegation terminated across instances.
        instance._terminated_contexts = coordinator._terminated_contexts
        return instance

    # ── Tool call interception ─────────────────────────────────────────

    async def _execute_and_cache(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command]],
        cache_key: str,
        fail_base: int = 0,
    ) -> ToolMessage | Command:
        """Execute tool call and populate cache for both memory and backend.

        fail_base: consecutive-failure count carried over from a previous
        execution of the same cache key (real-retry path) — a retry that
        fails again accumulates to fail_base+1; a retry that recovers
        resets to 0.
        """
        result = await handler(request)

        if isinstance(result, ToolMessage):
            content = result.content if isinstance(result.content, str) else str(result.content)

            # Failure classification order (v3.22.2, extended v3.43.0):
            # failure FIRST — a short network error (e.g. "error: connection
            # timeout", <50 chars) must reach the circuit breaker instead of
            # being swallowed by the empty-result guard below.  The old order
            # made the breaker unreachable for short error messages: they were
            # never cached, so every identical re-issue executed for real,
            # unthrottled.
            #
            # v3.43.0 adds two branches: the framework-level error status
            # (LangChain tools that raise ToolException / set status="error")
            # and the closed structural contract of script-dispatch tools.
            if getattr(result, "status", "success") == "error" or _is_tool_failure(content):
                # A deterministic failure (unresolvable DNS name, missing
                # command) can never be fixed by re-issuing the same call:
                # start at the breaker threshold so the FIRST repeat is
                # refused instead of granted a real re-execution.  This is
                # the retry budget that fed the 167-repeat loop (2026-10-21).
                next_fail = (_TOOL_FAILURE_BREAKER
                             if is_deterministic_failure(content)
                             else fail_base + 1)
                self._tool_call_cache[cache_key] = (result, next_fail)
            elif not content or (len(content) < 50 and _EMPTY_PATTERNS.search(content)):
                # Non-failure empty/no-data result: never cached, so the
                # agent may retry or widen the query (caching it would
                # lock every later identical call onto the same void).
                logger.debug("Skipped caching empty/error result: %s", cache_key)
            else:
                self._tool_call_cache[cache_key] = (result, 0)

            # Persist to shared_backend for cross-agent dedup (failures
            # included: other agents hitting the same key should see the
            # cached failure instead of hammering a dead service).
            if self._backend and content and not (len(content) < 50 and _EMPTY_PATTERNS.search(content)):
                backend_key = f"{_DEDUP_CACHE_PREFIX}/{cache_key}"
                try:
                    await self._backend.awrite(backend_key, content)
                except Exception:
                    pass  # best-effort; memory cache still works

        return result

    def _hit_context_key(self, request: ToolCallRequest | ModelRequest) -> str:
        """Context key for repeat-hit counters (G24).

        Coordinator → fixed key (its context IS the whole session, so
        cross-round accumulation is correct).  Expert instances → the
        delegation's first HumanMessage (shared derivation with
        G11/G19/G26 and the guidance injector): subagent middleware
        instances are shared across all delegations, and each delegation
        is a fresh agent invocation whose first HumanMessage carries the
        task description — keying on it separates concurrent/stale
        delegations deterministically.
        """
        if not self._is_subagent:
            return "coordinator"
        # Shared derivation (G11/G19/G26/guidance): the guidance middleware
        # reads these counters with the same key, so it must not diverge.
        return delegation_key_from_request(request)

    def _dedup_hit_result(
        self,
        cache_key: str,
        tool_call_id: str,
        tool_name: str,
        full_content: str,
    ) -> ToolMessage:
        """Build the 1st-hit ToolMessage (G24 L2): data + three-way guidance.

        Data shape forks on B1 (unchanged): a fresh-context subagent gets
        the FULL content below _FULL_CONTENT_MAX (its history lacks the
        original result; a bare summary would force a read_file
        round-trip).  Everything else gets a head+tail structured summary
        with total size and retrieval hints — replacing the old 300-char
        preview, which left the LLM's data need entirely unmet and
        motivated re-issuing the same call (2026-09-08 production).

        The three-way guidance is appended to BOTH shapes from the very
        first hit — the old preview branch silently dropped the
        escalation warning for large results (R2b), making the warning
        channel exactly dead in the scenario most prone to repetition.
        """
        backend_path = f"{_DEDUP_CACHE_PREFIX}/{cache_key}"
        guidance = (
            "\n下一步三选一：\n"
            f"① 需全文 → read_file \"{backend_path}\""
            "（limit≥500，按 offset 顺推，避免逐页翻读）；\n"
            "② 需不同数据 → 调整查询参数（时间窗/维度/过滤）——"
            "以完全相同参数重发不会获得新数据；\n"
            "③ 数据已足够 → 直接引用本结果继续分析并产出结论。"
        )
        if self._is_subagent and len(full_content) <= _FULL_CONTENT_MAX:
            return ToolMessage(
                content=(
                    f"[系统去重] {tool_name} 与此前一次调用的参数完全相同，"
                    "本次未重复执行。完整结果（"
                    f"{len(full_content)} 字符，与此前一致）如下：\n"
                    f"---\n{full_content}\n---"
                    + guidance
                ),
                tool_call_id=tool_call_id,
                name=tool_name,
            )
        head = full_content[:1500].rstrip()
        tail = full_content[-1500:].lstrip() if len(full_content) > 3000 else ""
        summary = f"{head}\n…（中段省略）…\n{tail}" if tail else head
        return ToolMessage(
            content=(
                f"[系统去重] {tool_name} 与此前一次调用的参数完全相同，"
                "本次未重复执行。结果首尾摘要"
                f"（全文 {len(full_content)} 字符，与此前一致）：\n"
                f"---\n{summary}\n---"
                + guidance
            ),
            tool_call_id=tool_call_id,
            name=tool_name,
        )

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelResponse:
        """G24 L1 proactive reminder: once a repeat hit occurred in this
        context, inject a one-line system reminder on subsequent model
        calls so the agent stops re-issuing identical calls even if the
        original hit receipt was evicted from the conversation.

        Selective injection (silent when no hit — Proactive Memory Agent
        principle); runs on BOTH Coordinator and expert instances (the
        ledger middleware skips prompt injection for subagents, so this
        channel is the only one available inside a delegation).
        Guidance must never become a failure source — any error falls
        through to the unmodified request.
        """
        try:
            hits = self._dedup_hit_counts.get(self._hit_context_key(request))
            total = sum(hits.values()) if hits else 0
            if total:
                reminder = (
                    f"[系统提醒] 本上下文中已有 {total} 次重复调用被去重"
                    "——相关数据已在去重回执中提供并缓存于 /_dedup_cache/，"
                    "以相同参数重发不会获得新数据；直接引用已有结果，"
                    "或 read_file 读取缓存，或调整查询参数获取新数据。"
                )
                request = request.override(
                    system_message=append_to_system_message(
                        request.system_message, reminder))
        except Exception:
            pass
        response = await handler(request)
        return self._cap_fanout(response)

    # ── Per-turn fanout cap (G24-W) ─────────────────────────────────────
    @staticmethod
    def _select_tool_calls(tool_calls: list) -> tuple[list, list]:
        """Keep DISTINCT calls first (in order), then fill up to the cap.

        Distinct-first matters: in a runaway turn the repeated calls vastly
        outnumber the distinct ones, so truncating verbatim would keep N
        copies of the same call and drop the genuinely different ones.
        """
        keep: list = []
        dropped: list = []
        seen: set[str] = set()
        for tc in tool_calls:
            if len(keep) >= _MAX_TOOL_CALLS_PER_TURN:
                dropped.append(tc)
                continue
            key = _make_cache_key(tc.get("name", ""), tc.get("args") or {})
            if key in seen:
                dropped.append(tc)
                continue
            seen.add(key)
            keep.append(tc)
        return keep, dropped

    def _cap_fanout(self, response: ModelResponse) -> ModelResponse:
        """Trim an over-wide assistant turn and answer the trimmed calls.

        LangGraph dispatches every tool call of one assistant message and
        charges one superstep for the whole fan-out, so an unbounded turn is
        invisible to `recursion_limit`; execution-side dedup does not help
        either, because it blocks execution but still emits one ToolMessage
        per blocked call (measured 2026-10-21: 886 calls in one turn, 869
        deduped).  This cap protects the CONTEXT.  Trimmed calls are answered
        with a paired ToolMessage (same shape as the G28 back-jump placeholder
        contract) so no tool_call_id is left dangling.
        """
        try:
            if not isinstance(response, ModelResponse):
                return response
            messages = (response.result
                        if isinstance(response.result, list) else [response.result])
            capped: list = []
            trimmed = False
            for msg in messages:
                tool_calls = getattr(msg, "tool_calls", None) or []
                if len(tool_calls) >= _FANOUT_OBSERVE_MIN:
                    distinct = len({
                        _make_cache_key(tc.get("name", ""), tc.get("args") or {})
                        for tc in tool_calls
                    })
                    logger.info(
                        "单批工具调用规模: %d 个（唯一 %d，上限 %d）",
                        len(tool_calls), distinct, _MAX_TOOL_CALLS_PER_TURN,
                    )
                if len(tool_calls) <= _MAX_TOOL_CALLS_PER_TURN:
                    capped.append(msg)
                    continue
                keep, dropped = self._select_tool_calls(tool_calls)
                try:
                    capped.append(msg.model_copy(update={"tool_calls": keep}))
                except Exception:
                    # Cannot trim this message type — keep it untouched rather
                    # than emit an unbalanced conversation.
                    capped.append(msg)
                    continue
                trimmed = True
                logger.warning(
                    "单批工具调用超上限: 收到 %d，保留 %d（唯一），丢弃 %d，上限 %d",
                    len(tool_calls), len(keep), len(dropped),
                    _MAX_TOOL_CALLS_PER_TURN,
                )
                for tc in dropped:
                    capped.append(ToolMessage(
                        content=_FANOUT_RECEIPT.format(
                            cap=_MAX_TOOL_CALLS_PER_TURN),
                        tool_call_id=tc.get("id", ""),
                        name=tc.get("name", ""),
                        status="error",
                    ))
            if not trimmed:
                return response
            return ModelResponse(
                result=capped,
                structured_response=getattr(response, "structured_response", None),
            )
        except Exception:
            return response

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

        # G24: the context (Coordinator session / this expert delegation) is
        # resolved once per call — the repeat counters, the termination set
        # and the termination check must all agree on it.
        try:
            hit_key = self._hit_context_key(request)
        except Exception:
            hit_key = "coordinator"

        # ── G24-T: terminated context (v3.43.0) ──
        # Once this context exhausted its repeat budget, no further tool call
        # is dispatched.  The check sits BEFORE the cache lookup on purpose: a
        # terminated loop must not be resumable with different arguments —
        # with the observation shape that caused it (script-dispatch tools
        # returning execution logs), every new target produces the same useless
        # result and the same repeat pattern.
        if hit_key in self._terminated_contexts:
            logger.warning("去重终止级拒绝(上下文%s): %s", hit_key, cache_key)
            return ToolMessage(
                content=_REPEAT_TERMINATE_RECEIPT,
                tool_call_id=tool_call_id,
                name=tool_name,
            )

        # ── P0: In-memory cache lookup + circuit breaker ──
        if cache_key in self._tool_call_cache:
            cached_msg, fail_count = self._tool_call_cache[cache_key]
            if fail_count == 0:
                logger.info("去重命中: %s", cache_key)
                self._emit_dedup_event(tool_call_id)
                full = cached_msg.content if isinstance(
                    cached_msg.content, str) else str(cached_msg.content)
                counts = self._dedup_hit_counts.setdefault(hit_key, {})
                # v3.37.4 (P0/P1): an IDENTICAL repeat inside the SAME
                # context carries no new information — the original result
                # is already in this conversation, so only a fixed one-line
                # receipt is returned.  The pre-fix path re-injected the full
                # body on the first repeat and an escalating "this is repeat
                # #N" note afterwards; measured 2026-09-11 that polluted 28%
                # of the tool-result characters of one delegation and the
                # model echoed the pattern into a 69x reasoning loop.
                # Cross-context first hits (parallel delegations, the
                # Coordinator) keep the full-content path: their history
                # does NOT contain the result, so a summary there would
                # force an unnecessary read_file round-trip.
                if cache_key in counts:
                    counts[cache_key] += 1
                    # G24-T: the escalation ladder now ends.  A receipt-level
                    # block was measured 167 times in a row (2026-10-21) with
                    # the model re-issuing the call immediately after each
                    # one — the pattern, not the call, is the failure.
                    if counts[cache_key] >= _DEDUP_REPEAT_TERMINATE:
                        self._terminated_contexts.add(hit_key)
                        logger.warning(
                            "去重终止级(上下文%s 重复%d次): %s",
                            hit_key, counts[cache_key], cache_key,
                        )
                        return ToolMessage(
                            content=_REPEAT_TERMINATE_RECEIPT,
                            tool_call_id=tool_call_id,
                            name=cached_msg.name or tool_name,
                        )
                    logger.warning(
                        "去重拦截(上下文%s 重复%d次): %s",
                        hit_key, counts[cache_key], cache_key,
                    )
                    return ToolMessage(
                        content=_DEDUP_REPEAT_RECEIPT,
                        tool_call_id=tool_call_id,
                        name=cached_msg.name or tool_name,
                    )
                counts[cache_key] = 1
                return self._dedup_hit_result(
                    cache_key, tool_call_id, cached_msg.name or tool_name,
                    full,
                )
            # Previous call(s) failed → this is a retry
            if fail_count >= _TOOL_FAILURE_BREAKER:
                cached_text = (cached_msg.content
                               if isinstance(cached_msg.content, str)
                               else str(cached_msg.content))
                # v3.43.0: name the reason instead of a generic "network may
                # be unreachable".  The reason is the EVIDENCE the model
                # needs (an unresolvable name is a finding, not a tool bug),
                # and a receipt that contradicts what the model can see is a
                # direct trigger for one more retry.
                reason = failure_reason(cached_text)
                detail = f"：{reason}" if reason else "。"
                logger.warning("熔断: %s (连续失败%d次)", cache_key, fail_count)
                return ToolMessage(
                    content=(
                        f"⛔ [系统熔断] {tool_name} 已连续失败"
                        f"{fail_count}次{detail}\n"
                        "重试不会改变结果——请把该失败本身当作证据"
                        "（目标不可达/域名不可解析/权限不足/资源不存在等），"
                        "改换查询参数或换维度取证，基于已有证据继续诊断。"
                    ),
                    tool_call_id=tool_call_id,
                    name=cached_msg.name,
                )
            # Real retry (v3.22.2): actually re-execute once instead of
            # replaying the cached failure — transient faults (brief
            # service flap) recover on the second execution, persistent
            # faults accumulate to the breaker.  The old "warn without
            # executing" path never gave transient faults a real chance
            # despite the comment claiming "allow one retry".
            logger.info("失败重试(真实执行): %s", cache_key)
            result = await self._execute_and_cache(
                request, handler, cache_key, fail_base=fail_count)
            if isinstance(result, ToolMessage):
                new_content = (result.content if isinstance(result.content, str)
                               else str(result.content))
                if (getattr(result, "status", "success") == "error"
                        or _is_tool_failure(new_content)):
                    reason = failure_reason(new_content)
                    detail = f"：{reason}" if reason else ""
                    return ToolMessage(
                        content=(
                            f"{new_content}\n\n"
                            f"⚠ [系统提示] {tool_name} 第{fail_count + 1}次调用仍失败"
                            f"{detail}，后续同参数调用将被熔断（禁止重试）；"
                            "如需数据请调整查询参数，或基于已有证据继续诊断。"
                        ),
                        tool_call_id=tool_call_id,
                        name=result.name or tool_name,
                        status="error",
                    )
            return result

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
        except BaseException:
            # On failure, resolve with exception so waiters get unblocked
            # and can re-execute (cache was not populated for failures).
            # BaseException covers cancellation too: a cancelled first call
            # must not leave every duplicate waiter asleep forever on a
            # future that nobody will ever complete (2026-10-25 review H1).
            if not future.done():
                future.set_exception(
                    RuntimeError("In-flight call failed or was cancelled")
                )
            raise
        finally:
            # Defence in depth: every exit path completes the future.
            if not future.done():
                future.set_exception(
                    RuntimeError("In-flight call did not complete"))
            self._in_flight.pop(cache_key, None)
