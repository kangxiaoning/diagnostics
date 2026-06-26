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
    check_exit_conditions,
    derive_phase,
    ledger_to_json,
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
    "commit_hypotheses", "select_path", "record_finding",
})


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
            summary = (output or "")[:300]

            if summary:
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
            should_exit, reason = check_exit_conditions(ledger)
            if should_exit:
                ledger["current_phase"] = "report"
                if "根因确认" in reason:
                    node = ledger["hypotheses"][hypothesis_id]
                    ledger["root_cause"] = node["statement"]
                    ledger["root_cause_hypothesis_id"] = hypothesis_id
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

    # ── Persistence ──

    def _persist_ledger(self, ledger: DiagnosisLedger) -> None:
        """Persist ledger to filesystem (best-effort)."""
        if not self.ledger_path:
            return
        try:
            import pathlib
            path = pathlib.Path(self.ledger_path.lstrip("/"))
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(ledger_to_json(ledger), encoding="utf-8")
            logger.debug("Ledger persisted to %s", self.ledger_path)
        except Exception as e:
            logger.warning("Failed to persist ledger: %s", e)
