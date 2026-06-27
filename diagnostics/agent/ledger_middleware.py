"""Diagnosis ledger middleware — hypothesis-driven diagnosis orchestration.

This middleware:
1. Injects a compact ledger summary into the system message each LLM call
2. Provides three hypothesis management tools (commit_hypotheses, select_path, record_finding)
3. Auto-records evidence from diagnostic tool calls
4. Persists the ledger to the filesystem on key changes

See: private/HYPOTHESIS_LEDGER_DESIGN.md
"""

from __future__ import annotations

import logging
import pathlib
import re
from collections.abc import Awaitable, Callable

from langchain.agents.middleware.types import (
    AgentMiddleware,
    ExtendedModelResponse,
    ModelRequest,
    ModelResponse,
)
from langchain_core.messages import ToolMessage
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


# ── Middleware ──

class DiagnosisLedgerMiddleware(AgentMiddleware):
    """Hypothesis-driven diagnosis ledger middleware."""

    state_schema = DiagnosisLedgerState

    def __init__(self, ledger_path: str = "", report_path: str = "") -> None:
        self.ledger_path = ledger_path
        self.report_path = report_path
        self._current_ledger: DiagnosisLedger | None = None
        self._model_call_count: int = 0
        self.tools: list = [
            self._make_commit_hypotheses_tool(),
            self._make_select_path_tool(),
            self._make_record_finding_tool(),
            self._make_backtrack_tool(),
        ]

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
        if context_block:
            new_sys = append_to_system_message(request.system_message, context_block)
            new_request = request.override(system_message=new_sys)
            response = await handler(new_request)
        else:
            response = await handler(request)

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

        # Auto-record evidence for diagnostic tools and expert delegations
        if tool_name not in _SCAFFOLDING_TOOLS:
            output = ""
            if isinstance(result, ToolMessage):
                output = result.content if isinstance(result.content, str) else str(result.content)
            elif isinstance(result, Command):
                for msg in result.update.get("messages", []):
                    if isinstance(msg, ToolMessage):
                        output = msg.content if isinstance(msg.content, str) else str(msg.content)
                        break

            source = f"expert:{tool_args.get('subagent_type', '?')}" if tool_name == "task" else f"tool:{tool_name}"
            summary = output or ""

            if summary:
                # ── Hypothesis-aware evidence routing ──
                # For task() calls, parse the description to find which
                # hypothesis is being verified (e.g. "验证假设 H2") and
                # route evidence there.  Fall back to active_path when
                # the description doesn't mention a specific hypothesis.
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

            record_round(
                ledger, derive_phase(ledger),
                tools_called=[tool_name],
                action_summary=tool_args.get("description", "") if tool_name == "task" else tool_name,
            )

        # After ledger management tools, persist and emit snapshot
        if tool_name in _LEDGER_TOOLS:
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

        # Capture report content when write_file writes to the pre-generated
        # report path — emit for frontend answer-body without text parsing.
        if tool_name == "write_file" and self.report_path:
            file_path = tool_args.get("file_path") or tool_args.get("path") or ""
            if file_path and _paths_match(file_path, self.report_path):
                content = tool_args.get("content") or tool_args.get("text") or ""
                if content:
                    try:
                        sw = getattr(request.runtime, "stream_writer", None)
                        if sw:
                            sw({
                                "type": "report_content",
                                "content": content,
                            })
                    except Exception:
                        pass

                # Auto-finalize any hypotheses still in "verifying" state
                # when the report is written.  The LLM may have skipped
                # record_finding for indirectly refuted hypotheses (e.g.
                # H2 verification data also disproves H3, but the LLM
                # jumps directly to REPORT without calling record_finding
                # on the indirectly refuted hypothesis).
                if ledger:
                    hypotheses = ledger.get("hypotheses", {})
                    finalized_any = False
                    for hid, node in hypotheses.items():
                        if node.get("status") == "verifying":
                            record_finding(
                                ledger, hid, "refuted",
                                "诊断报告已生成，该假设在验证其他假设时被间接证据证伪，系统自动终结。",
                                5,
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
        ) -> str:
            ledger = mw._current_ledger
            if ledger is None:
                return "错误: 诊断台账不存在"

            try:
                record_finding(
                    ledger, hypothesis_id, verdict,
                    evidence_summary, probability_update, new_insights,
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
            return (
                f"已记录验证结果: {hypothesis_id} → {verdict} (p={probability_update}%)\n"
                f"{evidence_summary}{exit_hint}"
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
