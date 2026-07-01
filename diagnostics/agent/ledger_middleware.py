"""Diagnosis ledger middleware — hypothesis-driven diagnosis orchestration.

This middleware:
1. Injects a compact ledger summary into the system message each LLM call
2. Provides three hypothesis management tools (commit_hypotheses, select_path, record_finding)
3. Auto-records evidence from diagnostic tool calls
4. Persists the ledger to the filesystem on key changes

See: private/HYPOTHESIS_LEDGER_DESIGN.md
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import pathlib
import re
from collections.abc import Awaitable, Callable

from langchain.agents.middleware.types import (
    AgentMiddleware,
    ExtendedModelResponse,
    ModelRequest,
    ModelResponse,
)
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import ToolMessage
from langchain_core.tools import StructuredTool
from langgraph.prebuilt.tool_node import ToolCallRequest
from langgraph.types import Command
from pydantic import BaseModel, Field

from deepagents.backends.protocol import BackendProtocol

from diagnostics.agent.ledger import (
    DiagnosisLedger,
    DiagnosisLedgerState,
    add_evidence_to_active,
    add_hypotheses,
    backtrack,
    check_exit_conditions,
    derive_phase,
    finalize_pending_for_report,
    ledger_to_json,
    new_evidence,
    new_ledger,
    record_finding,
    record_round,
    render_ledger_context,
    select_path,
)
from deepagents.middleware._utils import append_to_system_message

logger = logging.getLogger(__name__)

# Tools that are scaffolding (not diagnostic). "task" is included because
# expert delegation results are recorded by the Coordinator via record_finding.
_SCAFFOLDING_TOOLS = frozenset({
    "write_file", "read_file", "edit_file",
    "write_todos", "read_todos",
    "ls", "glob", "grep",
})

# Ledger management tools (handled by this middleware)
_LEDGER_TOOLS = frozenset({
    "commit_hypotheses", "select_path", "record_finding", "backtrack",
})

# ── Safety mechanism thresholds ──
_MAX_ROUNDS = 20                    # Hard limit: force REPORT after this many LLM rounds
_STAGNATION_THRESHOLD = 3           # Consecutive task() without record_finding → stagnation
_MIN_ROUND_FOR_SAFETY = 3           # Don't activate safety checks before this round
_MODEL_CALL_TIMEOUT = int(os.getenv("DIAGNOSTICS_MODEL_TIMEOUT", "240"))  # seconds per LLM call
_REPORT_PHASE_TIMEOUT = int(os.getenv("DIAGNOSTICS_REPORT_TIMEOUT", "480"))  # report phase needs longer timeout
_TOOL_FAILURE_BREAKER = 2           # Consecutive failures of same tool → auto-block retry
_DEDUP_CACHE_PREFIX = "/_dedup_cache"  # shared_backend path for cross-agent dedup


def _build_subagent_context(ledger: dict, max_chars: int = 1200) -> str:
    """Extract already-collected evidence from the ledger for subagent injection.

    When a subagent is delegated via task(), it normally sees only the
    Coordinator's description.  This function extracts the key findings
    already recorded in the ledger so the subagent can skip redundant
    tool calls and focus on its specific verification task.
    """
    lines: list[str] = []

    # ── Diagnostic tools already called (by Coordinator or prior experts) ──
    tools_called: set[str] = set()
    for rd in ledger.get("rounds", []):
        tools_called.update(rd.get("tools_called", []))
    diagnostic_tools = sorted(tools_called - _SCAFFOLDING_TOOLS - _LEDGER_TOOLS - {"task"})
    if diagnostic_tools:
        lines.append(f"Coordinator 已通过 Argus 收集基线数据: {', '.join(diagnostic_tools)}")

    # ── Evidence already collected per hypothesis ──
    for hid, h in ledger.get("hypotheses", {}).items():
        evidence_entries = h.get("evidence", [])
        if not evidence_entries:
            continue
        summaries: list[str] = []
        for e in evidence_entries:
            s = e.get("summary", "").strip()
            if s:
                # Truncate each evidence entry to keep context compact
                summaries.append(s[:250])
        if summaries:
            status_label = {"confirmed": "✓", "refuted": "✗", "verifying": "⟳"}.get(h["status"], "?")
            lines.append(f"\n假设 {hid} [{status_label}] {h.get('statement', '')}:")
            for s in summaries:
                lines.append(f"  - {s}")

    # ── Expert analysis results ──
    expert = ledger.get("expert_analysis")
    if expert:
        finding = expert.get("finding", "")
        if finding:
            lines.append(f"\n已委派专家: {expert.get('expert', '?')} ({expert.get('skill', '?')})")
            lines.append(f"  结论: {finding[:300]}")

    context = "\n".join(lines)
    if len(context) > max_chars:
        context = context[:max_chars] + "\n...(已截断)"
    return context


# Match unsafe filesystem path characters
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


# Patterns that indicate a tool call failed at the transport/network level
# rather than returning valid (possibly empty) diagnostic data.
_TOOL_FAILURE_PATTERNS = re.compile(
    r"timeout|timed out|connection refused|connection reset|"
    r"network (is )?unreachable|no route to host|"
    r"OSP.*(?:fail|error|timeout)|remote.*(?:fail|error|timeout)|"
    r"channel closed|broken pipe",
    re.IGNORECASE,
)


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


def _sanitize_ledger(ledger: dict) -> dict:
    """Strip internal fields from ledger before sending to frontend/streaming."""
    internal_keys = {"_inconclusive_streak", "_backtrack_count"}
    return {k: v for k, v in ledger.items() if k not in internal_keys}


# 8-char hex hash at end of filename, e.g. "...-1acc02b6.md"
_REPORT_HASH_RE = re.compile(r'-([0-9a-f]{8})\.md$')


def _paths_match(tool_path: str, report_path: str) -> bool:
    """Check if tool_path targets the same file as report_path.

    The report path is pre-generated as ``YYYY-MM-DD-HHMMSS-<hash>.md``
    and injected verbatim into the LLM prompt.  However, the LLM may
    alter the path (e.g. strip the HHMMSS segment) when calling
    ``write_file``.  We therefore fall back to comparing the 8-char
    hex hash suffix — unique per diagnosis session — when the exact
    path comparison fails.
    """
    if not tool_path or not report_path:
        return False
    a = tool_path.lstrip("/").rstrip("/")
    b = report_path.lstrip("/").rstrip("/")
    if a == b:
        return True
    # Fallback: compare the unique hash suffix
    ma = _REPORT_HASH_RE.search(a)
    mb = _REPORT_HASH_RE.search(b)
    return bool(ma and mb and ma.group(1) == mb.group(1))


_HYPOTHESIS_ID_PATTERN = re.compile(r'(?:验证)?假设\s*(H\d+(?:\.\d+)?)')


def _parse_hypothesis_id_from_description(
    description: str,
    hypotheses: dict,
) -> str | None:
    """Extract a hypothesis ID from a task() description like '验证假设 H2：...'.

    Returns the matching hypothesis ID if found and present in *hypotheses*,
    otherwise None.
    """
    if not description or not hypotheses:
        return None
    match = _HYPOTHESIS_ID_PATTERN.search(description)
    if match:
        hid = match.group(1)
        if hid in hypotheses:
            return hid
    return None


# ── Pydantic schemas for tool inputs ──

class HypothesisSpec(BaseModel):
    statement: str = Field(description="假设陈述")
    probability: int = Field(description="可能性 0-100", ge=0, le=100)
    rationale: str = Field(default="", description="假设依据")


class DeprioritizedSpec(BaseModel):
    id: str = Field(description="被降级的假设ID")
    reason: str = Field(description="降级原因")


class CommitHypothesesInput(BaseModel):
    hypotheses: list[HypothesisSpec] = Field(
        description="假设列表（最多3个，按概率降序）",
    )
    parent_hypothesis_id: str | None = Field(
        default=None,
        description="父假设ID。None=顶层假设，否则在该假设下深化",
    )
    focus_summary: str = Field(
        default="",
        description="本轮假设形成的依据摘要",
    )


class SelectPathInput(BaseModel):
    selected_hypothesis_id: str = Field(description="选择的假设ID")
    rationale: str = Field(description="选择理由")
    deprioritized: list[DeprioritizedSpec] = Field(
        default_factory=list,
        description="未选中假设的降级原因",
    )


class RecordFindingInput(BaseModel):
    hypothesis_id: str = Field(description="验证的假设ID")
    verdict: str = Field(
        description="验证结论: confirmed(已证实) | refuted(已排除) | inconclusive(证据不足)",
        pattern="^(confirmed|refuted|inconclusive)$",
    )
    evidence_summary: str = Field(description="验证证据摘要")
    probability_update: int = Field(
        description="更新后的概率 0-100", ge=0, le=100,
    )
    new_insights: str = Field(
        default="",
        description="新发现，可能催生新假设",
    )
    statement_update: str = Field(
        default="",
        description=(
            "修正后的假设表述（可选）。当验证发现原始表述不准确时使用，"
            "例如将 'etcd compaction 导致...' 修正为 'etcd NOSPACE alarm 导致...'。"
            "留空表示不修改原表述。"
        ),
    )


# ── Middleware ──

class DiagnosisLedgerMiddleware(AgentMiddleware):
    """Hypothesis-driven diagnosis ledger middleware."""

    state_schema = DiagnosisLedgerState

    def __init__(
        self,
        ledger_path: str = "",
        report_path: str = "",
        model: BaseChatModel | None = None,
        backend: BackendProtocol | None = None,
    ) -> None:
        self.ledger_path = ledger_path
        self.report_path = report_path
        self._model = model
        self._backend = backend
        self._current_ledger: DiagnosisLedger | None = None
        self._model_call_count: int = 0
        # Safety mechanism tracking
        self._consecutive_task_count: int = 0  # global: stagnation detection
        self._same_delegate_count: int = 0     # per-key: delegation saturation
        self._last_delegate_key: str = ""      # "expert:H1" etc.
        self._last_finding_round: int = 0
        self._last_task_round: int = 0
        self._report_phase_start_round: int = 0  # track when REPORT phase began
        # Monotonic sequence counter for preserving tool call invocation
        # order within the same LLM round (LangGraph executes tool calls
        # concurrently, so completion order ≠ invocation order).
        self._round_record_seq: int = 0
        # Tool deduplication: block identical (name, args) calls in same session.
        # Value is (ToolMessage, failure_count): 0 = success, >0 = consecutive failures.
        self._tool_call_cache: dict[str, tuple[ToolMessage, int]] = {}
        # Filesystem path prefix for offloaded tool results.
        self._tool_results_prefix = "/tool_results"
        self.tools: list = [
            self._make_commit_hypotheses_tool(),
            self._make_select_path_tool(),
            self._make_record_finding_tool(),
            self._make_backtrack_tool(),
        ]

    # ── Fault profile extraction via structured output ──

    async def abefore_agent(self, state, runtime) -> dict | None:
        """Initialize the diagnosis ledger before the agent loop starts.

        Runs once before the first LLM invocation.  Creates an empty
        ledger that will be populated as the agent progresses through
        the diagnosis phases.
        """
        if self._current_ledger is not None:
            logger.info(
                "台账: 共享已有实例 (agent=%s, object_id=%s)",
                getattr(runtime, "name", "?"), id(self),
            )
            return None

        ledger = new_ledger()
        self._current_ledger = ledger
        logger.info(
            "台账: 创建新台账 (agent=%s, object_id=%s)",
            getattr(runtime, "name", "?"), id(self),
        )

        return {"_diagnosis_ledger": ledger}

    # ── Round detection via before_model hook ──

    def before_model(self, state, runtime) -> None:
        """Detect new LLM rounds and emit round_transition via stream_writer.

        This is more reliable than the streaming.py node-transition heuristic
        because it fires at the middleware level, independent of LangGraph's
        internal node naming.

        IMPORTANT: this hook MUST be synchronous (not async).  AgentMiddleware
        hooks like before_model / after_model / before_agent are invoked by
        LangGraph as sync callables; declaring them async returns a coroutine
        object which triggers InvalidUpdateError.
        """
        self._model_call_count += 1
        # Sync current_round in ledger with actual LLM call count so that
        # round numbers reflect LLM invocations, not individual tool calls.
        if self._current_ledger is not None:
            self._current_ledger["current_round"] = self._model_call_count
        try:
            sw = getattr(runtime, "stream_writer", None)
            if sw:
                sw({
                    "type": "round_transition",
                    "round": self._model_call_count,
                })
        except Exception:
            pass  # stream_writer not available in all contexts

    # ── Context injection ──

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelResponse | ExtendedModelResponse:
        # self._current_ledger takes priority over state — tools update it in-memory
        # between rounds and state may lag behind without Command-based updates.
        ledger = self._current_ledger
        if ledger is None:
            ledger = request.state.get("_diagnosis_ledger")
        if ledger is None:
            ledger = new_ledger()

        self._current_ledger = ledger
        # Sync round: before_model may have incremented _model_call_count
        # before the ledger was available (happens on the very first LLM
        # call when _current_ledger is still None).
        if self._current_ledger is not None:
            self._current_ledger["current_round"] = self._model_call_count

        context_block = render_ledger_context(ledger)
        safety_block = self._build_safety_warnings(ledger)

        # ── Phase guard: finalize pending hypotheses on REPORT entry ──
        # Catches all REPORT entry paths (derive_phase, timeout, max-rounds,
        # safety valves).  Without this, pending/verifying hypotheses remain
        # in the ledger when the report is generated (or when write_file is
        # never called due to P3 report=null).
        if (ledger.get("current_phase") == "report"
                or derive_phase(ledger) == "report"):
            if finalize_pending_for_report(ledger):
                ledger["current_phase"] = "report"
                self._persist_ledger(ledger)
                logger.info(
                    "Phase guard: finalized pending hypotheses for REPORT "
                    "(round %d)",
                    self._model_call_count,
                )
                # Re-render context so the LLM sees finalized hypothesis tree
                context_block = render_ledger_context(ledger)

        combined_block = context_block
        if safety_block:
            combined_block = f"{combined_block}\n\n{safety_block}" if combined_block else safety_block

        # ── Timeout protection: prevent single LLM call from hanging forever ──
        # REPORT phase generates a long markdown report — use a longer timeout.
        _call_request = request.override(system_message=append_to_system_message(
            request.system_message, combined_block)) if combined_block else request
        _effective_timeout = (
            _REPORT_PHASE_TIMEOUT
            if ledger.get("current_phase") == "report"
            else _MODEL_CALL_TIMEOUT
        )
        try:
            response = await asyncio.wait_for(
                handler(_call_request),
                timeout=_effective_timeout,
            )
        except asyncio.TimeoutError:
            logger.error(
                "Model call timed out after %ds (round %d, phase=%s). "
                "Forcing REPORT phase.",
                _effective_timeout,
                self._model_call_count,
                ledger.get("current_phase", "?"),
            )
            ledger["current_phase"] = "report"
            self._report_phase_start_round = self._model_call_count
            self._persist_ledger(ledger)
            # Return a synthetic error response so the agent loop can
            # continue and generate a report with existing evidence.
            from langchain_core.messages import AIMessage
            error_msg = AIMessage(
                content=(
                    f"[系统超时] 模型调用超过 {_effective_timeout} 秒限制。"
                    "已强制进入 REPORT 阶段。请基于已有证据立即生成诊断报告。"
                ),
            )
            # Build a minimal ModelResponse with the error message.
            # The agent loop will see this as the model's response and
            # proceed to the next iteration where the REPORT phase directive
            # will be injected via the safety mechanism.
            response = ModelResponse(result=[error_msg])

        # ── Post-response: detect verify-phase stall ──
        # If the model returned no tool calls while in verify phase with
        # hypotheses that have expert evidence, force progression to REPORT.
        # Without this, the agent loop exits when the LLM outputs plain
        # text instead of calling record_finding or write_file.
        ledger = self._current_ledger
        if ledger and ledger.get("current_phase") == "verify":
            ai_msgs = [
                m for m in (response.result if isinstance(response.result, list) else [response.result])
                if hasattr(m, "tool_calls")
            ]
            has_tool_calls = any(getattr(m, "tool_calls", None) for m in ai_msgs)
            if not has_tool_calls and ledger.get("hypotheses"):
                active_path = ledger.get("active_path", [])
                active_id = active_path[-1] if active_path else None
                if active_id and active_id in ledger.get("hypotheses", {}):
                    node = ledger["hypotheses"][active_id]
                    has_expert = any(
                        e.get("source", "").startswith("expert:")
                        for e in node.get("evidence", [])
                    )
                    has_coordinator_support = any(
                        e.get("supports") and e.get("source") == "coordinator"
                        for e in node.get("evidence", [])
                    )
                    if has_expert and node.get("status") == "verifying":
                        # Only auto-confirm when coordinator evidence with
                        # supports=True exists.  Expert evidence alone is
                        # insufficient — it always has supports=True
                        # regardless of actual verdict.
                        if has_coordinator_support:
                            record_finding(
                                ledger, active_id, "confirmed",
                                "[\u7cfb\u7edf\u81ea\u52a8] LLM \u5728 verify \u9636\u6bb5\u672a\u8c03\u7528\u5de5\u5177\uff0c"
                                "\u7cfb\u7edf\u57fa\u4e8e\u5df2\u6709\u534f\u8c03\u5458\u786e\u8ba4\u8bc1\u636e\u81ea\u52a8\u786e\u8ba4\u3002",
                                node["probability"],
                            )
                        else:
                            record_finding(
                                ledger, active_id, "inconclusive",
                                "[\u7cfb\u7edf\u81ea\u52a8] LLM \u5728 verify \u9636\u6bb5\u672a\u8c03\u7528\u5de5\u5177\uff0c"
                                "\u8bc1\u636e\u4e0d\u8db3\u4ee5\u786e\u8ba4\uff0c\u7cfb\u7edf\u6807\u8bb0\u4e3a\u672a\u5b8c\u6210\u3002",
                                node["probability"],
                            )
                        ledger["current_phase"] = "report"
                        self._report_phase_start_round = self._model_call_count
                        self._persist_ledger(ledger)
                        logger.warning(
                            "Safety: verify-phase stall (no tool calls, "
                            "round %d) — auto-confirmed %s, forced REPORT",
                            self._model_call_count, active_id,
                        )
                        # Append directive to the existing response so
                        # the agent loop continues into REPORT phase.
                        from langchain_core.messages import AIMessage
                        for m in ai_msgs:
                            if hasattr(m, "content") and isinstance(m.content, str):
                                m.content += (
                                    "\n\n[\u7cfb\u7edf\u5b89\u5168\u9600] verify \u9636\u6bb5\u5df2\u7ed3\u675f\uff0c"
                                    f"\u5047\u8bbe {active_id} \u5df2\u81ea\u52a8\u5904\u7406\u3002"
                                    "\u8bf7\u7acb\u5373\u8c03\u7528 write_file \u751f\u6210\u8bca\u65ad\u62a5\u544a\u3002"
                                )
                                break

        # ── Post-response: detect REPORT-phase stall ──
        # If the model returned no tool calls while in REPORT phase
        # (without having called write_file), auto-generate the report
        # from ledger evidence.  Without this safety net, the agent
        # loop exits with report=null and no .md file is created.
        # This is the REPORT-phase equivalent of the verify-phase
        # stall detection above (L410-451).
        ledger = self._current_ledger
        if ledger and ledger.get("current_phase") == "report":
            ai_msgs = [
                m for m in (response.result if isinstance(response.result, list) else [response.result])
                if hasattr(m, "tool_calls")
            ]
            has_tool_calls = any(getattr(m, "tool_calls", None) for m in ai_msgs)
            if not has_tool_calls and not ledger.get("report"):
                # LLM exited REPORT without write_file — auto-generate
                report_md = self._generate_report_from_ledger(ledger)
                ledger["report"] = report_md

                # Write .md to filesystem at canonical report_path
                if self.report_path:
                    try:
                        import pathlib as _pathlib
                        vpath = self.report_path
                        real_path = vpath.replace(
                            "/agent_data/",
                            str(self._AGENT_DATA_REAL) + "/",
                        )
                        path = _pathlib.Path(real_path)
                        path.parent.mkdir(parents=True, exist_ok=True)
                        path.write_text(report_md, encoding="utf-8")
                        logger.info(
                            "Auto-generated report written to %s (%d chars)",
                            path, len(report_md),
                        )
                    except Exception as e:
                        logger.warning(
                            "Failed to write auto-generated report: %s", e,
                        )

                self._persist_ledger(ledger)
                logger.warning(
                    "Safety: REPORT-phase stall (no write_file, "
                    "round %d) — auto-generated report (%d chars)",
                    self._model_call_count, len(report_md),
                )

        # ── Post-response: detect UNDERSTAND round-1 zero-tools stall ──
        # If the model returned no tool calls on the very first round
        # while still in understand phase with no hypotheses, the agent
        # loop would exit without collecting any data.
        # Fix: programmatically inject synthetic task() tool_calls to
        # force data collection.  This does NOT depend on LLM behaviour
        # — the agent loop will execute the injected tools normally.
        try:
            ledger = self._current_ledger
            if (
                ledger
                and self._model_call_count == 1
                and ledger.get("current_phase", "understand") == "understand"
                and not ledger.get("hypotheses")
            ):
                ai_msgs = [
                    m for m in (response.result
                                if isinstance(response.result, list)
                                else [response.result])
                    if hasattr(m, "tool_calls")
                ]
                has_tool_calls = any(
                    getattr(m, "tool_calls", None) for m in ai_msgs
                )
                if not has_tool_calls:
                    import uuid as _uuid
                    from langchain_core.messages import AIMessage

                    forced_calls = [
                        {
                            "name": "task",
                            "id": f"call_safety_{_uuid.uuid4().hex[:12]}",
                            "args": {
                                "subagent_type": "k8s-argus-expert",
                                "description": (
                                    "查询 K8s 集群 Argus 监控指标时序"
                                    "（节点状态、Pod 重启、API 延迟、"
                                    "DNS 延迟）"
                                ),
                            },
                        },
                        {
                            "name": "task",
                            "id": f"call_safety_{_uuid.uuid4().hex[:12]}",
                            "args": {
                                "subagent_type": "host-argus-expert",
                                "description": (
                                    "查询主机 Argus 监控指标时序"
                                    "（CPU、内存、磁盘 IO、网络）"
                                ),
                            },
                        },
                    ]

                    injected = False
                    for m in ai_msgs:
                        if hasattr(m, "tool_calls"):
                            m.tool_calls = forced_calls
                            # Replace hallucinated text with a system
                            # directive so the next LLM round is not
                            # confused by its own hallucination.
                            if hasattr(m, "content"):
                                m.content = (
                                    "[系统安全阀] 检测到第1轮未调用"
                                    "任何工具。系统已强制委派 Argus "
                                    "专家采集监控数据。请等待专家返回"
                                    "数据后再进行分析。"
                                )
                            injected = True
                            break

                    if injected:
                        logger.warning(
                            "Safety: round-1 understand-phase "
                            "zero-tools stall — injected 2 "
                            "synthetic task() calls"
                        )
                    else:
                        # No AIMessage with tool_calls attr — fall
                        # through to server-side fallback below.
                        raise ValueError(
                            "No suitable AIMessage for injection"
                        )
        except Exception as e:
            # Fallback: injection failed — generate a minimal report
            # server-side so the diagnosis is not completely empty.
            logger.warning(
                "Safety: round-1 injection failed (%s), "
                "falling back to server-side report generation", e,
            )
            try:
                ledger = self._current_ledger
                if ledger and not ledger.get("report"):
                    report_md = (
                        "# 诊断未完成\n\n"
                        "模型在第1轮未调用任何诊断工具，"
                        "无法采集数据。\n"
                        "请重新发起诊断。"
                    )
                    ledger["report"] = report_md
                    if self.report_path:
                        import pathlib as _pathlib
                        real_path = self.report_path.replace(
                            "/agent_data/",
                            str(self._AGENT_DATA_REAL) + "/",
                        )
                        path = _pathlib.Path(real_path)
                        path.parent.mkdir(parents=True, exist_ok=True)
                        path.write_text(report_md, encoding="utf-8")
                    self._persist_ledger(ledger)
                    logger.info(
                        "Fallback: wrote minimal report for "
                        "round-1 zero-tools stall"
                    )
            except Exception as e2:
                logger.error(
                    "Safety: fallback report generation also "
                    "failed: %s", e2,
                )

        # Sync ledger back to state via ExtendedModelResponse so it persists
        # across rounds and survives SummarizationMiddleware compression.
        if self._current_ledger is not None:
            return ExtendedModelResponse(
                model_response=response,
                command=Command(update={"_diagnosis_ledger": self._current_ledger}),
            )
        return response

    # ── Auto evidence collection ──

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command]],
    ) -> ToolMessage | Command:
        tool_name = request.tool_call.get("name", "")
        tool_call_id = request.tool_call.get("id", "")
        tool_args = request.tool_call.get("args", {})

        # ── Capture invocation order BEFORE any async work ──
        # LangGraph executes multiple tool calls from a single LLM
        # response concurrently.  Completion order (and thus
        # record_round insertion order) may not match the LLM's
        # intended ordering.  This sequence counter preserves the
        # original invocation order.
        self._round_record_seq += 1
        invocation_seq = self._round_record_seq

        # ── P0: Tool deduplication + circuit breaker ──
        # Blocks identical (name, args) calls.  If a tool previously
        # failed and is being retried, the breaker auto-escalates after
        # _TOOL_FAILURE_BREAKER consecutive failures.
        _DEDUP_SKIP = _SCAFFOLDING_TOOLS | _LEDGER_TOOLS | {"task"}
        if tool_name not in _DEDUP_SKIP:
            cache_key = _make_cache_key(tool_name, tool_args)
            if cache_key in self._tool_call_cache:
                cached_msg, fail_count = self._tool_call_cache[cache_key]
                if fail_count == 0:
                    # Previous call succeeded → normal cache hit
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
            # Memory cache miss: check if another agent (subagent or
            # Coordinator from earlier task) already called this tool
            # and persisted the result to the shared backend.
            if self._backend and tool_name not in _DEDUP_SKIP:
                backend_key = f"{_DEDUP_CACHE_PREFIX}/{cache_key}"
                try:
                    cached_content = await self._backend.aread(backend_key)
                except Exception:
                    cached_content = None
                if cached_content:
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

        # ── P1: Block coordinator from calling expert-only tools ──
        # query_argus_* tools are exclusively available to Argus expert
        # subagents (host-argus-expert / k8s-argus-expert).  If the
        # coordinator tries to call them directly, return a redirect.
        _EXPERT_ONLY_TOOLS = frozenset({
            "query_argus_cpu", "query_argus_memory",
            "query_argus_disk", "query_argus_network",
            "query_argus_nodes", "query_argus_services",
            "query_argus_gpu",
        })
        if tool_name in _EXPERT_ONLY_TOOLS:
            logger.warning(
                "Blocked coordinator from calling expert-only tool: %s",
                tool_name,
            )
            return ToolMessage(
                content=(
                    f"⚠️ {tool_name} 是专家专用工具，Coordinator 不能直接调用。\n"
                    "请通过 task() 委派对应专家：\n"
                    "- 主机指标(CPU/内存/磁盘/网络) → "
                    "task('host-argus-expert', '查询主机Argus指标时序')\n"
                    "- K8s集群指标(节点/Pod/API/DNS) → "
                    "task('k8s-argus-expert', '查询K8s Argus指标时序')"
                ),
                tool_call_id=tool_call_id,
            )

        # ── Inject ledger context into task() descriptions ──
        # Subagents normally see only the Coordinator's description and have
        # no access to the diagnosis ledger.  By appending a summary of
        # already-collected evidence, the subagent can skip redundant tool
        # calls and focus on its specific verification task.
        if tool_name == "task":
            ledger = self._current_ledger
            if ledger:
                ctx = _build_subagent_context(ledger)
                if ctx:
                    desc = tool_args.get("description", "")
                    tool_args["description"] = (
                        f"{desc}\n\n---\n"
                        f"[Coordinator 基线数据 — 供交叉验证]\n"
                        f"⚠ 不得重复调用下面列出的工具。基线数据可作为时序趋势参考，"
                        f"但你的分析必须有独立推理，不能仅重述 Coordinator 数据\n"
                        f"{ctx}"
                    )
                    try:
                        request.tool_call["args"] = tool_args
                    except (TypeError, AttributeError, KeyError):
                        pass  # request is immutable; proceed without injection

        # Emit precise tool timing events via stream_writer
        sw = getattr(request.runtime, "stream_writer", None)
        if sw:
            try:
                sw({
                    "type": "tool_executing",
                    "tool_call_id": tool_call_id,
                    "tool_name": tool_name,
                })
            except Exception:
                pass

        result = await handler(request)

        # ── Cache result with failure tracking ──
        # Only cache ToolMessage results (Command objects indicate
        # routing overrides and should not be cached).
        _DEDUP_SKIP = _SCAFFOLDING_TOOLS | _LEDGER_TOOLS | {"task"}
        if tool_name not in _DEDUP_SKIP and isinstance(result, ToolMessage):
            cache_key = _make_cache_key(tool_name, tool_args)
            content = result.content if isinstance(result.content, str) else str(result.content)
            fail_count = 1 if _is_tool_failure(content) else 0
            self._tool_call_cache[cache_key] = (result, fail_count)

            # ── Persist to shared_backend for cross-agent dedup ──
            # Both Coordinator and subagents write here so that any
            # agent can detect duplicate (name, args) calls across
            # the entire session, not just within its own process.
            if self._backend and not fail_count:
                backend_key = f"{_DEDUP_CACHE_PREFIX}/{cache_key}"
                try:
                    await self._backend.awrite(backend_key, content)
                except Exception:
                    pass  # best-effort; memory cache still works

            # ── Offload to shared backend for cross-round access ──
            # Writes the full tool result to the filesystem so the LLM
            # can use read_file / grep to retrieve data from previous
            # rounds instead of re-calling the same tool.
            if self._backend and content:
                # Encode tool_name + args hash in path so different
                # parameters produce distinct files (same tool, different
                # args = NOT a duplicate call).
                safe_name = _sanitize_path_component(tool_name)
                # Use first 12 chars of cache_key suffix for uniqueness
                args_suffix = cache_key.split(":", 1)[1] if ":" in cache_key else ""
                safe_suffix = _sanitize_path_component(args_suffix)[:24] if args_suffix else "noargs"
                file_path = (
                    f"{self._tool_results_prefix}/r{self._model_call_count}"
                    f"/{safe_name}_{safe_suffix}.txt"
                )
                try:
                    await self._backend.awrite(file_path, content)
                    logger.debug("Offloaded tool result: %s → %s", tool_name, file_path)
                except Exception:
                    file_path = ""  # best-effort: don't block diagnosis
                    logger.debug("Offload write skipped for %s (backend error)", tool_name)

                # Store metadata in ledger for context injection
                lines = content.splitlines()
                ledger = self._current_ledger
                if ledger is not None:
                    ledger.setdefault("tool_results", {})[cache_key] = {
                        "path": file_path,
                        "round": self._model_call_count,
                        "preview": content[:200].replace("\n", " "),
                        "lines": len(lines),
                        "is_failure": fail_count > 0,
                    }
                logger.info(
                    "Cross-round tool result saved: %s → %s (%d lines)",
                    cache_key, file_path, len(lines),
                )

        # Use the in-memory ledger (shared via self._current_ledger)
        ledger = self._current_ledger
        if ledger is None:
            return result

        # Extract output for all non-scaffolding tools (used by both
        # evidence collection and round recording below).
        if tool_name not in _SCAFFOLDING_TOOLS:
            output = ""
            if isinstance(result, ToolMessage):
                output = result.content if isinstance(result.content, str) else str(result.content)
            elif isinstance(result, Command):
                for msg in result.update.get("messages", []):
                    if isinstance(msg, ToolMessage):
                        output = msg.content if isinstance(msg.content, str) else str(msg.content)
                        break

            # ── Auto-record evidence (skip ledger tools — they self-manage) ──
            # record_finding already writes coordinator evidence to the correct
            # hypothesis; auto-collection would create a duplicate routed to
            # active_path (always H1) regardless of which hypothesis is targeted.
            if tool_name not in _LEDGER_TOOLS:
                source = f"expert:{tool_args.get('subagent_type', '?')}" if tool_name == "task" else f"tool:{tool_name}"
                summary = output or ""

                if summary:
                    # ── Hypothesis-aware evidence routing ──
                    if tool_name == "task":
                        target_hid = _parse_hypothesis_id_from_description(
                            tool_args.get("description", ""),
                            ledger.get("hypotheses", {}),
                        )
                        if target_hid and target_hid in ledger.get("hypotheses", {}):
                            node = ledger["hypotheses"][target_hid]
                            node["evidence"].append(new_evidence(
                                source, summary, supports=True, tool_call_id=tool_call_id,
                            ))
                            if tool_name not in node["verification_tools"]:
                                node["verification_tools"].append(tool_name)
                        else:
                            add_evidence_to_active(
                                ledger, source, summary,
                                supports=True,
                                tool_call_id=tool_call_id,
                                tool_name=tool_name,
                            )
                    else:
                        add_evidence_to_active(
                            ledger, source, summary,
                            supports=True,
                            tool_call_id=tool_call_id,
                            tool_name=tool_name,
                        )

            # Build key_findings from tool output (first 200 chars)
            key_findings = ""
            if output:
                key_findings = output[:200].replace("\n", " ").strip()

            record_round(
                ledger, derive_phase(ledger),
                tools_called=[tool_name],
                action_summary=tool_args.get("description", "") if tool_name == "task" else tool_name,
                key_findings=key_findings,
                _seq=invocation_seq,
            )

        # After ledger management tools, persist and emit snapshot
        if tool_name in _LEDGER_TOOLS:
            # Track record_finding calls for stagnation detection
            if tool_name == "record_finding":
                self._consecutive_task_count = 0
                self._same_delegate_count = 0
                self._last_delegate_key = ""
                self._last_finding_round = self._model_call_count
            # commit_hypotheses marks the UNDERSTAND→VERIFY boundary;
            # reset stagnation counters so that UNDERSTAND-phase Argus task()
            # calls do not accumulate into the VERIFY-phase threshold.
            elif tool_name == "commit_hypotheses":
                self._consecutive_task_count = 0
            self._persist_ledger(ledger)
            # Emit ledger snapshot via stream_writer for frontend
            try:
                import langgraph.context as _lgctx
                sw = getattr(request.runtime, "stream_writer", None)
                if sw and ledger:
                    from diagnostics.agent.ledger import ledger_to_json
                    import json
                    sw({
                        "type": "ledger_snapshot",
                        "ledger": _sanitize_ledger(ledger),
                    })
            except Exception:
                pass  # stream_writer not available in all contexts
        elif tool_name == "task":
            # Track consecutive task() calls without record_finding (stagnation)
            self._consecutive_task_count += 1
            self._last_task_round = self._model_call_count

            # Track per-(expert, hypothesis) delegation count (saturation)
            expert = tool_args.get("subagent_type", "?")
            hid = _parse_hypothesis_id_from_description(
                tool_args.get("description", ""),
                ledger.get("hypotheses", {}),
            ) or "?"
            delegate_key = f"{expert}:{hid}"
            if delegate_key == self._last_delegate_key:
                self._same_delegate_count += 1
            else:
                self._same_delegate_count = 1
                self._last_delegate_key = delegate_key

        # Capture report content when write_file writes to the pre-generated
        # report path — emit for frontend answer-body without text parsing.
        if tool_name == "write_file" and self.report_path:
            file_path = tool_args.get("file_path") or tool_args.get("path") or ""
            if file_path and _paths_match(file_path, self.report_path):
                content = tool_args.get("content") or tool_args.get("text") or ""
                if content:
                    # Backfill report content into the ledger so that
                    # result.json carries the full report text alongside
                    # the structured hypothesis data.
                    if ledger:
                        ledger["report"] = content

                    try:
                        sw = getattr(request.runtime, "stream_writer", None)
                        if sw:
                            sw({
                                "type": "report_content",
                                "content": content,
                            })
                    except Exception:
                        pass

                # Auto-finalize any hypotheses still in non-terminal state
                # when the report is written.  The LLM may have skipped
                # record_finding for indirectly refuted hypotheses (e.g.
                # H2 verification data also disproves H3, but the LLM
                # jumps directly to REPORT without calling record_finding
                # on the indirectly refuted hypothesis).  Similarly,
                # low-probability hypotheses may remain "pending" if
                # the LLM confirmed a high-confidence root cause and
                # proceeded directly to report generation.
                if ledger:
                    hypotheses = ledger.get("hypotheses", {})
                    finalized_any = False
                    for hid, node in hypotheses.items():
                        status = node.get("status")
                        if status == "verifying":
                            # Only auto-confirm when coordinator (record_finding)
                            # evidence with supports=True exists — this indicates
                            # the LLM explicitly confirmed the hypothesis but
                            # write_file was called before the finding was saved.
                            # Auto-collected expert evidence always has
                            # supports=True regardless of actual verdict, so
                            # it is NOT a reliable confirmation signal.
                            has_coordinator_support = any(
                                e.get("supports")
                                and e.get("source") == "coordinator"
                                for e in node.get("evidence", [])
                            )
                            if has_coordinator_support:
                                record_finding(
                                    ledger, hid, "confirmed",
                                    "诊断报告已生成，系统根据已有协调员确认证据自动确认。",
                                    node.get("probability", 75),
                                )
                            else:
                                # No explicit coordinator confirmation —
                                # mark as dead_end (inconclusive) to preserve
                                # the actual verification state.
                                record_finding(
                                    ledger, hid, "inconclusive",
                                    "诊断报告已生成，该假设验证未得出明确结论，系统标记为未完成。",
                                    node.get("probability", 50),
                                )
                            finalized_any = True
                        elif status == "pending":
                            record_finding(
                                ledger, hid, "refuted",
                                "诊断报告已生成，核心假设已确认，该假设未被验证，系统自动关闭。",
                                node.get("probability", 5),
                            )
                            finalized_any = True
                    if finalized_any:
                        ledger["current_phase"] = derive_phase(ledger)
                    # Always persist after write_file so that report content
                    # is saved to the ledger JSON regardless of whether
                    # auto-finalize ran.  Without this, the report field is
                    # lost when all hypotheses were already in terminal state.
                    self._persist_ledger(ledger)
                    if finalized_any:
                        # Emit final ledger snapshot so the frontend receives
                        # the correct hypothesis statuses.
                        try:
                            sw_snap = getattr(request.runtime, "stream_writer", None)
                            if sw_snap:
                                sw_snap({
                                    "type": "ledger_snapshot",
                                    "ledger": _sanitize_ledger(ledger),
                                })
                        except Exception:
                            pass

        # Emit tool completion timing
        if sw:
            try:
                sw({
                    "type": "tool_completed",
                    "tool_call_id": tool_call_id,
                    "tool_name": tool_name,
                })
            except Exception:
                pass

        return result

    # ── Safety mechanisms: max-round, loop detection, stagnation fallback ──

    def _build_safety_warnings(self, ledger: DiagnosisLedger) -> str:
        """Build safety directive messages injected into the system prompt.

        Safety mechanisms prevent infinite loops and premature termination:

        1. **Max rounds (P1)**: Hard limit on LLM rounds — forces REPORT phase.
        2. **Understand stagnation (P2/P3)**: When round >= 2 but no hypotheses
           committed, inject a strong directive to force commit_hypotheses.
           Runs independently of the min-round-for-safety threshold.
        3. **Stagnation detection (P2/P3)**: After ≥3 consecutive task() calls
           without record_finding, auto-records an inconclusive finding for
           the active hypothesis and warns the LLM.
        4. **Loop warning (P2)**: Weaker early warning when ≥3 task() calls
           pile up before the stagnation threshold triggers auto-recording.

        Returns an empty string when no warnings apply.
        """
        round_num = self._model_call_count
        warnings: list[str] = []

        # ── P1: Hard maximum round limit ──
        if round_num >= _MAX_ROUNDS:
            ledger["current_phase"] = "report"
            self._report_phase_start_round = round_num
            warnings.append(
                "⛔ [系统强制] 已达到最大诊断轮次限制"
                f"（{_MAX_ROUNDS}轮）。\n"
                "禁止再调用任何诊断工具或委派专家。\n"
                f"必须立即调用 write_file 将诊断报告写入 {self.report_path}。\n"
                "即使证据不完整，也必须给出最佳判断和建议。"
            )
            logger.warning(
                "Safety: max rounds (%d) reached — forcing REPORT phase",
                round_num,
            )

        # ── Understand-phase stagnation safety valve ──
        # Runs independently of _MIN_ROUND_FOR_SAFETY: if the LLM has
        # completed round 1 (data collection) but round 2 starts without
        # any hypotheses, inject a hard directive.
        # Without this, the LLM may output analysis as plain text instead
        # of calling commit_hypotheses, causing the agent loop to exit.
        # Differentiate between:
        #   (a) Diagnostic data WAS collected → force commit_hypotheses
        #   (b) Diagnostic data was NOT collected → force data collection
        if (
            round_num >= 2
            and not ledger.get("hypotheses")
        ):
            rounds_history = ledger.get("rounds", [])
            _data_collection_tools = frozenset({
                "task",
                "query_argus_nodes", "query_argus_services",
                "query_argus_cpu",
                "query_argus_memory", "query_argus_disk",
                "query_argus_network", "query_argus_gpu",
                "check_kubernetes_pods", "check_kubernetes_nodes",
                "check_gpu_nodes",
            })
            has_collected_data = any(
                _data_collection_tools & set(r.get("tools_called", []))
                for r in rounds_history
            )

            if has_collected_data:
                # Data collected but no hypotheses — force commit
                warnings.append(
                    "⚠ [系统强制] 数据采集已完成（第1轮），当前是第"
                    f"{round_num}轮。你尚未提交任何假设。\n"
                    "禁止继续调用诊断工具或输出纯文本分析。\n"
                    "你必须在本轮调用 commit_hypotheses 提交至少1个假设，"
                    "否则诊断将提前终止。\n"
                    "基于上一轮已获取的 Argus 数据，提出假设并立即调用 "
                    "commit_hypotheses。"
                )
                logger.warning(
                    "Safety: understand stagnation with data (round %d, "
                    "no hypotheses) — forcing commit",
                    round_num,
                )
            else:
                # No data collected — force diagnostic data collection
                warnings.append(
                    "⚠ [系统强制] 当前是第"
                    f"{round_num}轮，你尚未提交任何假设，"
                    "且未调用任何诊断工具采集数据。\n"
                    "禁止输出纯文本分析，禁止调用 commit_hypotheses。\n"
                    "你必须在本轮调用 task() 委派 Argus 专家"
                    "（argus_kubernetes / argus_hosts）或主机专家"
                    "采集集群和主机的监控指标数据。\n"
                    "这是诊断的第一步——没有数据就无法形成假设。"
                )
                logger.warning(
                    "Safety: understand stagnation without data (round %d, "
                    "no hypotheses, no diagnostic tools) — forcing "
                    "data collection",
                    round_num,
                )

        # ── Delegation saturation — prevent redundant re-delegation ──
        # Detects the pattern: task(expert, Hn) → record_finding →
        # task(same expert, same Hn) repeated.  Uses per-key tracking
        # (_same_delegate_count) so that delegating to different experts
        # or for different hypotheses does not trigger false positives.
        # IMPORTANT: Only fire after at least one record_finding call
        # (_last_finding_round > 0), so UNDERSTAND-phase Argus expert
        # tasks do not trigger premature saturation warnings that would
        # cause the Coordinator to skip VERIFY expert delegation.
        if (
            ledger.get("hypotheses")
            and self._same_delegate_count >= 2
            and round_num < _MAX_ROUNDS
            and self._last_finding_round > 0
        ):
            warnings.append(
                f"⚠ [系统警告] 已连续{self._same_delegate_count}次"
                f"委派相同专家验证相同假设（{self._last_delegate_key}），"
                "此前已获得验证结论。\n"
                "专家的工具集有限，重复委派不会获得新数据。\n"
                "- 若已确认核心事实，仅缺少次要细节（如启动者、触发源），"
                "应基于已有证据给出最终判断并生成报告\n"
                "- 若有其他未验证的假设，应切换到其他假设验证\n"
                "- 禁止再次委派相同专家查询相同假设"
            )
            logger.warning(
                "Safety: delegation saturation (same_key=%s, count=%d, "
                "round=%d, phase=%s) — warning injected",
                self._last_delegate_key, self._same_delegate_count,
                round_num, ledger.get("current_phase", "?"),
            )

        # ── P2/P3: Stagnation & loop detection (only after min round) ──
        # Independent if-block (not elif) so it runs regardless of
        # delegation saturation check above.
        if round_num >= _MIN_ROUND_FOR_SAFETY:
            current_phase = ledger.get("current_phase", "")
            has_hypotheses = bool(ledger.get("hypotheses"))

            if self._consecutive_task_count >= _STAGNATION_THRESHOLD:
                if current_phase == "verify" and has_hypotheses:
                    # ── P3: Auto-record inconclusive for active hypothesis ──
                    active_path = ledger.get("active_path", [])
                    active_id = active_path[-1] if active_path else None
                    if active_id and active_id in ledger.get("hypotheses", {}):
                        record_finding(
                            ledger, active_id, "inconclusive",
                            f"连续{self._consecutive_task_count}次委派专家"
                            f"均未获得可判定结论（第{round_num}轮），"
                            "系统自动标记为不确定。",
                            ledger["hypotheses"][active_id]["probability"],
                        )
                        should_exit, exit_reason, _ = check_exit_conditions(ledger)
                        if should_exit:
                            ledger["current_phase"] = "report"
                            self._report_phase_start_round = round_num
                            warnings.append(
                                f"⚠ [系统安全阀] {exit_reason}\n"
                                "系统已自动将活跃假设标记为不确定。\n"
                                "必须立即进入REPORT阶段，调用 write_file 基于已有证据生成诊断报告。"
                            )
                        else:
                            phase = derive_phase(ledger)
                            ledger["current_phase"] = phase
                            warnings.append(
                                f"⚠ [系统自动记录] 验证阶段超时"
                                f"（连续{self._consecutive_task_count}次委派"
                                "无明确结论），已自动标记为不确定。\n"
                                f"当前阶段: {phase}。"
                                "请根据新阶段指示继续诊断或生成报告。"
                            )
                        self._persist_ledger(ledger)
                        self._consecutive_task_count = 0
                        self._same_delegate_count = 0
                        self._last_delegate_key = ""
                        self._last_finding_round = round_num
                        logger.warning(
                            "Safety: auto-inconclusive for %s (round %d, "
                            "consecutive_task=%d)",
                            active_id, round_num,
                            self._consecutive_task_count,
                        )
                else:
                    # ── P2: Loop warning without auto-recording ──
                    warnings.append(
                        f"⚠ [系统警告] 已连续{self._consecutive_task_count}次"
                        "委派专家但未记录验证结论。\n"
                        "工具已返回足够数据，请分析已有证据并调用"
                        " record_finding 记录结论，或直接生成报告。\n"
                        "避免重复委派专家查询相同数据。"
                    )
                    logger.warning(
                        "Safety: %d consecutive task() calls without "
                        "record_finding (round %d, phase=%s)",
                        self._consecutive_task_count, round_num, current_phase,
                    )

        # ── Verify-phase stuck detection ──
        # Safety net: when in verify phase, round >= 8, and the active
        # hypothesis has expert evidence but hasn't been finalized via
        # record_finding, force progression to REPORT.  This catches
        # the case where _consecutive_task_count is low (reset by
        # commit_hypotheses) so the normal stagnation threshold is
        # not reached, but the LLM still fails to call record_finding.
        if (
            round_num >= 8
            and ledger.get("current_phase") == "verify"
            and ledger.get("hypotheses")
            and "verify" not in "\n".join(warnings)  # no verify warning already
        ):
            active_path = ledger.get("active_path", [])
            active_id = active_path[-1] if active_path else None
            if active_id and active_id in ledger.get("hypotheses", {}):
                node = ledger["hypotheses"][active_id]
                has_expert = any(
                    e.get("source", "").startswith("expert:")
                    for e in node.get("evidence", [])
                )
                has_coordinator_support = any(
                    e.get("supports") and e.get("source") == "coordinator"
                    for e in node.get("evidence", [])
                )
                if has_expert and node.get("status") == "verifying":
                    if has_coordinator_support:
                        record_finding(
                            ledger, active_id, "confirmed",
                            f"[\u7cfb\u7edf\u81ea\u52a8] \u5df2\u83b7\u5f97\u4e13\u5bb6\u9a8c\u8bc1\u8bc1\u636e\u4e14\u534f\u8c03\u5458\u5df2\u786e\u8ba4"
                            f"\uff08\u7b2c{round_num}\u8f6e\uff09\uff0c\u7cfb\u7edf\u81ea\u52a8\u786e\u8ba4\u3002",
                            node["probability"],
                        )
                    else:
                        record_finding(
                            ledger, active_id, "inconclusive",
                            f"[\u7cfb\u7edf\u81ea\u52a8] \u5df2\u83b7\u5f97\u4e13\u5bb6\u9a8c\u8bc1\u8bc1\u636e\u4f46\u672a\u5f97\u5230\u534f\u8c03\u5458\u786e\u8ba4"
                            f"\uff08\u7b2c{round_num}\u8f6e\uff09\uff0c\u7cfb\u7edf\u6807\u8bb0\u4e3a\u672a\u5b8c\u6210\u3002",
                            node["probability"],
                        )
                    ledger["current_phase"] = "report"
                    self._report_phase_start_round = round_num
                    warnings.append(
                        "\u26d4 [\u7cfb\u7edf\u5b89\u5168\u9600] verify \u9636\u6bb5\u505c\u6ede\u8fc7\u4e45"
                        f"\uff08\u7b2c{round_num}\u8f6e\uff09\uff0c\u6d3b\u8dc3\u5047\u8bbe {active_id} \u5df2"
                        "\u83b7\u5f97\u4e13\u5bb6\u8bc1\u636e\u4f46\u672a\u8bb0\u5f55\u7ed3\u8bba\u3002\u7cfb\u7edf\u5df2\u81ea\u52a8\u5904\u7406\u3002\n"
                        "\u5fc5\u987b\u7acb\u5373\u8c03\u7528 write_file \u751f\u6210\u8bca\u65ad\u62a5\u544a\u3002"
                    )
                    self._persist_ledger(ledger)
                    self._consecutive_task_count = 0
                    self._same_delegate_count = 0
                    self._last_delegate_key = ""
                    self._last_finding_round = round_num
                    logger.warning(
                        "Safety: verify-phase stuck for %s (round %d) "
                        "— auto-confirmed, forced REPORT",
                        active_id, round_num,
                    )

        # ── EVALUATE-phase guard: suppress REPORT messages while
        # actionable root hypotheses are still pending.  Prevents the
        # system from prematurely injecting REPORT-directives (via
        # safety valves or phase-derivation edge cases) when the LLM
        # should be evaluating remaining hypotheses first.
        current_phase = ledger.get("current_phase", "")
        _pending_roots: list[str] = []  # only populated in evaluate phase
        if current_phase == "evaluate":
            root_ids = ledger.get("root_hypothesis_ids", [])
            _all_hypotheses = ledger.get("hypotheses", {})
            _pending_roots = [
                hid for hid in root_ids
                if (node := _all_hypotheses.get(hid))
                and node.get("status") in ("pending", "verifying")
                and node.get("probability", 0) > 15
            ]
        # _pending_roots is empty for non-evaluate phases (initialized above)

        # ── REPORT phase write_file safety valve ──
        # If the LLM entered REPORT phase but didn't call write_file,
        # inject a progressively stronger directive.
        # SKIP when pending root hypotheses remain in EVALUATE phase.
        if (current_phase == "report"
                and not ledger.get("report")
                and not _pending_roots):
            # Track when REPORT phase started
            if self._report_phase_start_round == 0:
                self._report_phase_start_round = round_num
            elif round_num - self._report_phase_start_round >= 1:
                # LLM has been in REPORT for 2+ rounds without write_file
                warnings.append(
                    "⛔ [系统强制] 你已进入 REPORT 阶段"
                    f"（第{self._report_phase_start_round}轮起）"
                    f"，但尚未调用 write_file 生成报告。\n"
                    "禁止调用任何诊断工具或委派专家。\n"
                    f"必须立即调用 write_file 将诊断报告写入 {self.report_path}。\n"
                    "报告内容基于上方 <diagnosis_ledger> 中的证据链和根因。"
                )
                logger.warning(
                    "Safety: REPORT phase round %d without write_file "
                    "(started round %d) — forcing write_file",
                    round_num, self._report_phase_start_round,
                )
        elif current_phase != "report":
            # Reset tracker when leaving REPORT phase (e.g. backtrack)
            self._report_phase_start_round = 0

        return "\n\n".join(warnings) if warnings else ""

    # ── Tool: commit_hypotheses ──

    def _make_commit_hypotheses_tool(self) -> StructuredTool:
        mw = self

        async def _run(
            hypotheses: list[HypothesisSpec],
            parent_hypothesis_id: str | None = None,
            focus_summary: str = "",
        ) -> str:
            ledger = mw._current_ledger or new_ledger()

            # Convert Pydantic objects to plain dicts (args_schema parses JSON)
            specs = [h.model_dump() if hasattr(h, "model_dump") else dict(h) for h in hypotheses]

            try:
                created = add_hypotheses(ledger, specs, parent_hypothesis_id)
            except ValueError as e:
                return f"假设提交失败: {e}"

            # Update phase
            ledger["current_phase"] = derive_phase(ledger)

            # Build confirmation
            lines = [f"已提交 {len(created)} 个假设:"]
            for h in created:
                sel = " ★" if h["selected"] else ""
                lines.append(f"  {h['id']} [{h['status']} p={h['probability']}%]{sel} {h['statement']}")

            mw._persist_ledger(ledger)
            mw._current_ledger = ledger

            return "\n".join(lines)

        return StructuredTool.from_function(
            name="commit_hypotheses",
            description=(
                "提交诊断假设（HYPOTHESIZE阶段必须调用）。每层最多3个假设，按概率降序。"
                "若在已确认假设下深化，传入parent_hypothesis_id。"
                "提交后系统自动选择概率最高的假设进入验证。"
            ),
            coroutine=_run,
            args_schema=CommitHypothesesInput,
        )

    # ── Tool: select_path ──

    def _make_select_path_tool(self) -> StructuredTool:
        mw = self

        async def _run(
            selected_hypothesis_id: str,
            rationale: str = "",
            deprioritized: list[DeprioritizedSpec] | None = None,
        ) -> str:
            ledger = mw._current_ledger
            if ledger is None:
                return "错误: 诊断台账不存在"

            # Convert Pydantic objects to plain dicts
            dep_dicts = [d.model_dump() if hasattr(d, "model_dump") else dict(d)
                         for d in (deprioritized or [])]

            try:
                select_path(ledger, selected_hypothesis_id, rationale, dep_dicts)
            except ValueError as e:
                return f"路径选择失败: {e}"

            ledger["current_phase"] = derive_phase(ledger)

            selected = ledger["hypotheses"][selected_hypothesis_id]
            mw._persist_ledger(ledger)
            mw._current_ledger = ledger

            return (
                f"已选择路径: {selected_hypothesis_id} {selected['statement']}\n"
                f"理由: {rationale}\n"
                f"活动路径: {' → '.join(ledger['active_path'])}"
            )

        return StructuredTool.from_function(
            name="select_path",
            description=(
                "选择1条最接近根因的假设路径深入（EVALUATE阶段必须调用）。"
                "未选中的假设自动降级为deprioritized，可后续回溯。"
            ),
            coroutine=_run,
            args_schema=SelectPathInput,
        )

    # ── Tool: record_finding ──

    def _make_record_finding_tool(self) -> StructuredTool:
        mw = self

        async def _run(
            hypothesis_id: str,
            verdict: str,
            evidence_summary: str,
            probability_update: int,
            new_insights: str = "",
            statement_update: str = "",
        ) -> str:
            ledger = mw._current_ledger
            if ledger is None:
                return "错误: 诊断台账不存在"

            # Capture old statement for root_causes sync
            old_statement = ""
            if statement_update and hypothesis_id in ledger.get("hypotheses", {}):
                hnode = ledger["hypotheses"][hypothesis_id]
                # Defect C: refuted hypotheses must not have their statement
                # modified — the original hypothesis statement should be
                # preserved for historical accuracy.
                if hnode.get("status") == "refuted":
                    return (
                        f"⚠️ 假设 {hypothesis_id} 已被证伪(status=refuted)，"
                        "禁止修改假设表述(statement_update)。"
                        "请使用新的证据描述发现，不要修改已排除假设的原始表述。"
                    )
                old_statement = hnode.get("statement", "")

            # Defect D: Terminal-state guard — hypotheses already in
            # confirmed/refuted must not be re-recorded.  This prevents
            # the LLM from accidentally overwriting a correct verdict
            # (e.g. calling record_finding(H1, refuted) after already
            # calling record_finding(H1, confirmed)).
            if hypothesis_id in ledger.get("hypotheses", {}):
                _hnode = ledger["hypotheses"][hypothesis_id]
                _terminal = _hnode.get("status")
                if _terminal in ("confirmed", "refuted"):
                    return (
                        f"⚠️ 假设 {hypothesis_id} 已处于终态"
                        f"(status={_terminal}, "
                        f"p={_hnode.get('probability')}%)，"
                        "不允许重复记录验证结论。"
                        "如需修正，请使用新假设(commit_hypotheses)"
                        "或回溯(backtrack)。"
                    )

            try:
                record_finding(
                    ledger, hypothesis_id, verdict,
                    evidence_summary, probability_update, new_insights,
                    statement_update,
                )
            except ValueError as e:
                return f"记录失败: {e}"

            # Check exit conditions
            should_exit, reason, confirmed_id = check_exit_conditions(ledger)

            # ── Multi-root: accumulate ALL confirmed root causes ──
            # Iterate over every root hypothesis — not just the single
            # confirmed_id returned by check_exit_conditions (which is
            # always the first confirmed hypothesis).  This ensures that
            # in multi-root scenarios (e.g. memory_leak_and_disk_full),
            # each confirmed root cause is tracked independently.
            cids: list[str] = ledger.setdefault("root_cause_hypothesis_ids", [])
            causes: list[str] = ledger.setdefault("root_causes", [])
            for rid in ledger.get("root_hypothesis_ids", []):
                node = ledger["hypotheses"].get(rid)
                if node and node["status"] == "confirmed" and rid not in cids:
                    cids.append(rid)
                    stmt = node["statement"]
                    # Deduplicate: different hypotheses may converge on
                    # the same root cause statement after statement_update.
                    if stmt not in causes:
                        causes.append(stmt)
            # Keep backward-compat fields in sync with first entry
            if cids:
                ledger["root_cause"] = causes[0]
                ledger["root_cause_hypothesis_id"] = cids[0]

            # Sync root_causes text when statement was updated for an
            # already-confirmed hypothesis (e.g. "etcd compaction" → "etcd NOSPACE").
            if old_statement and statement_update:
                causes_list = ledger.get("root_causes", [])
                for i, rc in enumerate(causes_list):
                    if rc == old_statement:
                        causes_list[i] = statement_update
                        break
                # Also update backward-compat field
                if ledger.get("root_cause") == old_statement:
                    ledger["root_cause"] = statement_update

            # ── Cleanup: remove non-confirmed hypotheses from
            # root_cause_hypothesis_ids.  Ensures that refuted or
            # deprioritized hypotheses do not appear as root causes.
            hypotheses_dict = ledger.get("hypotheses", {})
            cids[:] = [
                cid for cid in cids
                if hypotheses_dict.get(cid, {}).get("status") == "confirmed"
            ]
            # Rebuild root_causes to match cleaned cids
            causes[:] = [
                hypotheses_dict[cid]["statement"]
                for cid in cids
                if cid in hypotheses_dict
            ]
            # Re-sync backward-compat fields after cleanup
            if cids:
                ledger["root_cause"] = causes[0] if causes else ""
                ledger["root_cause_hypothesis_id"] = cids[0]
            else:
                ledger.pop("root_cause", None)
                ledger.pop("root_cause_hypothesis_id", None)

            # ── Defect E: Sort root_cause_hypothesis_ids by probability
            # descending so the highest-confidence root cause is always
            # primary, regardless of record_finding call order. ──
            if len(cids) > 1:
                cids.sort(
                    key=lambda _c: hypotheses_dict.get(_c, {}).get("probability", 0),
                    reverse=True,
                )
                causes[:] = [
                    hypotheses_dict[_c]["statement"]
                    for _c in cids
                    if _c in hypotheses_dict
                ]
                ledger["root_cause"] = causes[0] if causes else ""
                ledger["root_cause_hypothesis_id"] = cids[0]

            if should_exit:
                ledger["current_phase"] = "report"
            elif "部分根因" in reason:
                # Some roots confirmed, others pending — guide LLM to continue
                ledger["current_phase"] = "evaluate"
            else:
                ledger["current_phase"] = derive_phase(ledger)

            mw._persist_ledger(ledger)
            mw._current_ledger = ledger

            if should_exit:
                exit_hint = (
                    f"\n⚠ {reason} — 进入REPORT阶段。\n"
                    "⛔ 你必须在本轮调用 write_file 将诊断报告写入"
                    f" {mw.report_path}。\n"
                    "禁止调用任何诊断工具，禁止输出纯文本分析。"
                    "立即基于 <diagnosis_ledger> 中的证据链生成报告并调用 write_file。"
                )
                # Pre-set report phase tracker so safety valve fires
                # immediately if LLM ignores the directive.
                mw._report_phase_start_round = mw._model_call_count
            else:
                exit_hint = ""
            stmt_hint = f"\nℹ 假设表述已修正为: {statement_update}" if statement_update else ""
            return (
                f"已记录验证结果: {hypothesis_id} → {verdict} (p={probability_update}%)\n"
                f"{evidence_summary}{stmt_hint}{exit_hint}"
            )

        return StructuredTool.from_function(
            name="record_finding",
            description=(
                "记录假设验证结论（VERIFY阶段后必须调用）。"
                "verdict: confirmed(已证实，可同时存在多个确认的根因/加剧因素)"
                " | refuted(证据明确证伪，该假设不成立)"
                " | inconclusive(证据不足)。"
                "注意：多个假设可以同时为confirmed（多根因场景），refuted仅用于证据明确否定的假设。"
                "所有root假设均到达终态且至少1个confirmed时自动进入REPORT阶段。"
            ),
            coroutine=_run,
            args_schema=RecordFindingInput,
        )

    # ── Tool: backtrack ──

    def _make_backtrack_tool(self) -> StructuredTool:
        mw = self

        async def _run() -> str:
            ledger = mw._current_ledger
            if ledger is None:
                return "错误: 诊断台账不存在"

            new_active_id = backtrack(ledger)
            if new_active_id is None:
                ledger["current_phase"] = "report"
                mw._persist_ledger(ledger)
                mw._current_ledger = ledger
                # Pre-set report phase tracker for safety valve
                mw._report_phase_start_round = mw._model_call_count
                return (
                    "回溯失败: 没有可回溯的假设，所有路径已穷尽。\n"
                    "已进入 REPORT 阶段。\n"
                    "⛔ 你必须在本轮调用 write_file 将诊断报告写入"
                    f" {mw.report_path}。\n"
                    "禁止调用任何诊断工具，立即基于 <diagnosis_ledger> "
                    "中的证据链生成报告。"
                )

            ledger["current_phase"] = derive_phase(ledger)
            new_active = ledger["hypotheses"][new_active_id]
            mw._persist_ledger(ledger)
            mw._current_ledger = ledger

            return (
                f"已回溯到: {new_active_id} {new_active['statement']}\n"
                f"活动路径: {' → '.join(ledger['active_path'])}\n"
                f"当前阶段: {ledger['current_phase']}"
            )

        return StructuredTool.from_function(
            name="backtrack",
            description=(
                "回溯到最近的 deprioritized 或 pending 假设重新验证"
                "（BACKTRACK 阶段必须调用，或在 EVALUATE 阶段全部 refuted 时调用）。"
                "如果没有可回溯的假设，自动进入 REPORT 阶段。"
            ),
            coroutine=_run,
        )

    # ── Auto-report generation (P11 safety net) ──

    def _generate_report_from_ledger(self, ledger: DiagnosisLedger) -> str:
        """Generate a diagnostic report from ledger evidence.

        Used as a last-resort safety net when the LLM exits the REPORT
        phase without calling write_file (P11).
        """
        import datetime as _dt

        lines: list[str] = []
        lines.append("# 故障诊断报告\n")
        lines.append(
            f"**诊断日期**：{_dt.datetime.now().strftime('%Y-%m-%d')}"
        )

        # Fault overview from root causes
        root_causes = ledger.get("root_causes", [])
        if root_causes:
            lines.append(
                f"**故障概述**：{root_causes[0]}"
            )

        # Confidence from confirmed hypotheses
        hypotheses = ledger.get("hypotheses", {})
        confirmed = [
            h for h in hypotheses.values() if h.get("status") == "confirmed"
        ]
        if confirmed:
            max_p = max(h.get("probability", 0) for h in confirmed)
            level = (
                "高" if max_p >= 80
                else "中" if max_p >= 50
                else "低"
            )
            lines.append(f"**根因置信度**：{level}（{max_p}%）")
        lines.append("")

        # Root cause analysis
        if root_causes:
            lines.append("## 根因分析\n")
            for i, rc in enumerate(root_causes, 1):
                lines.append(f"{i}. {rc}")
            lines.append("")

        # Evidence chain
        lines.append("## 证据链与时间线\n")
        for hid, h in hypotheses.items():
            status = h.get("status", "unknown")
            p = h.get("probability", 0)
            stmt = h.get("statement", "")
            status_cn = {
                "confirmed": "✅已确认",
                "refuted": "❌已排除",
                "deprioritized": "⬇已降级",
                "verifying": "🔄验证中",
                "pending": "⏳待验证",
            }.get(status, status)
            lines.append(f"- **{hid}** [{status_cn} p={p}%] {stmt}")
            for e in h.get("evidence", []):
                src = e.get("source", "")
                summ = e.get("summary", "")[:200]
                if summ:
                    lines.append(f"  - [{src}] {summ}")
        lines.append("")

        # Excluded hypotheses
        refuted = [
            h for h in hypotheses.values()
            if h.get("status") in ("refuted", "deprioritized")
        ]
        if refuted:
            lines.append("## 排除的假设\n")
            for h in refuted:
                lines.append(
                    f"- {h.get('statement', '')}"
                    f"（{h.get('status')}，概率{h.get('probability', 0)}%）"
                )
            lines.append("")

        # Repair suggestions (placeholder)
        lines.append("## 修复建议\n")
        lines.append("（请根据根因分析结果制定具体修复方案）")

        return "\n".join(lines)

    # ── Persistence ──

    # Root of the agent_data/ directory on the real filesystem.
    # _persist_ledger receives a virtual path (/agent_data/traces/...)
    # and must map it to the real project directory.
    _AGENT_DATA_REAL = pathlib.Path(__file__).resolve().parent.parent.parent / "agent_data"

    def _persist_ledger(self, ledger: DiagnosisLedger) -> None:
        """Persist ledger to filesystem (best-effort).

        The ledger_path uses a virtual /agent_data/ prefix (matching the
        FilesystemBackend route).  We resolve it against the real project
        agent_data/ directory so the file lands in the correct location.
        """
        if not self.ledger_path:
            return
        try:
            # Convert virtual /agent_data/... to real filesystem path
            vpath = self.ledger_path
            real_path = vpath.replace("/agent_data/", str(self._AGENT_DATA_REAL) + "/")
            path = pathlib.Path(real_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(ledger_to_json(ledger), encoding="utf-8")
            logger.debug("Ledger persisted to %s", path)
        except Exception as e:
            logger.warning("Failed to persist ledger (path=%s): %s", self.ledger_path, e)
