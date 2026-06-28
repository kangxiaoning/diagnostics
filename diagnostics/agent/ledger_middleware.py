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
from langchain_core.messages import HumanMessage, ToolMessage
from langchain_core.tools import StructuredTool
from langgraph.prebuilt.tool_node import ToolCallRequest
from langgraph.types import Command
from pydantic import BaseModel, Field

from diagnostics.agent.ledger import (
    DiagnosisLedger,
    DiagnosisLedgerState,
    add_evidence_to_active,
    add_hypotheses,
    backtrack,
    check_exit_conditions,
    derive_phase,
    ledger_to_json,
    new_evidence,
    new_fault_profile,
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
        lines.append(f"已调用工具: {', '.join(diagnostic_tools)}")

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


def _sanitize_ledger(ledger: dict) -> dict:
    """Strip internal fields from ledger before sending to frontend/streaming."""
    internal_keys = {"_inconclusive_streak", "_backtrack_count"}
    return {k: v for k, v in ledger.items() if k not in internal_keys}


def _paths_match(tool_path: str, report_path: str) -> bool:
    """Check if tool_path targets the same file as report_path."""
    if not tool_path or not report_path:
        return False
    # Normalise: strip leading /, compare tails (tool may use relative or absolute)
    return tool_path.lstrip("/").rstrip("/") == report_path.lstrip("/").rstrip("/")


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


class FaultProfileSchema(BaseModel):
    """Structured output schema for extracting fault profile from user input."""

    entities: list[str] = Field(
        description="故障实体列表（如集群名、节点名、服务名、主机名）",
    )
    symptoms: list[str] = Field(
        description="观察到的故障症状列表（如 P99延迟飙升、OOMKilled、磁盘满）",
    )
    timeline: str = Field(
        default="",
        description="故障时间线描述（如 '15:03 开始'、'最近1小时'）",
    )
    recent_changes: str = Field(
        default="",
        description="故障前的最近变更（如部署、配置变更），无则为空字符串",
    )
    prior_actions: str = Field(
        default="",
        description="用户已尝试的修复操作，无则为空字符串",
    )


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
    ) -> None:
        self.ledger_path = ledger_path
        self.report_path = report_path
        self._model = model
        self._current_ledger: DiagnosisLedger | None = None
        self._model_call_count: int = 0
        # Safety mechanism tracking
        self._consecutive_task_count: int = 0
        self._last_finding_round: int = 0
        self._last_task_round: int = 0
        self.tools: list = [
            self._make_commit_hypotheses_tool(),
            self._make_select_path_tool(),
            self._make_record_finding_tool(),
            self._make_backtrack_tool(),
        ]

    # ── Fault profile extraction via structured output ──

    async def abefore_agent(self, state, runtime) -> dict | None:
        """Extract fault_profile from user input using structured output.

        Runs once before the agent loop starts.  Uses the chat model's
        ``with_structured_output`` to reliably parse entities, symptoms,
        timeline, etc. from the first HumanMessage — eliminating the
        dependency on the LLM voluntarily calling a tool.
        """
        if self._model is None:
            return None

        # Only extract once — skip if ledger already has a fault_profile
        # or if this is not the first invocation (ledger already exists).
        if self._current_ledger is not None:
            return None

        messages = state.get("messages", [])
        user_text = ""
        for msg in messages:
            if isinstance(msg, HumanMessage):
                user_text = msg.content if isinstance(msg.content, str) else str(msg.content)
                break
        if not user_text:
            return None

        try:
            # Use function_calling method because Qwen/LM Studio does not
            # support the json_schema response_format.  The schema is wrapped
            # as a tool and the model responds via a tool call.
            structured_model = self._model.with_structured_output(
                FaultProfileSchema, method="function_calling",
            )
            profile: FaultProfileSchema = await structured_model.ainvoke(
                [
                    {"role": "system", "content": (
                        "从用户故障描述中提取结构化故障画像。"
                        "提取实体（集群、节点、服务名）、症状、时间线、最近变更和已尝试操作。"
                        "如果某项信息未提及，使用空字符串或空列表。"
                    )},
                    {"role": "user", "content": user_text},
                ],
            )
        except Exception as e:
            logger.warning("Failed to extract fault_profile via structured output: %s", e)
            # Graceful fallback: create ledger without fault_profile so
            # diagnosis can proceed.  The LLM will still extract symptom
            # information in its text reasoning.
            ledger = new_ledger()
            self._current_ledger = ledger
            return {"_diagnosis_ledger": ledger}

        ledger = new_ledger()
        ledger["fault_profile"] = new_fault_profile(
            entities=profile.entities,
            symptoms=profile.symptoms,
            user_raw=user_text,
            timeline=profile.timeline,
            recent_changes=profile.recent_changes,
            prior_actions=profile.prior_actions,
        )
        self._current_ledger = ledger

        logger.info(
            "Fault profile extracted: entities=%s, symptoms=%s",
            profile.entities, profile.symptoms,
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
                        f"{desc}\n\n---\n[已收集的诊断数据 — 无需重复查询]\n{ctx}"
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
            )

        # After ledger management tools, persist and emit snapshot
        if tool_name in _LEDGER_TOOLS:
            # Track record_finding calls for stagnation detection
            if tool_name == "record_finding":
                self._consecutive_task_count = 0
                self._last_finding_round = self._model_call_count
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
            # Track consecutive task() calls without record_finding (loop detection)
            self._consecutive_task_count += 1
            self._last_task_round = self._model_call_count

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
                            record_finding(
                                ledger, hid, "refuted",
                                "诊断报告已生成，该假设在验证其他假设时被间接证据证伪，系统自动终结。",
                                5,
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
                        self._persist_ledger(ledger)
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
            warnings.append(
                "⛔ [系统强制] 已达到最大诊断轮次限制"
                f"（{_MAX_ROUNDS}轮）。\n"
                "禁止再调用任何诊断工具或委派专家。\n"
                "必须立即基于已有证据生成诊断报告。\n"
                "即使证据不完整，也必须给出最佳判断和建议。"
            )
            logger.warning(
                "Safety: max rounds (%d) reached — forcing REPORT phase",
                round_num,
            )

        # ── P2/P3: Understand stagnation — force commit_hypotheses ──
        # Runs independently of _MIN_ROUND_FOR_SAFETY: if the LLM has
        # completed round 1 (data collection) but round 2 starts without
        # any hypotheses, inject a hard directive to commit hypotheses NOW.
        # Without this, the LLM may output analysis as plain text instead
        # of calling commit_hypotheses, causing the agent loop to exit.
        if (
            round_num >= 2
            and ledger.get("fault_profile") is None
            and not ledger.get("hypotheses")
        ):
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
                "Safety: understand stagnation detected (round %d, "
                "no hypotheses, no fault_profile) — forcing commit",
                round_num,
            )

        # ── Delegation saturation — prevent redundant re-delegation ──
        # Detects the pattern: task() → record_finding → task() again.
        # Even though record_finding resets _consecutive_task_count,
        # reaching count >= 2 means the Coordinator already obtained
        # findings but is re-delegating instead of progressing.
        # This is a generic behavior-pattern check — no assumptions
        # about specific tools, hypotheses, or failure modes.
        if (
            ledger.get("hypotheses")
            and self._consecutive_task_count >= 2
            and round_num < _MAX_ROUNDS
        ):
            warnings.append(
                f"⚠ [系统警告] 已连续{self._consecutive_task_count}次"
                "委派专家验证，此前已获得验证结论。\n"
                "专家的工具集有限，重复委派不会获得新数据。\n"
                "- 若已确认核心事实，仅缺少次要细节（如启动者、触发源），"
                "应基于已有证据给出最终判断并生成报告\n"
                "- 若有其他未验证的假设，应切换到其他假设验证\n"
                "- 禁止再次委派专家查询相同假设"
            )
            logger.warning(
                "Safety: delegation saturation (count=%d, round=%d, "
                "phase=%s) — warning injected",
                self._consecutive_task_count, round_num,
                ledger.get("current_phase", "?"),
            )

        # ── P2/P3: Stagnation & loop detection (only after min round) ──
        elif round_num >= _MIN_ROUND_FOR_SAFETY:
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
                            warnings.append(
                                f"⚠ [系统安全阀] {exit_reason}\n"
                                "系统已自动将活跃假设标记为不确定。\n"
                                "必须立即进入REPORT阶段，基于已有证据生成诊断报告。"
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
                old_statement = ledger["hypotheses"][hypothesis_id].get("statement", "")

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

            # ── Multi-root: accumulate confirmed root causes ──
            # When a hypothesis is confirmed, record it in the root_causes list
            # even if other root hypotheses are still pending.  Only transition
            # to REPORT when all root hypotheses are resolved.
            if confirmed_id and "根因確認" not in reason and "根因确认" in reason:
                # Single-root confirmed → traditional exit path
                pass
            if confirmed_id and confirmed_id in ledger.get("hypotheses", {}):
                node = ledger["hypotheses"][confirmed_id]
                cids: list[str] = ledger.setdefault("root_cause_hypothesis_ids", [])
                causes: list[str] = ledger.setdefault("root_causes", [])
                if confirmed_id not in cids:
                    cids.append(confirmed_id)
                    causes.append(node["statement"])
                    # Keep backward-compat fields in sync with first entry
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

            if should_exit:
                ledger["current_phase"] = "report"
            elif "部分根因" in reason:
                # Some roots confirmed, others pending — guide LLM to continue
                ledger["current_phase"] = "evaluate"
            else:
                ledger["current_phase"] = derive_phase(ledger)

            mw._persist_ledger(ledger)
            mw._current_ledger = ledger

            exit_hint = f"\n⚠ {reason} — 进入REPORT阶段生成报告" if should_exit else ""
            stmt_hint = f"\nℹ 假设表述已修正为: {statement_update}" if statement_update else ""
            return (
                f"已记录验证结果: {hypothesis_id} → {verdict} (p={probability_update}%)\n"
                f"{evidence_summary}{stmt_hint}{exit_hint}"
            )

        return StructuredTool.from_function(
            name="record_finding",
            description=(
                "记录假设验证结论（VERIFY阶段后必须调用）。"
                "verdict: confirmed(已证实) | refuted(已排除) | inconclusive(证据不足)。"
                "confirmed且概率≥80%且假设足够具体时自动进入REPORT阶段。"
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
                return (
                    "回溯失败: 没有可回溯的假设，所有路径已穷尽。\n"
                    "已进入 REPORT 阶段，请基于现有证据生成诊断报告。"
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
