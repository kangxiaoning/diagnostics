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
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.tools import StructuredTool
from langgraph.prebuilt.tool_node import ToolCallRequest
from langgraph.types import Command
from pydantic import AliasChoices, BaseModel, Field

from deepagents.backends.protocol import BackendProtocol

from diagnostics.agent.ledger import (
    KAPPA_STOP,
    MAX_COMMIT_CALLS,
    DiagnosisLedger,
    DiagnosisLedgerState,
    _SBR_KNOWN_PHASES,
    _coverage_ready,
    _economically_dead,
    _parse_hypothesis_num,
    activate_hypothesis,
    add_evidence_to_active,
    add_hypotheses,
    backtrack,
    can_commit_root_hypotheses,
    check_exit_conditions,
    delegation_value,
    derive_phase,
    finalize_pending_for_report,
    fmt_hid,
    ledger_to_json,
    new_evidence,
    new_ledger,
    parse_beliefs_field,
    parse_root_cause_field,
    record_finding,
    record_round,
    render_ledger_context,
    resolve_hypothesis_id,
    select_path,
    strip_diagnosis_status_blocks,
)
from deepagents.middleware._utils import append_to_system_message

logger = logging.getLogger(__name__)


# ── V2 phase helpers (state-machine-v2.md §3) ──
# The ledger no longer persists ``current_phase``; the phase is derived
# on demand.  Two flavours are needed inside this middleware:
#
#   _phase(ledger)          — derived phase, honouring the forced-REPORT
#                             marker below (guardrail equivalent of the
#                             old committed phase).
#   _force_report(ledger)   — guardrail "force REPORT" action: marks the
#                             ledger so every subsequent derivation lands
#                             on REPORT via check_exit_conditions-friendly
#                             data, plus the explicit _forced_terminal flag
#                             consumed by write_file gating and finalize.
#
# Legacy ledger JSON may still carry a stale ``current_phase`` key; it is
# ignored everywhere (never read, never written by new code).

def _phase(ledger: dict) -> str:
    """Effective phase: explicit REPORT lock > forced terminal > derived."""
    if ledger.get("report"):
        return "report"
    if ledger.get("_forced_terminal"):
        return "report"
    return derive_phase(ledger)


def _force_report(ledger: dict) -> None:
    """Guardrail entry into REPORT.

    Sets the terminal marker consumed by the write_file gate, the
    finalize guard, and derive_phase's step-1 REPORT lock — once the
    marker is set, a stale selected/active hypothesis can no longer
    pull the derived phase back to "verify".  The phase itself is NOT
    stored (derived on demand, P1).
    """
    ledger["_forced_terminal"] = True


# ── Phase tool gating (state-machine-v2.md §7) ──
# Tools REJECTED per phase for the Coordinator.  Anything not listed is
# allowed; read-only scaffolding (read_file/grep/glob/ls/write_todos) is
# never gated.  The gate exists to make illegal phases fail fast with a
# corrective message instead of silently corrupting the ledger.
# NOTE: write_file appears in NO phase gate — it is guarded exclusively
# by the dedicated write_file gate in awrap_tool_call (exit conditions
# based), so a legitimate report write from any phase is never killed by
# phase bookkeeping lag (see the evaluate comment below).
# Required args of the deepagents file tools — used ONLY for an
# observability warning when the model emits a malformed (empty-args)
# tool call.  Not a gate: schema validation produces the actual error
# returned to the LLM.
_FILE_TOOL_REQUIRED_ARGS: dict[str, tuple[str, ...]] = {
    "write_file": ("file_path", "content"),
    "read_file": ("file_path",),
    "edit_file": ("file_path", "old_string", "new_string"),
}

_PHASE_TOOL_GATE: dict[str, frozenset[str]] = {
    # UNDERSTAND collects data only — committing hypotheses before the
    # coverage gate opens would skip HYPOTHESIZE entirely.
    "understand": frozenset({
        "commit_hypotheses", "record_finding", "select_path", "backtrack",
    }),
    # HYPOTHESIZE only commits; no findings/paths/delegations yet
    # (§7 whitelist: commit_hypotheses + read_file).  Letting task()
    # through here would pull VERIFY behaviour forward and expose the
    # G5 delegation-value gate outside its designed phase.
    "hypothesize": frozenset({
        "record_finding", "select_path", "backtrack", "task",
    }),
    # VERIFY delegates + records findings; no commits/paths.
    "verify": frozenset({
        "commit_hypotheses", "select_path", "backtrack",
    }),
    # EVALUATE picks direction; recording a finding is ALSO legal here
    # (v2.8.1: record_finding is an evidence-ledgering EVENT, not a phase
    # milestone — Statecharts shared-event semantics, valid in VERIFY as
    # the T3 transition and in EVALUATE as an internal self-transition).
    # Gating it in evaluate contradicted the evaluate guidance itself
    # ("confirmed 假设需更具体 → record_finding(statement_update=...)";
    # multi-root secondary confirmations) and the v2.7.1 confirmed→confirmed
    # strengthening channel, which has no legal path back to verify
    # (select_path/backtrack/auto-heal all target pending/inconclusive
    # only).  Production evidence: 2026-07-25 "record_finding blocked by
    # phase gate in evaluate" ×5 (round 5 retries).  Semantic guards stay
    # in the tool handler (Defect D anti-flip, refuted frozen, ID
    # validation), so allowing it here opens no abuse surface.
    # write_file is deliberately NOT gated here — the dedicated write_file
    # gate above (check_exit_conditions / _forced_terminal / report) is
    # the correct guard for "may I write the report now".  Gating
    # write_file by phase would kill the legitimate escape hatch where
    # exit conditions are met while the derived phase still reads
    # "evaluate" (observed 2026-07-20 32-round session: round-14/18
    # write_file from evaluate was rejected by the phase gate, the report
    # content was lost twice, and diagnosis wandered for 18 more rounds).
    # REPORT only writes the report; diagnosis tools are locked out.
    "report": frozenset({
        "commit_hypotheses", "record_finding", "select_path", "backtrack",
        "task",
    }),
}
# NOTE: report_beliefs (SBR v2 self-report) is intentionally absent from
# every denylist entry above — it is phase-agnostic process metadata and
# must be callable in ALL phases, including REPORT.

_PHASE_ALLOWED_HINT: dict[str, str] = {
    "understand": "task(*-argus-expert), read_file",
    "hypothesize": "commit_hypotheses",
    "verify": "task(*-expert), record_finding",
    "evaluate": "select_path / commit_hypotheses / backtrack / record_finding",
    "report": "write_file",
}


def _gate_hint(phase: str) -> str:
    return _PHASE_ALLOWED_HINT.get(phase, phase)

# Tools that are scaffolding (not diagnostic). "task" is included because
# expert delegation results are recorded by the Coordinator via record_finding.
# report_beliefs is process metadata (SBR v2 self-report) — it never
# advances the diagnosis itself, so it is scaffolding for round-recording
# classification, and non-substantive for every stall predicate.
_SCAFFOLDING_TOOLS = frozenset({
    "write_file", "read_file", "edit_file",
    "write_todos", "read_todos",
    "ls", "glob", "grep",
    "report_beliefs",
})

# Ledger management tools (handled by this middleware)
_LEDGER_TOOLS = frozenset({
    "commit_hypotheses", "select_path", "record_finding", "backtrack",
})

# Self-report tools (SBR v2) — excluded from every "substantive activity"
# predicate: a response containing ONLY report_beliefs calls must still
# count as loop-ending (G9/G12 stall handlers) and as a ledger-idle round
# (G14), because reporting beliefs produces no evidence and no ledger
# progress.  Not in _LEDGER_TOOLS: it doesn't manage hypotheses.
_BELIEF_TOOLS = frozenset({"report_beliefs"})

# ── Safety mechanism thresholds ──
_MAX_ROUNDS = 40                    # G1 轮次上限（简单场景，state-machine-v2.md §8）：超过此轮次强制 REPORT
_MAX_ROUNDS_COMPLEX = 24            # Hard limit when diagnosis needed a retry batch:
                                    # a diagnosis that needed a second batch or sub-hypotheses is
                                    # already past the healthy budget — further rounds show
                                    # diminishing returns (observed 2026-07-20 32-round session:
                                    # 18 unproductive rounds after the root cause was already
                                    # confirmed at p=75).
_MAX_ROUNDS_COMPLEX_GRACE = 5       # Smaller grace for the complex-scenario cap
                                    # (24→40: room for 2 root batches + sub-hypothesis
                                    # deepening + multi-root diagnosis; progress-aware
                                    # grace means this only fires on true stagnation)
_MAX_ROUNDS_GRACE = 10              # Extra rounds granted when diagnosis is progressing
_PROGRESS_WINDOW = 5                # Recent-round window used to detect progress
_STAGNATION_THRESHOLD = 3           # G3 停滞阈值（state-machine-v2.md §8）：连续 task() 无 record_finding 判定为 VERIFY 委派无结论
_MIN_ROUND_FOR_SAFETY = 3           # Don't activate safety checks before this round
_VERIFY_STUCK_GAP = 3               # Rounds without verdict/evidence progress → verify-stuck
_VERIFY_STUCK_COOLDOWN = 3          # Min rounds between two verify-stuck interventions
_MODEL_CALL_TIMEOUT = int(os.getenv("DIAGNOSTICS_MODEL_TIMEOUT", "300"))  # seconds per LLM call attempt
_REPORT_PHASE_TIMEOUT = int(os.getenv("DIAGNOSTICS_REPORT_TIMEOUT", "600"))  # report phase generates a long report — longer timeout per attempt

# ── G7' model-call retry (state-machine-v2.md §8) ──
# A transient failure (timeout / connection break / 5xx / 429) is retried
# with exponential backoff BEFORE the G7 fallback fires, so a single
# network blip no longer terminates the whole diagnosis.  Retries run
# inside awrap_model_call — before_model has already executed — so round
# and safety counters are untouched; model calls are side-effect-free
# (tools have not executed), which makes retrying safe.
_MODEL_MAX_ATTEMPTS = max(1, int(os.getenv("DIAGNOSTICS_MODEL_MAX_ATTEMPTS", "3")))  # total attempts (1 initial + retries); floor 1
_MODEL_RETRY_BACKOFF_BASE = float(os.getenv("DIAGNOSTICS_MODEL_RETRY_BASE", "2"))  # backoff base seconds (doubles per attempt)
_MODEL_RETRY_BACKOFF_MAX = float(os.getenv("DIAGNOSTICS_MODEL_RETRY_BACKOFF_MAX", "30"))  # backoff cap seconds

# ── Expert verdict text-signal patterns ───────────────────────────────────
# Used by verify-stall detection to infer verdict from expert subagent
# output when the LLM fails to call record_finding.

_CONFIRMED_PATTERNS = re.compile(
    r"假设验证[：:]\s*\*{0,2}confirmed|"
    r"\*{0,2}confirmed\*{0,2}\s*[（(]|"
    r"假设成立|假设.*已确认|根因确认|验证结论.*成立|"
    r"verdict.*confirmed",
    re.IGNORECASE,
)

_REFUTED_PATTERNS = re.compile(
    r"假设验证[：:]\s*\*{0,2}refuted|"
    r"\*{0,2}refuted\*{0,2}\s*[（(]|"
    r"假设不成立|假设.*已排除|验证结论.*不成立|"
    r"verdict.*refuted",
    re.IGNORECASE,
)


def _infer_verdict_from_text(text: str) -> str | None:
    """Analyze expert output text for explicit verdict signals.

    Returns 'confirmed', 'refuted', or None if no clear signal found.
    Used by verify-stall detection to make reliable auto-decisions
    instead of blindly defaulting to inconclusive.
    """
    if not text:
        return None
    # Check confirmed signals first (more specific patterns)
    if _CONFIRMED_PATTERNS.search(text):
        return "confirmed"
    if _REFUTED_PATTERNS.search(text):
        return "refuted"
    return None


def _build_evidence_audit(ledger: dict) -> str:
    """Generate a transparent evidence quality report for REPORT phase.

    Provides the LLM with a multi-dimensional view of evidence quality
    for each hypothesis so it can calibrate report confidence.  The
    audit is informational only — no state is modified.
    """
    hypotheses = ledger.get("hypotheses", {})
    if not hypotheses:
        return ""

    confirmed = [(hid, h) for hid, h in hypotheses.items()
                 if h.get("status") == "confirmed"]

    if not confirmed:
        return ""

    lines = ["<evidence_audit>"]
    for hid, node in confirmed:
        evidence_list = node.get("evidence", [])
        # supports is tri-state: True/False/None(undetermined).
        # None counts as neither supporting nor refuting.
        supporting = [e for e in evidence_list if e.get("supports") is True]
        refuting = [e for e in evidence_list if e.get("supports") is False]
        expert_sources = {e.get("source") for e in evidence_list
                         if isinstance(e.get("source"), str)
                         and e.get("source", "").startswith("expert:")}
        multi_source = bool(expert_sources) and any(
            e.get("source") == "coordinator" for e in evidence_list
        )
        if len(supporting) >= 2 and multi_source:
            label = "充分"
        elif len(supporting) >= 1:
            label = "基本"
        else:
            label = "不足"
        lines.append(
            f"  {fmt_hid(hid)}: 证据{label} "
            f"({len(supporting)}条支持/{len(refuting)}条反对, "
            f"{len(expert_sources)}个专家来源, "
            f"{'多源交叉验证' if multi_source else '单一来源'})"
        )
    lines.append("</evidence_audit>")
    return "\n".join(lines)


# ── Empty-data patterns ─────────────────────────────────────────────────
# Patterns found in expert / tool responses that indicate NO meaningful
# data was returned.  Used by understand-phase safety valves to
# distinguish "collected rich data" from "collected nothing useful"
# and trigger early REPORT instead of forcing commit_hypotheses.

# Argus outcome classification patterns (state-machine-v2.md §8 G2, v2.5).
# Three semantically distinct outcomes must NOT be conflated:
_ARGUS_PARAM_MISSING_PATTERNS = (
    "需要补充",            # expert asking for missing params
    "需要更具体的信息",     # expert asking for clarification
)
_ARGUS_FAILURE_PATTERNS = (
    "无监控数据返回",
    "无数据返回",
    "查询失败",
    "执行失败",
    "调用失败",
    "报错",
)
_ARGUS_ALL_NORMAL_PATTERNS = (
    "全部指标无异常波动",   # all metrics normal (data IS available)
)


def _classify_argus_data(ledger: dict) -> str:
    """Classify Argus expert outcomes when every expert round is "empty".

    Returns one of:
      ""              — at least one expert returned real diagnostic data
                        (or no expert delegated yet): normal flow;
      "param_missing" — experts asked for missing params (hostname/time
                        window).  The delegation itself was malformed,
                        NOT an argus outage → re-delegate with full params;
      "failure"       — argus returned empty data or errored.  The argus
                        monitoring platform is considered unavailable →
                        degrade to user-input hypotheses (T1 bypass);
      "all_normal"    — data collected, all metrics normal.  Record
                        faithfully and continue the normal flow.
    """
    rounds = ledger.get("rounds", [])
    expert_rounds = [
        r for r in rounds
        if "task" in r.get("tools_called", [])
        and r.get("delegated_experts")
    ]
    if not expert_rounds:
        return ""  # no experts delegated yet — not "empty", just "not yet"

    saw_param_missing = False
    saw_failure = False
    saw_all_normal = False
    for r in expert_rounds:
        kf = r.get("key_findings", "")
        if any(p in kf for p in _ARGUS_PARAM_MISSING_PATTERNS):
            saw_param_missing = True
        elif any(p in kf for p in _ARGUS_FAILURE_PATTERNS):
            saw_failure = True
        elif any(p in kf for p in _ARGUS_ALL_NORMAL_PATTERNS):
            saw_all_normal = True
        else:
            return ""  # at least one expert returned real data

    # param_missing wins: re-delegation with correct params may fix all.
    if saw_param_missing:
        return "param_missing"
    if saw_failure:
        return "failure"
    if saw_all_normal:
        return "all_normal"
    return ""


def _build_diagnostic_synthesis(ledger: dict, max_rounds: int) -> str:
    """Generate a comprehensive diagnostic synthesis when the round limit is reached.

    Unlike the blunt "STOP NOW" directive, this produces a structured summary
    of everything the diagnostic process has learned — hypothesis findings,
    evidence quality, best conclusions, and a report template — so the LLM
    can produce a high-quality report even when interrupted mid-diagnosis.
    """
    hypotheses = ledger.get("hypotheses", {})
    rounds_data = ledger.get("rounds", [])
    current_round = ledger.get("current_round", 0)

    lines = ["<diagnostic_synthesis>"]
    lines.append(
        f"## 系统通知：已达到最大诊断轮次（{current_round}/{max_rounds}）"
    )
    lines.append(
        "诊断过程已自动终结。下方是你已收集的所有信息的综合总结，"
        "请基于这些信息调用 write_file 生成诊断报告。"
    )
    lines.append("")

    # ── 1. Journey overview ──
    lines.append("## 1. 诊断历程概览")
    total_tools = 0
    total_tasks = 0
    experts_used: set[str] = set()
    tools_used: set[str] = set()
    for rd in rounds_data:
        tools = rd.get("tools_called", [])
        total_tools += len(tools)
        if "task" in tools:
            total_tasks += 1
        experts_used.update(rd.get("delegated_experts", []))
        tools_used.update(tools)

    _scaff = {"task", "write_file", "read_file", "edit_file",
              "commit_hypotheses", "select_path", "record_finding", "backtrack",
              "ls", "glob", "grep", "write_todos", "read_todos"}
    diagnostic = sorted(tools_used - _scaff)
    lines.append(f"- 总轮次: {current_round} | 工具调用: {total_tools} 次 | 委派专家: {total_tasks} 次")
    if experts_used:
        lines.append(f"- 已委派专家: {', '.join(sorted(experts_used))}")
    if diagnostic:
        lines.append(f"- 诊断工具: {', '.join(diagnostic)}")
    lines.append("")

    # ── 2. Hypothesis findings ──
    if hypotheses:
        lines.append("## 2. 假设验证结果综合")
        confirmed = [(hid, h) for hid, h in hypotheses.items()
                     if h.get("status") == "confirmed"]
        refuted = [(hid, h) for hid, h in hypotheses.items()
                   if h.get("status") == "refuted"]
        unfinished = [(hid, h) for hid, h in hypotheses.items()
                      if h.get("status") in ("pending", "inconclusive")]
        root_ids = set(ledger.get("root_hypothesis_ids", []))

        if confirmed:
            lines.append("### ✅ 已确认假设")
            for hid, node in sorted(confirmed, key=lambda x: -x[1].get("probability", 0)):
                prob = node.get("probability", 0)
                is_root = hid in root_ids
                evidence = node.get("evidence", [])
                supporting = [e for e in evidence if e.get("supports") is True]
                refuting_ev = [e for e in evidence if e.get("supports") is False]
                expert_srcs = {e.get("source") for e in evidence
                              if isinstance(e.get("source"), str)
                              and e.get("source", "").startswith("expert:")}
                multi = (bool(expert_srcs)
                         and any(e.get("source") == "coordinator" for e in evidence))
                conf = "高" if prob >= 80 and multi else "中" if prob >= 60 else "低"
                lines.append(
                    f"- **{fmt_hid(hid)}**{' [根因]' if is_root else ''} "
                    f"置信度:{conf} 概率:{prob}% "
                    f"证据:{len(supporting)}支持/{len(refuting_ev)}反对 "
                    f"来源:{len(expert_srcs)}专家 "
                    f"{'✓交叉验证' if multi else '单一来源'}"
                )
                lines.append(f"  {node.get('statement', '')}")
                rationale = node.get("rationale", "")
                if rationale:
                    lines.append(f"  依据: {rationale}")
            lines.append("")

        if refuted:
            lines.append("### ❌ 已排除假设")
            for hid, node in refuted:
                rationale = node.get("rationale", "") or "(无记录)"
                lines.append(f"- **{fmt_hid(hid)}**: {node.get('statement', '')}")
                lines.append(f"  原因: {rationale}")
            lines.append("")

        if unfinished:
            lines.append("### ⚠ 因轮次限制中断的假设")
            lines.append("（应作为'后续排查建议'出现在报告中，不要丢弃）")
            for hid, node in unfinished:
                prob = node.get("probability", 0)
                evidence = node.get("evidence", [])
                supporting = [e for e in evidence if e.get("supports") is True]
                lines.append(
                    f"- **{fmt_hid(hid)}**: {node.get('statement', '')} "
                    f"(概率:{prob}%, 已收集{len(supporting)}条证据)"
                )
            lines.append("")
    else:
        lines.append("## 2. 假设验证结果")
        lines.append("（未提交假设 — 诊断在数据采集阶段被中断）")
        lines.append("")

    # ── 3. Best conclusions ──
    lines.append("## 3. 最佳结论建议")
    confirmed = [(hid, h) for hid, h in hypotheses.items()
                 if h.get("status") == "confirmed"]
    root_ids = set(ledger.get("root_hypothesis_ids", []))
    root_causes = ledger.get("root_causes", [])
    if confirmed:
        for i, (hid, node) in enumerate(
            sorted(confirmed, key=lambda x: -x[1].get("probability", 0))
        ):
            rc_text = root_causes[i] if i < len(root_causes) else node.get("statement", "")
            is_root = hid in root_ids
            lines.append(f"{i + 1}. {'[根因] ' if is_root else ''}{rc_text}")
    elif hypotheses:
        refuted_all = all(
            h.get("status") == "refuted" or h.get("deferred")
            for h in hypotheses.values()
        )
        if refuted_all:
            lines.append("所有假设均已被证伪。")
            lines.append("报告中应说明：在20轮内无法确定根因，建议延长诊断或扩大排查范围。")
        else:
            lines.append("无已确认假设。")
            lines.append("报告中应基于已收集数据给出初步观察，并明确标注'低置信度'。")
    else:
        lines.append("无假设已被验证。请基于原始数据给出判断。")
    lines.append("")

    # ── 4. Report template ──
    lines.append("## 4. 报告结构模板")
    lines.append("请按以下结构调用 write_file 生成报告：")
    lines.append("")
    lines.append("```markdown")
    lines.append("# 诊断报告")
    lines.append("## 诊断摘要")
    lines.append("- 故障描述、诊断范围、核心结论（1-2句话）")
    lines.append("## 关键发现")
    lines.append("- 按时间线/严重程度列出关键发现")
    lines.append("## 根因分析")
    lines.append("### 主要原因")
    lines.append("- 根因描述 + 置信度 + 完整证据链")
    if unfinished:
        lines.append("")
        lines.append("### 未验证假设（后续排查建议）")
        for hid, node in unfinished:
            lines.append(f"- {node.get('statement', hid)}")
    lines.append("## 建议措施")
    lines.append("### 紧急措施 / 长期优化")
    lines.append("## 附录")
    lines.append("- 诊断过程说明 + 轮次限制声明")
    lines.append("```")
    lines.append("")

    # ── 5. Evidence handling guidance ──
    lines.append("## 5. 不完整证据处理指引")
    lines.append("- 证据充分的假设 → 高置信度结论")
    lines.append("- 证据基本的假设 → 给出结论但标注\"存在一定不确定性\"")
    lines.append("- 证据不足的假设 → 标注\"初步推断\"或\"待进一步验证\"")
    lines.append("- 未完成的假设 → 放入\"后续排查建议\"，不要丢弃")
    if not confirmed and hypotheses:
        lines.append("- ⚠ 无已确认假设 → 聚焦于\"已排除的根因\"和\"排查建议\"")
        lines.append("- 不要编造根因，但要给出基于已收集数据的最佳判断")

    lines.append("</diagnostic_synthesis>")
    return "\n".join(lines)


def _write_report_file(report_path: str, content: str,
                       agent_data_real: str) -> None:
    """Write report .md to filesystem.  No-op if path or content is empty."""
    if not report_path or not content:
        return
    try:
        import pathlib as _pl
        real = report_path.replace("/agent_data/", str(agent_data_real) + "/")
        p = _pl.Path(real)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        logger.info("Report written to %s (%d chars)", p, len(content))
    except Exception as e:
        logger.warning("Failed to write report: %s", e)


def _emit_synthetic_write_file_event(report_path: str, report_md: str,
                                      round_num: int) -> None:
    """Emit tool_start / tool_end events so the frontend renders a
    '写入诊断记录' node even when write_file was never called by the LLM.
    """
    import uuid as _uuid
    try:
        from langgraph.config import get_stream_writer
        sw = get_stream_writer()
        tc_id = "call_synth_" + _uuid.uuid4().hex[:8]
        sw({
            "type": "tool_start",
            "name": "write_file",
            "args": {
                "file_path": report_path,
                "content_length": len(report_md),
            },
            "id": tc_id,
            "round": round_num,
            "description": "系统自动生成诊断报告",
        })
        sw({
            "type": "tool_end",
            "name": "write_file",
            "id": tc_id,
            "output": (
                f"诊断报告已生成（系统自动，{len(report_md)} 字符）"
            ),
            "round": round_num,
            "output_full": report_md[:500],
        })
        logger.info(
            "Emitted synthetic write_file events for frontend "
            "(round %d, %d chars)", round_num, len(report_md),
        )
    except RuntimeError:
        pass  # stream_writer unavailable outside ToolNode context


def _response_ends_loop(response) -> bool:
    """True when this response would let the agent loop terminate.

    The graph router continues the loop only while the latest AIMessage
    carries tool calls; a plain-text answer or an empty result both end
    it.  Guardrails use this single predicate (state-machine-v2.md §8)
    instead of re-implementing the check per phase.

    A response whose tool calls are ALL report_beliefs (SBR v2
    self-report, _BELIEF_TOOLS) also counts as loop-ending: the
    self-report is process metadata that produces no evidence and no
    ledger progress, so it must not mask a genuine stall from the
    guardrails that key on this predicate.
    """
    result_list = (response.result
                   if isinstance(response.result, list)
                   else [response.result])
    for m in result_list:
        for tc in (getattr(m, "tool_calls", None) or []):
            if isinstance(tc, dict):
                name = tc.get("name", "")
            else:
                name = getattr(tc, "name", "")
            if name and name not in _BELIEF_TOOLS:
                return False
    return True


def _inject_tool_calls(response, tool_calls: list[dict],
                        content_override: str = "") -> bool:
    """Inject synthetic tool calls into the first AIMessage of the response.

    Modifies *response.result* in-place.  The graph router checks
    ``AIMessage.tool_calls`` to decide whether to continue to the
    tools node or END.  This helper ensures the loop continues even
    when the LLM output pure text.

    Each injected call must carry ``type: "tool_call"``: the router
    dispatches each pending call as ``Send("tools", [tool_call])`` and
    ToolNode recognizes that shape by ``input[-1]["type"] == "tool_call"``
    (langgraph tool_node._parse_input).  Post-assignment
    (``msg.tool_calls = [...]``) bypasses AIMessage's init-time
    normalization which would add the field, so it must be added here —
    otherwise ToolNode falls into the message-list branch and raises
    ``ValueError: No AIMessage found in input`` (observed 2026-07-19,
    session f119ac84).

    Returns True on success.  When the response carries no usable
    AIMessage (e.g. the model returned an empty result as the loop was
    about to end), a fresh AIMessage is synthesized so the injection can
    never silently fail — guardrails must not become a failure source
    themselves (state-machine-v2.md §8 工程约束).
    """
    result_list = (response.result
                   if isinstance(response.result, list)
                   else [response.result])
    normalized = [
        tc if tc.get("type") == "tool_call" else {**tc, "type": "tool_call"}
        for tc in tool_calls
    ]
    for m in result_list:
        if hasattr(m, "tool_calls"):
            m.tool_calls = list(normalized)
            if content_override and hasattr(m, "content"):
                m.content = content_override
            return True
    msg = AIMessage(content=content_override or "")
    msg.tool_calls = list(normalized)
    response.result = [msg]
    return True


# ── G7' retry helpers ──

# Transport-level exception class names indicating a transient connection
# failure.  Matched by NAME (not isinstance) to stay robust across
# openai/httpx version bumps; HTTP status codes are checked separately.
_RETRYABLE_EXC_NAMES = frozenset({
    "APIConnectionError", "APITimeoutError",
    "ConnectError", "ReadError", "WriteError",
    "ConnectTimeout", "ReadTimeout", "PoolTimeout",
    "RemoteProtocolError", "ServiceUnavailableError",
})


def _is_retryable_model_error(exc: BaseException) -> bool:
    """True only for transient model-call failures worth retrying.

    Retryable: asyncio.TimeoutError, HTTP 408/429/5xx, connection and
    transport errors (connection refused/reset, read/write breaks).
    NOT retryable: CancelledError (user disconnect — must propagate),
    4xx client errors (bad params/auth/context-length — retrying cannot
    help), and unknown errors (surface bugs instead of masking them).
    """
    if isinstance(exc, asyncio.TimeoutError):
        return True
    if isinstance(exc, asyncio.CancelledError):
        return False
    status = getattr(exc, "status_code", None)
    if isinstance(status, int):
        return status in (408, 429) or status >= 500
    if isinstance(exc, (ConnectionError, OSError)):
        return True
    return type(exc).__name__ in _RETRYABLE_EXC_NAMES


def _retry_backoff_seconds(attempt: int) -> float:
    """Exponential backoff for retry *attempt* (1-based): base * 2^(n-1),
    capped, with ±50% jitter to avoid thundering-herd on a shared backend."""
    import random as _rnd
    base = _MODEL_RETRY_BACKOFF_BASE * (2 ** (attempt - 1))
    return min(base, _MODEL_RETRY_BACKOFF_MAX) * _rnd.uniform(0.5, 1.5)


def _extract_response_text(response) -> str:
    """Concatenate the string content of all messages in a ModelResponse."""
    result_list = (response.result
                   if isinstance(response.result, list)
                   else [response.result])
    parts: list[str] = []
    for m in result_list:
        content = getattr(m, "content", None)
        if isinstance(content, str) and content.strip():
            parts.append(content)
        elif isinstance(content, list):
            parts.extend(
                c.get("text", "") for c in content
                if isinstance(c, dict) and c.get("text")
            )
    return "\n".join(parts)


def _is_substantial_report_text(text: str) -> bool:
    """Heuristic: the text is an actual report, not bare chatter
    ("好的，我将生成报告").  Requires report structure markers — no
    length requirement (v2.5): a legitimate report can be short
    (scenario 29 produced a valid 615-char conclusion that the old
    800-char floor wrongly rejected).  Overwrite safety is already
    guaranteed by the caller's conditions (salvage only fires when no
    report exists or the on-disk one is an auto-generated stub; an
    LLM-authored report is never overwritten)."""
    if not text:
        return False
    _REPORT_MARKERS = ("# 故障", "## ", "**根因", "根因分析", "证据链", "修复建议")
    return any(marker in text for marker in _REPORT_MARKERS)


# ── SBR belief-ledger consistency guard (state-machine-v2.md §12) ──
# Every Coordinator round should call report_beliefs, self-reporting its
# belief state as flat tool args (phase/beliefs/root_cause_candidate/
# menu_choice).  The tool handler parses the args and compares the
# SELF-REPORTED beliefs against the ledger — the system's only window
# into the model's mind before it ACTS.  Divergence found here fires
# one round EARLIER than the action gates (write_file exit gate / phase
# gate): e.g. a self-reported root cause @95% with no confirmed
# hypothesis in the ledger means the model is about to either write_file
# (blocked, wasting a full report-generation call) or wander into
# ritual verification.  Corrections are returned IN THE TOOL RESULT —
# the channel the model demonstrably reads and acts on — informational,
# never commands; the hard gates remain the backstop.
#
# (v1 parsed a diagnosis_status text block from the response — the
# local model emitted it 0/14 rounds.  Tool args are its reliable
# structured channel, and tool-result feedback is its strongest
# behavioral signal.)
#
# Rule design constraints (P1): every rule is a deterministic predicate
# over (parsed args, ledger, same-round tool calls); conservative by
# construction — a rule only fires on UNAMBIGUOUS divergence, anything
# parseable as "already being resolved this round" suppresses it.

def _response_tool_calls(response) -> list[tuple[str, dict]]:
    """Extract (name, args) pairs for every tool call in a ModelResponse."""
    result_list = (response.result
                   if isinstance(response.result, list)
                   else [response.result])
    calls: list[tuple[str, dict]] = []
    for m in result_list:
        for tc in (getattr(m, "tool_calls", None) or []):
            if isinstance(tc, dict):
                calls.append((tc.get("name", ""), tc.get("args") or {}))
    return calls


def _sbr_norm_hid(raw: object) -> str | None:
    """Normalize a hypothesis id to the ledger's numeric-string key."""
    n = _parse_hypothesis_num(raw)
    return str(n) if n is not None else None


# Kill-intent toward a hypothesis in the menu_choice free text, both
# orders: "终结 H3" / "H3 可证伪".  Used by C3 only.
_SBR_KILL_INTENT_RES = (
    re.compile(r"(?:终结|证伪|埋葬|排除|record_finding)\s*[，,、]?\s*[Hh]?(\d+)"),
    re.compile(r"[Hh]?(\d+)\s*(?:终结|证伪|埋葬|排除|可证伪|已证伪)"),
)


def _check_belief_consistency(
    sbr: dict,
    ledger: DiagnosisLedger,
    tool_calls: list[tuple[str, dict]],
) -> list[tuple[str, str]]:
    """Deterministic belief-ledger divergence checks (C0-C3).

    Returns a list of ``(rule_id, message)`` pairs, at most one per
    rule, total capped at 3 (context budget).  Messages are phrased as
    calibration information, never imperatives.
    """
    corrections: list[tuple[str, str]] = []
    hypotheses = ledger.get("hypotheses", {})

    # Same-round tool calls that already resolve a divergence suppress
    # the corresponding rule (the model is formalizing RIGHT NOW).
    rf_verdicts: dict[str, str] = {}
    select_targets: list[str] = []
    commit_called = False
    for name, args in tool_calls:
        if name == "record_finding":
            hid = _sbr_norm_hid(args.get("hypothesis_id", ""))
            if hid:
                rf_verdicts[hid] = str(args.get("verdict", "")).lower()
        elif name == "commit_hypotheses":
            commit_called = True
        elif name == "select_path":
            sid = _sbr_norm_hid(args.get("selected_hypothesis_id", ""))
            if sid:
                select_targets.append(sid)

    # ── C0: self-reported phase contradicts the derived phase ──
    sp = sbr.get("phase")
    derived = derive_phase(ledger)
    if sp and sp != derived:
        corrections.append((
            "C0",
            f"⚠ [认知校准] 你自报阶段={sp}，但台账推导当前阶段为 "
            f"{derived}——以台账为准，请按 {derived} 阶段的「本步要求」行动。",
        ))

    # ── C1: root cause claimed (≥80%) but nothing confirmed formally ──
    rc = sbr.get("root_cause")
    if rc and rc[1] is not None and rc[1] >= 80:
        has_confirmed = any(
            h.get("status") == "confirmed" for h in hypotheses.values()
        )
        formalizing = commit_called or any(
            v == "confirmed" for v in rf_verdicts.values()
        )
        if not has_confirmed and not formalizing:
            corrections.append((
                "C1",
                f"⚠ [认知校准] 你自报认定根因（{rc[0][:40]}@{rc[1]}%），"
                "但台账尚无任何 confirmed 假设——自报不产生结论。合法路径："
                "① 现有证据已可判定的未决假设 → 直接 record_finding 终结"
                "（跨假设证据复用，无需重复委派）；"
                "② 全部判定后 commit_hypotheses 提交该根因假设；"
                "③ record_finding(verdict=confirmed, p≥80) 正式确认。"
                "出口判据满足后系统自动进入 REPORT，提前 write_file 会被门控拦截。",
            ))

    # ── C2: belief claims a terminal verdict the ledger doesn't have ──
    diverged: list[str] = []
    for hid, (st, _prob) in (sbr.get("beliefs") or {}).items():
        node = hypotheses.get(hid)
        if node is None:
            continue  # id not in ledger (stale/hallucinated) — no opinion
        ledger_status = node.get("status")
        if st in ("refuted", "confirmed") and ledger_status in (
                "pending", "inconclusive"):
            if rf_verdicts.get(hid) == st:
                continue  # record_finding this round is resolving it
            diverged.append(
                f"{fmt_hid(hid)}=已{'证伪' if st == 'refuted' else '确认'}"
                f"(台账仍为 {ledger_status})"
            )
    if diverged:
        corrections.append((
            "C2",
            "⚠ [认知校准] 你自报认为 "
            + "、".join(diverged[:3])
            + "——若证据足够，请用 record_finding 正式记录对应结论；"
            "自报不改变台账。",
        ))

    # ── C3: declared kill-intent on Hx but the action reactivates it ──
    menu = sbr.get("menu_choice") or ""
    if menu and select_targets:
        kill_ids: set[str] = set()
        for kre in _SBR_KILL_INTENT_RES:
            for km in kre.finditer(menu):
                hid = _sbr_norm_hid(km.group(1))
                if hid:
                    kill_ids.add(hid)
        for sid in select_targets:
            if sid in kill_ids:
                corrections.append((
                    "C3",
                    f"⚠ [认知校准] 你自报「{menu[:50]}」，但本轮动作是 "
                    f"select_path 选择 {fmt_hid(sid)} 继续验证——两者矛盾。"
                    f"若证据已可判定 {fmt_hid(sid)}，直接 record_finding "
                    "终结更省轮次；若确需验证，请在下轮 report_beliefs "
                    "如实更新判断。",
                ))
                break

    return corrections[:3]


def _build_subagent_context(ledger: dict, subagent_type: str = "",
                             max_chars: int = 24000) -> str:
    """Extract already-collected evidence from the ledger for subagent injection.

    When a subagent is delegated via task(), it normally sees only the
    Coordinator's description.  This function extracts the key findings
    already recorded in the ledger so the subagent can skip redundant
    tool calls and focus on its specific verification task.

    Injects three layers of context:
    1. Session baseline — original user message + time range (all subagents)
    2. Entity identifiers — hostname/cluster_name mapped to tool parameter names
    3. Diagnostic evidence — already-collected data the subagent should not re-query
    """
    lines: list[str] = []

    # ── Layer 1: Session baseline (all subagents) ──
    # NOTE: the raw user message is kept for symptom context only —
    # parameter values in it are superseded by Layers 2/3, so the
    # subagent must not copy entity names / time windows from here.
    user_message = ledger.get("user_message", "")
    if user_message:
        lines.append("## 诊断会话基线（仅作症状背景，参数以下方预解析值为准）\n")
        lines.append(f"用户原始输入: {user_message}\n")

    # ── Layer 2: Entity identifiers ──
    # Mapped to ACTUAL tool parameter names so the subagent LLM doesn't
    # need to infer the mapping from descriptive labels.
    entity_name = ledger.get("entity_name", "")
    entity_type = ledger.get("entity_type", "")
    hostname = ledger.get("hostname", "")
    _host_experts = ("host-argus-expert", "host-expert", "gpu-expert")
    _k8s_experts = ("k8s-argus-expert", "k8s-expert")

    entity_hints: list[str] = []
    if subagent_type in _host_experts and hostname:
        entity_hints.append(f"  - hostname: **{hostname}**")
    if subagent_type in _k8s_experts and entity_name and entity_type == "kubernetes":
        entity_hints.append(f"  - cluster_name: **{entity_name}**")
        entity_hints.append("  - 实体类型: kubernetes")

    if entity_hints:
        lines.append(
            "## 系统预解析的诊断实体"
            "（工具参数必须使用以下值；若 Coordinator 描述或用户输入中的"
            "实体名/时间窗口与此处不一致，一律以此处为准）:\n"
            + "\n".join(entity_hints) + "\n"
        )

    # ── Layer 3: Frontend param_overrides (JSON) ──
    param_overrides = ledger.get("param_overrides", {})
    start_time = ledger.get("start_time", "")
    end_time = ledger.get("end_time", "")
    if param_overrides or start_time or end_time:
        try:
            # Exclude fields already covered in Layer 2 to avoid
            # triplicated parameters (user message → Layer 2 → JSON).
            _redundant = {"hostname", "cluster_name", "fault_time_range"}
            clean = {k: v for k, v in param_overrides.items() if k not in _redundant}
            # Add time window from ledger if present
            if start_time:
                clean["start_time"] = start_time
            if end_time:
                clean["end_time"] = end_time
            if clean:
                import json as _json
                lines.append(
                    "## 前端参数覆盖（工具参数直接使用；"
                    "若与 Coordinator 描述中的时间窗口冲突，以此为准）:\n"
                    "```json\n"
                    + _json.dumps(clean, ensure_ascii=False, indent=2)
                    + "\n```\n"
                )
        except Exception:
            pass  # non-critical; skip on serialization failure

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
                summaries.append(s)
        if summaries:
            status_label = {"confirmed": "✓", "refuted": "✗", "inconclusive": "?"}.get(h["status"], "⟳")
            lines.append(f"\n假设 {fmt_hid(hid)} [{status_label}] {h.get('statement', '')}:")
            for s in summaries:
                lines.append(f"  - {s}")

    # ── Expert analysis results ──
    expert = ledger.get("expert_analysis")
    if expert:
        finding = expert.get("finding", "")
        if finding:
            lines.append(f"\n已委派专家: {expert.get('expert', '?')} ({expert.get('skill', '?')})")
            lines.append(f"  结论: {finding}")

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


def _sanitize_ledger(ledger: dict) -> dict:
    """Strip internal fields from ledger before sending to frontend/streaming."""
    internal_keys = {"_inconclusive_streak", "_backtrack_count",
                     "_root_commit_count", "_commit_count"}
    return {k: v for k, v in ledger.items() if k not in internal_keys}


# ── Agent memory injection (replaces deepagents MemoryMiddleware) ──
#
# AGENTS.md is loaded by this middleware and injected BEFORE the
# <diagnosis_ledger> block, so the current diagnosis state stays at the
# tail of the system prompt (highest attention).  This replaces
# deepagents' memory= parameter, whose MemoryMiddleware appends memory
# AFTER our ledger block and unconditionally includes ~5KB of generic
# <memory_guidelines> irrelevant to diagnosis.

def _load_agent_memory(agent_data_root: pathlib.Path) -> str:
    """Load AGENTS.md for injection."""
    parts: list[str] = []
    agents_md = agent_data_root / "AGENTS.md"
    try:
        if agents_md.exists():
            parts.append(
                "## AGENTS.md（诊断协作知识）\n"
                + agents_md.read_text(encoding="utf-8").strip()
            )
    except Exception as e:
        logger.warning("Failed to load AGENTS.md: %s", e)
    if not parts:
        return ""
    return "<agent_memory>\n" + "\n\n".join(parts) + "\n</agent_memory>"


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

    The description may have been wrapped by ``_build_subagent_context``
    (system context prepended before a ``[Coordinator 委派指令]`` marker)
    and the Coordinator itself may paste copied context fragments — both
    can contain hypothesis snapshots like "假设 H1 [✗]" that precede the
    actual delegation target.  A plain ``search()`` would return that
    snapshot ID and mis-route the expert's evidence to the wrong
    hypothesis.  We therefore search the segment after the LAST
    delegation marker first (the Coordinator's real instruction), falling
    back to a full-text match.
    """
    if not description or not hypotheses:
        return None

    def _canon(raw: str) -> str | None:
        # v2.7: ledger keys are canonical numeric strings ("2"); the
        # regex captures display form ("H2") — normalize before lookup.
        n = _parse_hypothesis_num(raw)
        if n is None:
            return None
        hid = str(n)
        return hid if hid in hypotheses else None

    marker = "[Coordinator 委派指令]"
    idx = description.rfind(marker)
    if idx >= 0:
        # Search only the Coordinator's real instruction.  On miss,
        # return None and let the caller fall back to active-path
        # routing — a full-text match here would more likely hit a
        # hypothesis snapshot in the wrapped context than the target.
        match = _HYPOTHESIS_ID_PATTERN.search(description[idx + len(marker):])
        if match:
            return _canon(match.group(1))
        return None
    match = _HYPOTHESIS_ID_PATTERN.search(description)
    if match:
        return _canon(match.group(1))
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
        validation_alias=AliasChoices("probability_update", "probability"),
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


class ReportBeliefsInput(BaseModel):
    # All fields are optional flat strings (SBR v2, §12): nested schemas
    # are the known tool-serialization failure mode on local models
    # (write_file({}) incident); a flat string arg is the simplest
    # possible shape and is parsed leniently by the handler.
    phase: str = Field(
        default="",
        description="自报当前阶段: understand/hypothesize/verify/evaluate/report",
    )
    beliefs: str = Field(
        default="",
        description="各假设的当前判断，紧凑格式 'H序号=状态@概率'，"
                    "状态∈pending/inconclusive/refuted/confirmed，"
                    "如 '1=refuted@5, 3=pending@20'",
    )
    root_cause_candidate: str = Field(
        default="",
        description="当前认定的根因与置信度，如 'DockerHub限速@95'；尚无则填 none",
    )
    menu_choice: str = Field(
        default="",
        description="本轮动作对应的相位菜单选项及一句话理由",
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
        is_subagent: bool = False,
        entity_type: str = "",
        hostname: str = "",
        entity_name: str = "",
        start_time: str = "",
        end_time: str = "",
        param_overrides: dict | None = None,
        valid_subagents: list[str] | None = None,
    ) -> None:
        self.ledger_path = ledger_path
        self.report_path = report_path
        self._model = model
        self._backend = backend
        self._is_subagent = is_subagent
        self._entity_type = entity_type
        self._hostname = hostname
        self._entity_name = entity_name
        self._start_time = start_time
        self._end_time = end_time
        self._param_overrides = param_overrides or {}
        # Valid subagent names for task() validation (Coordinator only).
        # None/empty disables the check (tests, subagent instances).
        self._valid_subagents = tuple(valid_subagents or ())
        # Agent memory (AGENTS.md), loaded once and injected before the
        # ledger block so the current diagnosis state stays at the
        # prompt tail.  Subagents skip injection.
        self._agent_memory_block = (
            "" if is_subagent else _load_agent_memory(self._AGENT_DATA_REAL)
        )
        self._current_ledger: DiagnosisLedger | None = None
        self._model_call_count: int = 0
        # Safety mechanism tracking
        self._consecutive_task_count: int = 0  # global: stagnation detection
        self._same_delegate_count: int = 0     # per-key: delegation saturation
        self._last_delegate_key: str = ""      # "expert:H1" etc.
        self._last_finding_round: int = 0
        self._last_evidence_round: int = 0  # round when diagnostic evidence last arrived
        self._last_task_round: int = 0
        self._report_phase_start_round: int = 0  # track when REPORT phase began
        self._verify_stall_count: int = 0  # consecutive verify-phase stalls
        self._verify_stuck_last_fired: int = -100  # cooldown for verify-stuck valve
        self._evaluate_stall_count: int = 0  # consecutive evaluate-phase stalls
        self._round_emitted: bool = False  # prevent duplicate round_transition on goto re-entry
        self._last_commit_round: int = -1  # round when commit_hypotheses last ran
        self._last_select_round: int = 0   # round when select_path/backtrack last ran
        self._max_rounds_grace_used: bool = False  # one-shot progress grace flag
        # ── Performance metrics ──
        self._inference_start: float = 0.0      # monotonic timestamp per round
        self._round_metrics: list[dict] = []    # [{round, phase, duration_s, input_tokens, output_tokens}, ...]
        self._tool_metrics: list[dict] = []     # [{round, tool_name, duration_s}, ...]
        # Monotonic sequence counter for preserving tool call invocation
        # order within the same LLM round (LangGraph executes tool calls
        # concurrently, so completion order ≠ invocation order).
        self._round_record_seq: int = 0
        # This round's (name, args) tool calls, stashed post-response so
        # the report_beliefs handler can suppress consistency rules that
        # a same-round sibling call is already resolving (SBR v2 §12).
        self._round_tool_calls: list[tuple[str, dict]] = []
        # Filesystem path prefix for offloaded tool results.
        self._tool_results_prefix = "/tool_results"
        # Ledger management tools — only needed by Coordinator; subagents
        # share the same ledger object but should not expose management tools.
        self.tools: list = (
            []
            if is_subagent
            else [
                self._make_commit_hypotheses_tool(),
                self._make_select_path_tool(),
                self._make_record_finding_tool(),
                self._make_backtrack_tool(),
                self._make_report_beliefs_tool(),
            ]
        )

    @classmethod
    def for_subagent(cls, coordinator: "DiagnosisLedgerMiddleware") -> "DiagnosisLedgerMiddleware":
        """Create a subagent instance that shares the ledger and backend.

        Tool dedup is handled separately by ToolDedupMiddleware (shared
        via its own ``for_subagent()``).  This method only shares the
        diagnosis ledger state so subagents can't create their own
        independent ledger.

        P1 expert-only blocking and ledger management tools are disabled
        — subagents own their diagnostic tools and should not manage
        the hypothesis tree directly.
        """
        instance = cls(
            ledger_path=coordinator.ledger_path,
            report_path=coordinator.report_path,
            backend=coordinator._backend,
            is_subagent=True,
        )
        instance._current_ledger = coordinator._current_ledger
        # Inherit the Coordinator's round count so any future code that
        # references _model_call_count on the subagent instance starts
        # from the correct baseline.  before_model() is a no-op for
        # subagents, so this value won't be mutated.
        instance._model_call_count = coordinator._model_call_count
        return instance

    # ── Fault profile extraction via structured output ──

    async def abefore_agent(self, state, runtime) -> dict | None:
        """Initialize the diagnosis ledger before the agent loop starts.

        Runs once before the first LLM invocation.  Creates an empty
        ledger that will be populated as the agent progresses through
        the diagnosis phases.

        Subagents share the Coordinator's ledger — no state update.
        """
        if self._is_subagent:
            return None
        if self._current_ledger is not None:
            logger.info(
                "台账: 共享已有实例 (agent=%s, object_id=%s)",
                getattr(runtime, "name", "?"), id(self),
            )
            return None

        ledger = new_ledger(entity_type=self._entity_type, hostname=self._hostname,
                            entity_name=self._entity_name)
        # ── Store user-facing session parameters in the ledger ──
        # These are injected into subagent context via _build_subagent_context
        # so subagents have the same baseline as the Coordinator.
        ledger["start_time"] = self._start_time
        ledger["end_time"] = self._end_time
        ledger["param_overrides"] = dict(self._param_overrides)
        # Extract the original user message from state (first HumanMessage)
        for msg in (state.get("messages") or []):
            if hasattr(msg, "type") and msg.type == "human":
                ledger["user_message"] = str(getattr(msg, "content", ""))[:2000]
                break
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

        Subagents must NOT emit round_transition — their LLM calls happen
        asynchronously inside a task() delegation, and their events can
        arrive after the Coordinator has already advanced to the next round.
        Emitting round_transition from subagents would pollute the global
        round counter and cause subagent tool calls to be mis-attributed
        to the wrong round in traces.
        """
        if self._is_subagent:
            return
        self._model_call_count += 1
        import time as _time
        self._inference_start = _time.monotonic()
        # Sync current_round in ledger with actual LLM call count so that
        # round numbers reflect LLM invocations, not individual tool calls.
        if self._current_ledger is not None:
            self._current_ledger["current_round"] = self._model_call_count
        # Emit round_transition ONLY on the first before_model of each
        # round.  Re-entry via Command(goto="model") (stall handling)
        # triggers before_model again but is NOT a new round — suppress
        # duplicate emission to avoid inflating the round counter.
        if not self._round_emitted:
            self._round_emitted = True
            try:
                sw = getattr(runtime, "stream_writer", None)
                if sw:
                    sw({
                        "type": "round_transition",
                        "round": self._model_call_count,
                    })
            except Exception as exc:
                logger.warning(
                    "before_model: stream_writer unavailable "
                    "(round %d): %s", self._model_call_count, exc,
                )

    # ── Context injection ──

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelResponse | ExtendedModelResponse:
        # Subagent: skip ledger context injection and safety warnings.
        # The Coordinator already passes relevant context via task() description.
        if self._is_subagent:
            return await handler(request)

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

        context_block = render_ledger_context(ledger, report_path=self.report_path,
                                               start_time=self._start_time,
                                               end_time=self._end_time)
        safety_block, safety_mutated = self._build_safety_warnings(ledger)
        if safety_mutated:
            # A valve mutated the ledger during prompt assembly (auto
            # verdict / forced REPORT / argus degrade) — re-render the
            # context so the phase guidance describes the post-mutation
            # state instead of contradicting the valve's own directive
            # (same pattern as the Phase-guard re-render below).
            context_block = render_ledger_context(ledger, report_path=self.report_path,
                                                   start_time=self._start_time,
                                                   end_time=self._end_time)

        # ── Phase guard: finalize pending hypotheses on REPORT entry ──
        # Only fires on LEGITIMATE report entries:
        #   - check_exit_conditions — root cause confirmed / hypotheses
        #     exhausted / evidence saturated (natural entries via
        #     derive_phase or record_finding);
        #   - ledger["_forced_terminal"] — system-terminal valves
        #     (max-rounds / stall / empty-data) forced REPORT;
        #   - ledger["report"] — report already generated by the system.
        # Premature finalization would deprioritize resumable hypotheses
        # that the LLM still needs to harden (refuted/dead_end) before a
        # fresh root-hypothesis batch may be committed.
        if (_phase(ledger) == "report"
                or derive_phase(ledger) == "report"):
            legit_report = bool(
                check_exit_conditions(ledger)[0]
                or ledger.get("_forced_terminal")
                or ledger.get("report")
            )
            if legit_report and finalize_pending_for_report(ledger):
                _force_report(ledger)
                self._persist_ledger(ledger)
                logger.info(
                    "Phase guard: finalized pending hypotheses for REPORT "
                    "(round %d)",
                    self._model_call_count,
                )
                # Re-render context so the LLM sees finalized hypothesis tree
                context_block = render_ledger_context(ledger, report_path=self.report_path,
                                                       start_time=self._start_time,
                                                       end_time=self._end_time)

        # ── Evidence audit: inject quality labels when entering REPORT ──
        # Gives the LLM a transparent, multi-dimensional view of evidence
        # quality for each confirmed hypothesis so it can make informed
        # decisions about report confidence.  The LLM is NOT forced to act
        # on this — it's purely informational.
        audit_block = ""
        if (_phase(ledger) == "report"
                or derive_phase(ledger) == "report"):
            audit_block = _build_evidence_audit(ledger)

        # Injection order: agent_memory (background knowledge) FIRST,
        # then the diagnosis ledger + safety warnings + evidence audit,
        # so the current diagnosis state stays at the prompt tail where
        # the model attends most.  (Previously deepagents MemoryMiddleware
        # appended memory after the ledger, giving historical cases
        # higher salience than the live diagnosis state.)
        combined_block = self._agent_memory_block
        if context_block:
            combined_block = f"{combined_block}\n\n{context_block}" if combined_block else context_block
        if safety_block:
            combined_block = f"{combined_block}\n\n{safety_block}" if combined_block else safety_block
        if audit_block:
            combined_block = f"{combined_block}\n\n{audit_block}" if combined_block else audit_block

        # ── Timeout protection: prevent single LLM call from hanging forever ──
        # REPORT phase generates a long markdown report — use a longer timeout.
        _call_request = request.override(system_message=append_to_system_message(
            request.system_message, combined_block)) if combined_block else request
        _effective_timeout = (
            _REPORT_PHASE_TIMEOUT
            if _phase(ledger) == "report"
            else _MODEL_CALL_TIMEOUT
        )
        # ── G7' transient-failure retry (state-machine-v2.md §8) ──
        # Each attempt gets its own full timeout.  Retryable failures
        # (timeout / connection break / 5xx / 429) back off exponentially
        # and retry within the SAME round — before_model has already run,
        # so round and safety counters are untouched.  Only when every
        # attempt fails does the G7 fallback (else-branch) fire.
        response = None
        _call_error: BaseException | None = None
        for _attempt in range(1, _MODEL_MAX_ATTEMPTS + 1):
            try:
                response = await asyncio.wait_for(
                    handler(_call_request),
                    timeout=_effective_timeout,
                )
                _call_error = None
                break
            except asyncio.CancelledError:
                raise  # user disconnect / server shutdown — never retry
            except BaseException as _exc:  # noqa: BLE001 — retryability classified by _is_retryable_model_error
                if not _is_retryable_model_error(_exc):
                    raise  # 4xx / programming errors — retrying cannot help
                _call_error = _exc
                _err_label = (
                    f"timeout after {_effective_timeout}s"
                    if isinstance(_exc, asyncio.TimeoutError)
                    else f"{type(_exc).__name__}: {_exc}"
                )
                if _attempt < _MODEL_MAX_ATTEMPTS:
                    _backoff = _retry_backoff_seconds(_attempt)
                    logger.warning(
                        "Model call failed (attempt %d/%d, round %d, "
                        "phase=%s): %s — retrying in %.1fs",
                        _attempt, _MODEL_MAX_ATTEMPTS,
                        self._model_call_count, _phase(ledger),
                        _err_label, _backoff,
                    )
                    await asyncio.sleep(_backoff)
                    continue
                logger.error(
                    "Model call failed after %d attempts (round %d, "
                    "phase=%s): %s. Auto-generating report from ledger.",
                    _MODEL_MAX_ATTEMPTS, self._model_call_count,
                    _phase(ledger), _err_label,
                )
        if _call_error is None:
            # ── Per-round inference metrics ──
            import time as _time2
            _duration = round(_time2.monotonic() - self._inference_start, 1)
            _tokens_in = 0
            _tokens_out = 0
            _tokens_reason = 0
            try:
                result_list = (
                    response.result
                    if isinstance(response.result, list)
                    else [response.result]
                )
                for _m in result_list:
                    _um = getattr(_m, "usage_metadata", None) or {}
                    _tokens_in += int(_um.get("input_tokens", 0))
                    _tokens_out += int(_um.get("output_tokens", 0))
                    _rm = getattr(_m, "response_metadata", None) or {}
                    _tok = _rm.get("token_usage", {}) or {}
                    _tokens_in += int(_tok.get("prompt_tokens", 0))
                    _tokens_out += int(_tok.get("completion_tokens", 0))
                    # reasoning tokens (OpenAI o1-style models)
                    _tokens_reason += int(
                        _um.get("output_token_details", {}).get(
                            "reasoning_tokens", 0,
                        )
                    )
            except Exception:
                pass  # non-critical; skip token extraction on failure
            self._round_metrics.append({
                "round": self._model_call_count,
                "phase": _phase(ledger),
                "duration_s": _duration,
                "input_tokens": _tokens_in,
                "output_tokens": _tokens_out,
                "reasoning_tokens": _tokens_reason,
            })
            # Sync metrics to ledger for persistence
            if self._current_ledger:
                self._current_ledger["metrics"] = {
                    "rounds": self._round_metrics,
                    "tools": self._tool_metrics,
                    "total_inference_s": round(
                        sum(r["duration_s"] for r in self._round_metrics), 1,
                    ),
                    "total_input_tokens": sum(
                        r["input_tokens"] for r in self._round_metrics
                    ),
                    "total_output_tokens": sum(
                        r["output_tokens"] for r in self._round_metrics
                    ),
                    "total_reasoning_tokens": sum(
                        r.get("reasoning_tokens", 0)
                        for r in self._round_metrics
                    ),
                }
            if _tokens_in or _tokens_out:
                logger.debug(
                    "Round %d metrics: %.1fs, in=%d out=%d reason=%d",
                    self._model_call_count, _duration,
                    _tokens_in, _tokens_out, _tokens_reason,
                )
            # ── SBR belief self-report tracking (§12, v2 tool-based) ──
            # Stash this round's tool calls for the report_beliefs tool
            # handler (same-round sibling suppression in C1/C2/C3), and
            # track whether the model self-reported at all.  Consistency
            # corrections themselves are computed in the tool handler and
            # returned in the tool result — the channel the model
            # demonstrably reads.  Here we only detect ABSENCE: 3 rounds
            # without a report_beliefs call earns one actionable reminder
            # via the safety block.  Everything is best-effort — a
            # failure here must never break the loop.
            try:
                self._round_tool_calls = _response_tool_calls(response)
                _reported = any(
                    n == "report_beliefs" for n, _ in self._round_tool_calls
                )
                if _reported:
                    ledger["_sbr_missing_streak"] = 0
                else:
                    _streak = ledger.get("_sbr_missing_streak", 0) + 1
                    ledger["_sbr_missing_streak"] = _streak
                    if _streak >= 3:
                        ledger["_sbr_corrections"] = [
                            "⚠ [认知校准] 已连续 "
                            f"{_streak} 轮未调用 report_beliefs 自报认知状态。"
                            "每轮请先调用 report_beliefs"
                            "（phase / beliefs / root_cause_candidate / "
                            "menu_choice）再进行其他动作——它用于校准你的"
                            "认知与台账，缺失会降低系统的引导精度。",
                        ]
                        ledger["_sbr_missing_streak"] = 0
                        logger.warning(
                            "SBR: beliefs not reported for %d consecutive "
                            "rounds (round=%d, phase=%s)",
                            _streak, self._model_call_count, _phase(ledger),
                        )
            except Exception as _sbr_exc:  # noqa: BLE001 — advisory only
                logger.debug("SBR tracking skipped on error: %s", _sbr_exc)
        else:
            # ── G7 fallback: every attempt failed ──
            # NOTE: this recovery path must never crash the stream —
            # a safety valve must not become a failure source itself.
            # Any exception here degrades to a plain-text response that
            # lets the graph END cleanly (observed 2026-07-19 session
            # 720c4c8a: an UnboundLocalError in this handler killed the
            # stream after the report had already been written).
            try:
                # ── Finalize pending hypotheses ──
                # Any hypothesis still in "pending" state was never
                # reached by the LLM.  Hard-close as refuted with
                # terminal_reason=timeout_unreached (former dead_end)
                # so the report reflects that diagnosis was interrupted
                # before verifying them.
                for hid, node in ledger.get("hypotheses", {}).items():
                    if node.get("status") == "pending":
                        node["status"] = "refuted"
                        node["terminal_reason"] = "timeout_unreached"
                        node["selected"] = False
                        if not node.get("evidence"):
                            node["evidence"] = []
                        from datetime import datetime as _dt, timezone as _tz
                        node["evidence"].append({
                            "source": "system",
                            "summary": "[系统自动] 诊断因模型超时中断，"
                                       f"此假设未及验证。",
                            "supports": False,
                            "tool_call_id": None,
                            "timestamp": _dt.now(_tz.utc).isoformat(),
                        })
                _force_report(ledger)
                self._report_phase_start_round = self._model_call_count

                # ── Auto-generate report from everything we have ──
                # Skip regeneration when the LLM already wrote its
                # report (timeout hit a later call, e.g. the
                # post-write_file summary) — never overwrite the
                # richer LLM-authored report with the thinner
                # auto-generated one.
                if ledger.get("report"):
                    report_md = ledger["report"]
                else:
                    report_md = self._generate_report_from_ledger(ledger)
                    ledger["report"] = report_md
                    _write_report_file(
                        self.report_path, report_md,
                        self._AGENT_DATA_REAL,
                    )
                self._persist_ledger(ledger)

                # ── Inject synthetic write_file call ──
                # A naked AIMessage without tool_calls causes the graph
                # router to END but the state cleanup may crash
                # (KeyError on orphaned tool_call_ids).  Injecting a
                # write_file call routes through the tools node first,
                # which settles the graph state cleanly before END.
                import uuid as _uuid
                synthetic = [{
                    "name": "write_file",
                    "id": f"call_timeout_{_uuid.uuid4().hex[:12]}",
                    "args": {
                        "file_path": self.report_path,
                        "content": report_md,
                    },
                }]
                response = ModelResponse(result=[AIMessage(
                    content="[系统超时] 模型调用超时。系统已基于已有证据自动生成诊断报告。",
                )])
                _inject_tool_calls(
                    response, synthetic,
                    content_override=(
                        "[系统超时] 模型调用超时。"
                        "系统已基于台账中的证据链自动生成诊断报告。"
                    ),
                )
            except Exception:
                logger.exception(
                    "Timeout recovery failed (round %d) — degrading to "
                    "plain-text response so the graph can END cleanly",
                    self._model_call_count,
                )
                response = ModelResponse(result=[AIMessage(
                    content="[系统超时] 模型调用超时，诊断已中断。"
                            "部分结果可能已写入报告文件。",
                )])

        # ── G8 首轮文本模拟（state-machine-v2.md §8）：round-1 text simulation ──
        # The LLM may output the entire diagnostic flow as a single
        # simulated text block — complete with fake delegation results,
        # fake hypothesis submissions, and references to past report
        # paths — without ever calling a single tool.
        # This check fires BEFORE verify/understand/report stall detection
        # so that simulated diagnoses don't silently exit the agent loop.
        if (self._model_call_count == 1
                and not self._is_subagent
                and self._current_ledger):
            if _response_ends_loop(response):
                # Extract text content from the first message
                text = ""
                result_list = response.result if isinstance(response.result, list) else [response.result]
                for m in result_list:
                    if hasattr(m, "content") and isinstance(m.content, str):
                        text = m.content
                        break
                # Simulation markers: patterns that appear when LLM
                # pretends to delegate, verify, or write reports in text,
                # plus malformed XML-ish tool-call text (observed: qwen
                # emitting "<task>...<parameter=...>" as plain content
                # when its tool-call serialization breaks).
                _SIMULATION_MARKERS = (
                    "🔧委派专家诊断",
                    "📊**host-argus-expert分析摘要**",
                    "📊**k8s-argus-expert分析摘要**",
                    "🔧提交诊断假设",
                    "🔧记录验证结论",
                    "🔧写入文件",
                    "报告已写入",
                    "<task>",
                    "</tool_call>",
                )
                is_simulation = any(m in text for m in _SIMULATION_MARKERS)
                # Long-ish text (>200 chars) with no tool calls is also
                # suspicious — a normal round-1 response should be short
                # or contain tool calls.  (Was 500; a 372-char malformed
                # XML tool call once slipped through on 2026-07-19.)
                is_long_text_stall = len(text) > 200
                if is_simulation or is_long_text_stall:
                    # Log-only: do NOT return early.  Recovery is handled
                    # by the round-1 zero-tools handler below via the
                    # _inject_tool_calls pattern — a single, uniform
                    # recovery path (no Command(goto) anywhere).
                    logger.warning(
                        "Safety: round-1 text simulation detected "
                        "(marks=%s, len=%d) — will be recovered by "
                        "zero-tools injection",
                        is_simulation, len(text),
                    )

        # ── Post-response: detect verify-phase stall ──
        # If the model returned no tool calls while in verify phase with
        # hypotheses that have expert evidence, force progression to REPORT.
        # Without this, the agent loop exits when the LLM outputs plain
        # text instead of calling record_finding or write_file.
        ledger = self._current_ledger
        if ledger and _phase(ledger) == "verify":
            loop_ends = _response_ends_loop(response)
            if not loop_ends:
                self._verify_stall_count = 0  # progress made, reset stall counter
            elif loop_ends and ledger.get("hypotheses"):
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
                    if node.get("status") in ("pending", "inconclusive"):
                        # Only auto-confirm when coordinator evidence with
                        # supports=True exists.  Expert evidence alone is
                        # insufficient — it always has supports=True
                        # regardless of actual verdict.
                        # NOTE: entering this safety net only requires the
                        # active hypothesis to still be undecided (the
                        # verify focus).  Whether it has expert evidence
                        # only decides *which* branch runs; it must NOT
                        # gate the whole safety net (otherwise an
                        # undecided hypothesis with NO recorded evidence —
                        # e.g. Coordinator skipped record_finding after an
                        # inconclusive expert result — silently ends the
                        # loop without writing a report).
                        if has_expert and has_coordinator_support:
                            record_finding(
                                ledger, active_id, "confirmed",
                                "[\u7cfb\u7edf\u81ea\u52a8] LLM \u5728 verify \u9636\u6bb5\u672a\u8c03\u7528\u5de5\u5177\uff0c"
                                "\u7cfb\u7edf\u57fa\u4e8e\u5df2\u6709\u534f\u8c03\u5458\u786e\u8ba4\u8bc1\u636e\u81ea\u52a8\u786e\u8ba4\u3002",
                                node["probability"],
                            )
                            # Auto-finalize ALL other undecided hypotheses
                            # so the LLM has no reason to continue after
                            # the report is generated.
                            for _hid, _node in ledger.get("hypotheses", {}).items():
                                if _hid != active_id and _node.get("status") in (
                                        "pending", "inconclusive"):
                                    _node["deferred"] = True
                                    _node["selected"] = False
                                    if not _node.get("deferred_reason"):
                                        _node["deferred_reason"] = (
                                            "[系统] REPORT 阶段自动搁置")
                            _force_report(ledger)
                            self._report_phase_start_round = self._model_call_count
                            self._persist_ledger(ledger)
                            logger.warning(
                                "Safety: verify-phase stall (no tool calls, "
                                "round %d) — auto-confirmed %s, forced REPORT",
                                self._model_call_count, active_id,
                            )
                            # Do NOT generate/write the report here —
                            # verify only records the verdict and hands
                            # over to the state machine (same pattern as
                            # branch C below and the G9 evaluate valve).
                            # The REPORT-phase post-response handler runs
                            # later in this same pass: it nudges the LLM
                            # to author the report via write_file
                            # (level-1), with the ledger stub (level-2) or
                            # the G7 fallback reserved for "LLM unable".

                        elif has_expert:
                            # Expert evidence exists but no coordinator
                            # assessment yet — LLM likely received expert
                            # results but outputs text instead of calling
                            # record_finding.  Three-level escalation:
                            #   1st stall → gentle reminder
                            #   2nd stall → strong reminder with inferred verdict
                            #   3rd stall → auto-decision
                            self._verify_stall_count += 1

                            # Extract expert output text for signal analysis
                            expert_text = ""
                            for ev in node.get("evidence", []):
                                src = ev.get("source", "")
                                if isinstance(src, str) and src.startswith("expert:"):
                                    expert_text = ev.get("summary", "")
                                    break

                            if self._verify_stall_count == 1:
                                # First stall — REMINDER only (G13 level-1,
                                # §8 three-level pattern).  Nudge with a
                                # harmless ls call + a record_finding
                                # directive; the VERDICT decision stays
                                # with the LLM.  (v2.8.4: the previous
                                # level-1 auto-injected verdict="confirmed"
                                # even with NO coordinator support —
                                # contradicting G4's same-situation rule
                                # (coordinator support → confirmed, else
                                # inconclusive) and fabricating a positive
                                # verdict from unassessed expert output.)
                                # Appending text to AIMessage is
                                # insufficient because the deepagents graph
                                # router checks tool_calls for loop
                                # decisions — a text-only AIMessage with no
                                # tool_calls terminates the loop
                                # immediately, and the appended reminder
                                # text never reaches the LLM.
                                logger.info(
                                    "Verify-phase gentle reminder: expert "
                                    "evidence exists for %s but no "
                                    "record_finding (round %d) — nudging",
                                    active_id, self._model_call_count,
                                )
                                import uuid as _uuid
                                synthetic = [{
                                    "name": "ls",
                                    "id": f"call_safety_{_uuid.uuid4().hex[:12]}",
                                    "args": {"path": "/agent_data"},
                                }]
                                response = ModelResponse(result=[AIMessage(
                                    content=(
                                        "⛔ [系统提醒] 你已收到专家验证结果但"
                                        "未调用 record_finding 记录结论。\n"
                                        f"请立即对假设 {fmt_hid(active_id)} "
                                        "调用 record_finding（confirmed / "
                                        "refuted / inconclusive 由你根据证据"
                                        "判定；证据不足就给 inconclusive）。"
                                        "禁止以纯文本结束本阶段。"
                                    ),
                                )])
                                _inject_tool_calls(response, synthetic)
                                # No count reset: escalation must be
                                # monotonic (reminder → degrade → terminal)
                                # across CONSECUTIVE stalls.  Any real tool
                                # call resets the counter at the top of the
                                # verify-stall handler.

                            elif self._verify_stall_count == 2:
                                # Second stall — DEGRADE (G13 level-2):
                                # record the verdict inferred from the
                                # expert's explicit verdict text (no
                                # fabrication: inferred=None → inconclusive,
                                # never a blind confirmed).  Also uses
                                # ExtendedModelResponse to force loop
                                # continuation (same reason as stall 1).
                                inferred = _infer_verdict_from_text(expert_text)
                                logger.info(
                                    "Verify-phase 2nd stall for %s — "
                                    "inferred verdict=%s, injecting strong "
                                    "reminder (round %d)",
                                    active_id, inferred or "none",
                                    self._model_call_count,
                                )
                                hint = ""
                                if inferred == "confirmed":
                                    hint = ("系统从专家输出中检测到 'confirmed/假设成立' "
                                            "信号，建议 verdict=confirmed。")
                                elif inferred == "refuted":
                                    hint = ("系统从专家输出中检测到 'refuted/不成立' "
                                            "信号，建议 verdict=refuted。")
                                else:
                                    hint = "系统无法从专家输出中推断明确结论。"
                                import uuid as _uuid
                                synthetic = [{
                                    "name": "record_finding",
                                    "id": f"call_safety_{_uuid.uuid4().hex[:12]}",
                                    "args": {
                                        "hypothesis_id": active_id,
                                        "verdict": inferred if inferred else "inconclusive",
                                        "evidence_summary": (
                                            "[系统自动] LLM 连续 2 轮未调用 record_finding，"
                                            f"系统基于专家文本自动{'确认' if inferred else '标记'}。"
                                        ),
                                        "probability_update": node["probability"],
                                    },
                                }]
                                response = ModelResponse(result=[AIMessage(
                                    content=(
                                        f"[系统强制] 已连续 2 轮未调用 record_finding。"
                                        f"系统已自动处理假设 {fmt_hid(active_id)}。"
                                    ),
                                )])
                                _inject_tool_calls(response, synthetic)
                                # No count reset — next consecutive stall
                                # escalates to the terminal auto-decision.

                            else:
                                # Third consecutive stall → TERMINAL
                                # auto-decision (G13 level-3)
                                inferred = _infer_verdict_from_text(expert_text)
                                if inferred == "confirmed":
                                    record_finding(
                                        ledger, active_id, "confirmed",
                                        "[系统自动] 专家输出含 confirmed 信号，"
                                        "LLM 连续 3 轮未调用 record_finding，"
                                        "系统基于专家文本自动确认。",
                                        node["probability"],
                                    )
                                    auto_verdict = "confirmed"
                                elif inferred == "refuted":
                                    record_finding(
                                        ledger, active_id, "refuted",
                                        "[系统自动] 专家输出含 refuted 信号，"
                                        "LLM 连续 3 轮未调用 record_finding，"
                                        "系统基于专家文本自动排除。",
                                        node["probability"],
                                    )
                                    auto_verdict = "refuted"
                                else:
                                    record_finding(
                                        ledger, active_id, "inconclusive",
                                        "[系统自动] LLM 连续 3 轮未调用 "
                                        "record_finding，专家输出无明确结论，"
                                        "系统标记为未完成。",
                                        node["probability"],
                                    )
                                    auto_verdict = "inconclusive"
                                # Auto-finalize ALL other undecided hypotheses
                                # so the LLM has no reason to continue after
                                # the report is generated.
                                for _hid, _node in ledger.get("hypotheses", {}).items():
                                    if _hid != active_id and _node.get("status") in (
                                            "pending", "inconclusive"):
                                        _node["deferred"] = True
                                        _node["selected"] = False
                                        if not _node.get("deferred_reason"):
                                            _node["deferred_reason"] = (
                                                "[系统] REPORT 阶段自动搁置")
                                _force_report(ledger)
                                self._report_phase_start_round = self._model_call_count
                                self._persist_ledger(ledger)
                                logger.warning(
                                    "Safety: verify-phase stall (no tool calls, "
                                    "round %d) — 3rd stall, auto-%s %s, "
                                    "forced REPORT",
                                    self._model_call_count, auto_verdict,
                                    active_id,
                                )
                                # Do NOT generate/write the report here —
                                # verify only records the verdict and hands
                                # over to the state machine (same pattern as
                                # branch C below and the G9 evaluate valve).
                                # The REPORT-phase post-response handler runs
                                # later in this same pass: it nudges the LLM
                                # to author the report via write_file
                                # (level-1), with the ledger stub (level-2)
                                # or the G7 fallback reserved for "LLM
                                # unable".

                        else:
                            # —— 分支 C（新增）：verifying 但无任何已记录证据 ——
                            # 例如专家返回 inconclusive 而 Coordinator 未调用
                            # record_finding，导致假设 evidence 为空。此时无法
                            # 验证，应自动标记 inconclusive 并转入 REPORT 阶段，
                            # 由报告阶段的安全兜底（post-response REPORT handler，
                            # 见 ~1781 行）负责生成并写入诊断报告——verify 阶段
                            # 本身不写报告，只负责把专家结论落账并交接给状态机。
                            logger.warning(
                                "Safety: verify-phase stall with NO recorded "
                                "evidence (round %d, %s) — auto-marking "
                                "inconclusive, transitioning to REPORT phase",
                                self._model_call_count, active_id,
                            )
                            record_finding(
                                ledger, active_id, "inconclusive",
                                "[\u7cfb\u7edf\u81ea\u52a8] LLM \u5728 verify \u9636\u6bb5\u672a\u8c03\u7528"
                                "\u5de5\u5177\u4e14\u5047\u8bbe\u65e0\u5df2\u8bb0\u5f55\u8bc1\u636e\uff0c\u7cfb\u7edf"
                                "\u81ea\u52a8\u6807\u8bb0\u672a\u5b8c\u6210\u5e76\u8f6c\u5165\u62a5\u544a\u9636\u6bb5\u3002",
                                node["probability"],
                            )
                            # record_finding(inconclusive) already marks the
                            # node inconclusive and clears its selected flag
                            # (5-state model) — it is no longer the verify focus.
                            node["selected"] = False
                            # Auto-finalize ALL other undecided hypotheses
                            # so the ledger is clean when the report is generated.
                            for _hid, _node in ledger.get("hypotheses", {}).items():
                                if _hid != active_id and _node.get("status") in (
                                        "pending", "inconclusive"):
                                    _node["deferred"] = True
                                    _node["selected"] = False
                                    if not _node.get("deferred_reason"):
                                        _node["deferred_reason"] = (
                                            "[系统] REPORT 阶段自动搁置")
                            # Transition to REPORT phase.  We deliberately do
                            # NOT generate/write the report here — that belongs
                            # to the report phase.  The reliable post-response
                            # REPORT-phase handler (~line 1781) will auto-generate
                            # and persist the report (and emit a synthetic
                            # write_file frontend event) once the loop ends with
                            # current_phase=="report" and no report written.
                            # This keeps verify focused on recording the expert
                            # conclusion, per the ledger state machine.
                            _force_report(ledger)
                            self._report_phase_start_round = self._model_call_count
                            self._persist_ledger(ledger)

        # ── Post-response: detect UNDERSTAND-phase stall ──
        # When the LLM outputs plain text without calling commit_hypotheses
        # in UNDERSTAND phase (round >= 2, data collected, no hypotheses),
        # inject a SystemMessage to force the agent loop to continue.
        # Without this, the LLM can output "🔧提交诊断假设" as text
        # (not a tool call) and the agent loop exits with no hypotheses.
        # Mirrors the verify-phase stall detection above.
        ledger = self._current_ledger
        if (ledger
                and not ledger.get("hypotheses")
                and self._model_call_count >= 2
                and _phase(ledger) == "understand"):
            if _response_ends_loop(response):
                data_collected = any(
                    "task" in r.get("tools_called", [])
                    for r in ledger.get("rounds", [])
                )
                if data_collected:
                    _argus_state = _classify_argus_data(ledger)
                    if (_argus_state == "param_missing"
                            and not ledger.get("_argus_redelegated")):
                        # ── Argus params missing → re-delegate ONCE ──
                        # The expert asked for params (hostname/time
                        # window) — the delegation was malformed, NOT an
                        # argus outage.  Re-delegate the required argus
                        # experts with full entity/time-window params.
                        # One-shot: if the retry also comes back
                        # param-missing, the next stall degrades (below).
                        ledger["_argus_redelegated"] = True
                        self._persist_ledger(ledger)
                        logger.warning(
                            "Safety: understand stall — argus experts "
                            "missing params (round %d), re-delegating "
                            "with full entity/time-window",
                            self._model_call_count,
                        )
                        _entity = (ledger.get("entity_name")
                                   or ledger.get("hostname") or "")
                        _st = self._start_time
                        _et = self._end_time
                        _time_hint = (
                            f"时间窗口: {_st} ～ {_et}"
                            if _st and _et else
                            f"时间窗口: {_st}"
                            if _st else
                            "时间窗口: 最近 6 小时"
                        )
                        _required = ["host-argus-expert"]
                        if ledger.get("entity_type") != "host":
                            _required.append("k8s-argus-expert")
                        import uuid as _uuid_re
                        synthetic = []
                        for expert in _required:
                            desc = (
                                f"查询并分析 {_entity} 集群 Argus K8s "
                                f"指标时序（NotReady/Pod重启/API延迟/"
                                f"etcd/DNS）。{_time_hint}。"
                                if expert == "k8s-argus-expert"
                                else f"查询并分析主机 {_entity} 的 Argus "
                                f"指标时序（CPU/内存/磁盘/网络 1min 粒度"
                                f"）。{_time_hint}。"
                            )
                            synthetic.append({
                                "name": "task",
                                "id": f"call_safety_{_uuid_re.uuid4().hex[:12]}",
                                "args": {
                                    "subagent_type": expert,
                                    "description": desc,
                                },
                            })
                        response = ModelResponse(result=[AIMessage(
                            content=(
                                "[系统提醒] Argus 专家反馈参数不全，已按"
                                "完整实体名与时间窗口自动重新委派。"
                            ),
                        )])
                        _inject_tool_calls(response, synthetic)
                    else:
                        if _argus_state == "failure" or (
                                _argus_state == "param_missing"
                                and ledger.get("_argus_redelegated")):
                            # ── Argus platform failure → degrade ──
                            # Argus returned empty data / errors (or the
                            # one-shot re-delegation also failed): the
                            # monitoring platform is considered
                            # unavailable.  Degrade to hypotheses from
                            # user input — T1 coverage gate is bypassed
                            # via _argus_unavailable, the normal
                            # VERIFY→EVALUATE→REPORT flow continues, and
                            # domain experts can still collect evidence.
                            ledger["_argus_unavailable"] = True
                            self._persist_ledger(ledger)
                            logger.warning(
                                "Safety: argus platform failure "
                                "(round %d, state=%s) — degrading to "
                                "user-input hypotheses (T1 bypass)",
                                self._model_call_count, _argus_state,
                            )
                        # "all_normal" needs no special handling: the
                        # findings are recorded in the ledger and the
                        # normal coverage/commit flow continues (如实
                        # 记录，继续推进).
                        # ── Coverage-aware stall handling ──
                        # If Argus coverage is incomplete (e.g. only
                        # k8s-argus-expert delegated, host-argus-expert
                        # missing), injecting commit_hypotheses is
                        # guaranteed to be blocked by the phase gate
                        # (understand phase tool whitelist, §7).
                        # Instead, force delegation of the missing expert
                        # so coverage becomes complete and the normal
                        # UNDERSTAND→HYPOTHESIZE transition (T1) can fire.
                        if not _coverage_ready(ledger):
                            # Identify which expert(s) are missing.
                            experts_delegated: set[str] = set()
                            for r in ledger.get("rounds", []):
                                experts_delegated.update(
                                    r.get("delegated_experts", []))
                            has_host = "host-argus-expert" in experts_delegated
                            has_k8s = "k8s-argus-expert" in experts_delegated
                            is_host_only = (
                                ledger.get("entity_type") == "host")

                            missing: list[str] = []
                            if not is_host_only and not has_k8s:
                                missing.append("k8s-argus-expert")
                            if not has_host:
                                missing.append("host-argus-expert")
                            # Host-only: only host-argus-expert matters.
                            if is_host_only:
                                missing = (
                                    ["host-argus-expert"]
                                    if not has_host else [])

                            if missing:
                                logger.warning(
                                    "Safety: understand stall with "
                                    "incomplete coverage (round %d, "
                                    "missing: %s) — forcing expert "
                                    "delegation instead of "
                                    "commit_hypotheses",
                                    self._model_call_count,
                                    ", ".join(missing),
                                )
                                _cluster = ledger.get("entity_name", "")
                                _st = self._start_time
                                _et = self._end_time
                                _time_hint = (
                                    f"时间窗口: {_st} ～ {_et}"
                                    if _st and _et else
                                    f"时间窗口: {_st}"
                                    if _st else
                                    "时间窗口: 最近 6 小时"
                                )
                                import uuid as _uuid2
                                synthetic = []
                                for expert in missing:
                                    desc = (
                                        f"查询并分析 {_cluster} 集群 "
                                        f"Argus 指标时序。{_time_hint}。"
                                        if expert == "k8s-argus-expert"
                                        else f"查询并分析主机 Argus "
                                        f"指标时序（CPU/内存/磁盘/网络"
                                        f" 1min 粒度）。{_time_hint}。"
                                    )
                                    synthetic.append({
                                        "name": "task",
                                        "id": f"call_safety_{_uuid2.uuid4().hex[:12]}",
                                        "args": {
                                            "subagent_type": expert,
                                            "description": desc,
                                        },
                                    })
                                response = ModelResponse(result=[AIMessage(
                                    content=(
                                        f"[系统强制] Argus数据采集不完整"
                                        f"（缺少{'、'.join(missing)}），"
                                        f"已自动委派缺失的专家完成故障"
                                        f"理解阶段。"
                                    ),
                                )])
                                _inject_tool_calls(response, synthetic)
                            else:
                                # Coverage gate reports not-ready but
                                # all expected experts already delegated
                                # (should not normally happen).  Nudge the
                                # loop alive with a harmless read-only call
                                # — do NOT inject a placeholder commit: it
                                # would be rejected by the UNDERSTAND phase
                                # gate, consume root-batch budget, and
                                # pollute the hypothesis tree (same
                                # principle as the G9 evaluate valve, §8).
                                logger.warning(
                                    "Safety: understand stall (round %d) — "
                                    "coverage anomaly (experts delegated, "
                                    "gate not ready), nudging continuation",
                                    self._model_call_count,
                                )
                                import uuid as _uuid3
                                synthetic = [{
                                    "name": "ls",
                                    "id": f"call_safety_{_uuid3.uuid4().hex[:12]}",
                                    "args": {"path": "/agent_data"},
                                }]
                                response = ModelResponse(result=[AIMessage(
                                    content=(
                                        "[系统提醒] 监控数据采集状态异常"
                                        "（专家已委派但覆盖门控未就绪）。"
                                        "请查看已有工具调用结果：若数据已返回，"
                                        "请基于数据继续诊断流程。"
                                    ),
                                )])
                                _inject_tool_calls(response, synthetic)
                        else:
                            # Coverage is ready (or the argus-failure
                            # degrade bypassed T1) — nudge commit via a
                            # harmless read-only call.  Do NOT inject a
                            # placeholder commit_hypotheses: a placeholder
                            # root batch consumes batch budget and pollutes
                            # the tree (same principle as the G9 evaluate
                            # valve, state-machine-v2.md §8).
                            logger.warning(
                                "Safety: understand stall (round %d) — "
                                "LLM output text without commit_hypotheses, "
                                "nudging commit via synthetic call",
                                self._model_call_count,
                            )
                            import uuid as _uuid4
                            synthetic = [{
                                "name": "ls",
                                "id": f"call_safety_{_uuid4.uuid4().hex[:12]}",
                                "args": {"path": "/agent_data"},
                            }]
                            response = ModelResponse(result=[AIMessage(
                                content=(
                                    "[系统强制] Argus 监控平台故障（空数据"
                                    "/报错），监控数据不可用。请基于用户描述"
                                    "的故障现象立即调用 commit_hypotheses "
                                    "提交假设，后续专家诊断将继续验证并修正。"
                                    if ledger.get("_argus_unavailable")
                                    else
                                    "[系统强制] 你已连续输出纯文本而未提交假设。"
                                    "请立即基于已有数据调用 commit_hypotheses "
                                    "提交假设（最多3个，按概率降序，各附依据）。"
                                ),
                            )])
                            _inject_tool_calls(response, synthetic)

        # ── Post-response: EVALUATE-phase loop abandonment (guardrail G9) ──
        # Trigger: the LLM ends its EVALUATE turn without tool calls
        # (plain-text "conclusion" or empty response), which would exit
        # the agent loop while derive_phase still demands a direction
        # (i.e. check_exit_conditions said NO — there is no confirmed
        # root cause, so exiting here would be the premature closure the
        # exit model explicitly forbids, state-machine-v2.md §6.3).
        #
        # Intervention follows the §8 three-level pattern:
        #   1st offence — 降级: walk the code-decided direction on the
        #     LLM's behalf, using only legal transitions from §5 and the
        #     priority order of the EVALUATE direction menu shown to the
        #     LLM (ledger.py _phase_guidance):
        #     (a) actionable pending root → synthetic select_path (T5);
        #     (b) only shelved (deferred) roots → synthetic select_path
        #         reviving one (T5 explicitly allows deferred revival);
        #     (b′) weak-confirmed (p<80) present → nudge the v2.7.1
        #         strengthening channel (record_finding, verdict stays
        #         with the LLM);
        #     (c) layer fully hard-failed/economically dead + batch
        #         budget open → harmless nudge reminding the LLM to
        #         commit_hypotheses (T8; no placeholder commit — that
        #         would consume batch budget and pollute the tree).
        #   2nd consecutive offence, or no legal direction at all — 兜底
        #     (G7 pattern): finalize undecided hypotheses via
        #     finalize_pending_for_report (deferred marking, NOT status
        #     mutation — §10) and force REPORT.  The REPORT-phase handler
        #     further down runs in this same post-response pass and takes
        #     care of report generation + persistence.
        ledger = self._current_ledger
        if (ledger
                and ledger.get("hypotheses")
                and self._model_call_count >= _MIN_ROUND_FOR_SAFETY
                and _phase(ledger) == "evaluate"):
            if _response_ends_loop(response):
                all_h = ledger.get("hypotheses", {})
                # Actionable nodes (§6.3 S2 actionability semantics),
                # scanned TREE-WIDE — not just the root layer: pending /
                # inconclusive and not shelved; low-probability pending
                # (p<=15) is exempt.  v2.9: the value filter (≥κ) aligns
                # this direction choice with the G5 delegation gate —
                # selecting an economically dead node was self-
                # contradictory: the very next delegation it implies is
                # hard-blocked by G5 (reproduced 2026-07-31: G9 picked a
                # value=0.125<κ node, G5 blocked the follow-up task()).
                # Dead nodes fall through to the revivable/batch/terminal
                # branches instead — a fresh batch (budget open) or a
                # REPORT boundary is the legal way out of a dead layer.
                actionable = [
                    hid for hid, node in all_h.items()
                    if node.get("status") in ("pending", "inconclusive")
                    and not node.get("deferred")
                    and (node.get("status") == "inconclusive"
                         or node.get("probability", 0) > 15)
                    and delegation_value(node) >= KAPPA_STOP
                ]
                # Revivable nodes: explicitly shelved (deferred) but still
                # undecided — select_path clears the shelved mark.
                revivable = [
                    hid for hid, node in all_h.items()
                    if node.get("status") in ("pending", "inconclusive")
                    and node.get("deferred")
                ]
                # Strengthenable nodes: weak confirmations below the S1
                # bar (p<80).  The v2.7.1 confirmed→confirmed channel is
                # a LEGAL direction here — without this branch, a weak
                # confirmation surrounded by economically dead siblings
                # (nothing actionable, batch gate closed by the confirmed
                # node) hit the terminal branch on the FIRST abandonment
                # and the strengthening channel was lost.
                strengthenable = [
                    hid for hid, node in all_h.items()
                    if node.get("status") == "confirmed"
                    and node.get("probability", 0) < 80
                ]
                batch_open = can_commit_root_hypotheses(ledger)

                if (self._evaluate_stall_count >= 1
                        or (not actionable and not revivable
                            and not strengthenable and not batch_open)):
                    # ── Terminal: repeated abandonment, or nothing legal
                    # left to do.  Hand over to REPORT (report generation
                    # happens in the REPORT-phase handler below).
                    finalize_pending_for_report(ledger)
                    _force_report(ledger)
                    self._report_phase_start_round = self._model_call_count
                    self._persist_ledger(ledger)
                    logger.warning(
                        "Safety: evaluate stall terminal (round %d, "
                        "stall_count=%d, actionable=%s, revivable=%s, "
                        "batch_open=%s) — finalized undecided hypotheses, "
                        "forced REPORT",
                        self._model_call_count,
                        self._evaluate_stall_count,
                        actionable, revivable, batch_open,
                    )
                    self._evaluate_stall_count = 0
                else:
                    self._evaluate_stall_count += 1
                    import uuid as _uuid
                    if actionable:
                        # (a) T5 — select the highest-value actionable
                        # root (§6.2 delegation-value ordering).
                        hid = max(actionable,
                                  key=lambda h: delegation_value(all_h[h]))
                        synthetic = [{
                            "name": "select_path",
                            "id": f"call_safety_{_uuid.uuid4().hex[:12]}",
                            "args": {
                                "selected_hypothesis_id": hid,
                                "rationale": (
                                    "[系统强制] EVALUATE 阶段未调用工具，"
                                    "系统按状态机方向选定验证焦点"
                                ),
                            },
                        }]
                        content = (
                            "[系统强制] 当前处于 EVALUATE 阶段但未调用工具。"
                            f"系统已自动选择假设 {fmt_hid(hid)} 进入验证（select_path）。"
                        )
                        logger.warning(
                            "Safety: evaluate stall (round %d) — injecting "
                            "select_path(%s), forcing continuation",
                            self._model_call_count, hid,
                        )
                    elif revivable:
                        # (b) T5 revival — re-activate the shelved root
                        # with the highest probability.  Revival outranks
                        # batch retry: the EVALUATE direction menu shown
                        # to the LLM (ledger.py _phase_guidance) and the
                        # T8 guard in derive_phase both require the whole
                        # layer to be refuted before a retry batch becomes
                        # the designated direction; a shelved-but-undecided
                        # node is a resumable direction, not an exhausted
                        # one.  If the revived node is later refuted, the
                        # layer is then fully refuted and T8 opens the
                        # batch naturally.
                        hid = max(
                            revivable,
                            key=lambda h: all_h[h].get("probability", 0))
                        synthetic = [{
                            "name": "select_path",
                            "id": f"call_safety_{_uuid.uuid4().hex[:12]}",
                            "args": {
                                "selected_hypothesis_id": hid,
                                "rationale": (
                                    "[系统强制] EVALUATE 阶段未调用工具，"
                                    "系统复活已搁置假设继续验证"
                                ),
                            },
                        }]
                        content = (
                            "[系统强制] 当前处于 EVALUATE 阶段但未调用工具。"
                            f"系统已复活被搁置的假设 {fmt_hid(hid)} 继续验证"
                            "（select_path）。"
                        )
                        logger.warning(
                            "Safety: evaluate stall (round %d) — reviving "
                            "deferred %s via select_path",
                            self._model_call_count, hid,
                        )
                    elif strengthenable:
                        # (b′) Strengthen direction — a weak-confirmed
                        # node (p<80) is the best remaining direction:
                        # the v2.7.1 confirmed→confirmed channel can lift
                        # it over the S1 bar.  Nudge only — the verdict
                        # and probability are the LLM's judgement call,
                        # never system-fabricated.
                        hid = max(
                            strengthenable,
                            key=lambda h: all_h[h].get("probability", 0))
                        synthetic = [{
                            "name": "ls",
                            "id": f"call_safety_{_uuid.uuid4().hex[:12]}",
                            "args": {"path": "/agent_data"},
                        }]
                        content = (
                            "[系统提醒] 当前处于 EVALUATE 阶段但未调用工具。\n"
                            f"已确认假设 {fmt_hid(hid)} 概率 "
                            f"{all_h[hid].get('probability')}% 低于根因确认门槛"
                            "（80%）。请基于已有证据调用 "
                            f"record_finding(hypothesis_id=\"{fmt_hid(hid)}\", "
                            "verdict=\"confirmed\", probability_update=...) "
                            "强化置信度/补充证据/修正表述；"
                            "若证据不足维持现状，请按指引选择其他方向。"
                        )
                        logger.warning(
                            "Safety: evaluate stall (round %d) — nudging "
                            "strengthening of weak-confirmed %s (p=%d)",
                            self._model_call_count, hid,
                            all_h[hid].get("probability", 0),
                        )
                    else:
                        # (c) T8 direction — nothing actionable remains on
                        # this layer (all roots hard-failed or economically
                        # dead, v2.9) and the batch budget is open.  Nudge
                        # via a harmless call; do NOT inject
                        # commit_hypotheses itself — a placeholder
                        # hypothesis would consume batch budget and pollute
                        # the tree.
                        synthetic = [{
                            "name": "ls",
                            "id": f"call_safety_{_uuid.uuid4().hex[:12]}",
                            "args": {"path": "/agent_data"},
                        }]
                        content = (
                            "[系统提醒] 当前处于 EVALUATE 阶段但未调用工具，"
                            "本层假设已全部证伪或验证增益已低于成本"
                            "（经济死亡，禁止再委派验证）。\n"
                            f"假设提交预算仍开放（已用 "
                            f"{ledger.get('_commit_count', 0)}"
                            f"/{MAX_COMMIT_CALLS} 次）。请立即调用 "
                            "commit_hypotheses 提交新一批假设"
                            "（必须基于已排除证据换方向，禁止重复或仅换措辞）。"
                        )
                        logger.warning(
                            "Safety: evaluate stall (round %d, commit %d/%d "
                            "open) — nudging commit_hypotheses via "
                            "synthetic call",
                            self._model_call_count,
                            ledger.get("_commit_count", 0),
                            MAX_COMMIT_CALLS,
                        )
                    response = ModelResponse(result=[AIMessage(
                        content=content,
                    )])
                    _inject_tool_calls(response, synthetic)
            else:
                # Real tool call made — the loop advances on its own.
                self._evaluate_stall_count = 0

        # ── Post-response: detect HYPOTHESIZE-phase stall (G12) ──
        # HYPOTHESIZE has two entry paths (state-machine-v2.md §4/§5): T1
        # (first batch — coverage just became ready, the ledger has NO
        # hypotheses yet) and T8 (retry batch — the current batch is all
        # hard-failed and budget remains).  In BOTH, if the LLM outputs
        # plain text instead of calling commit_hypotheses, the agent loop
        # would exit silently with no report (2026-07-31 scenario-24
        # swap_thrashing: 2 rounds, zero hypotheses, no report, no ledger
        # persisted).  Nudge via a harmless synthetic call — do NOT inject
        # commit_hypotheses itself: a placeholder hypothesis would consume
        # batch budget and pollute the tree (same principle as the G9
        # evaluate valve, §8).  No round gate: the phase cannot occur
        # before round 2 (s₀=UNDERSTAND, T1 coverage needs ≥2 rounds), and
        # plain text always means the loop is about to END — so a nudge is
        # always correct.  This mirrors the sibling verify-stall valve
        # (which also fires with no round gate).
        ledger = self._current_ledger
        if ledger and _phase(ledger) == "hypothesize":
            if _response_ends_loop(response):
                _has_prior = bool(ledger.get("hypotheses"))
                logger.warning(
                    "Safety: hypothesize-phase stall (round %d, commit "
                    "%d/%d, %s) — nudging commit_hypotheses via "
                    "synthetic call",
                    self._model_call_count,
                    ledger.get("_commit_count", 0),
                    MAX_COMMIT_CALLS,
                    "retry-batch" if _has_prior else "first-batch",
                )
                import uuid as _uuid
                synthetic = [{
                    "name": "ls",
                    "id": f"call_safety_{_uuid.uuid4().hex[:12]}",
                    "args": {"path": "/agent_data"},
                }]
                _directive = (
                    "请立即调用 commit_hypotheses 提交新一批假设——"
                    "必须基于已排除证据换方向推断，"
                    "禁止与已排除假设重复或仅换措辞。"
                    if _has_prior else
                    "你尚未提交任何假设。请立即调用 commit_hypotheses "
                    "提交首批竞争性假设（≤3 个，按概率降序，各附依据）——"
                    "禁止以纯文本结束本阶段。"
                )
                response = ModelResponse(result=[AIMessage(
                    content=(
                        "[系统提醒] 当前处于 HYPOTHESIZE 阶段"
                        f"（假设提交预算已用 "
                        f"{ledger.get('_commit_count', 0)}"
                        f"/{MAX_COMMIT_CALLS} 次）。\n"
                        f"{_directive}"
                    ),
                )])
                _inject_tool_calls(response, synthetic)

        # ── Post-response: detect VERIFY ledger-only stall ──
        # When the LLM uses ledger tools (select_path, commit_hypotheses)
        # but fails to follow up with a diagnostic delegation (task),
        # the verifying hypothesis remains unverified and the session
        # exits without expert evidence.  Detection uses state, not a
        # side-effect flag: check whether the active hypothesis has
        # expert evidence AND whether the LLM's latest response only
        # contained ledger-management tools.
        ledger = self._current_ledger
        if (ledger
                and _phase(ledger) == "verify"
                and ledger.get("hypotheses")
                and self._model_call_count >= _MIN_ROUND_FOR_SAFETY):
            active_path = ledger.get("active_path", [])
            active_id = active_path[-1] if active_path else None
            if active_id and active_id in ledger.get("hypotheses", {}):
                node = ledger["hypotheses"][active_id]
                # Only intervene while the active hypothesis is undecided
                # (the verify focus in the 5-state model).
                if node.get("status") in ("pending", "inconclusive"):
                    has_expert_evidence = any(
                        e.get("source", "").startswith("expert:")
                        for e in node.get("evidence", [])
                    )
                    # If no expert evidence yet, the LLM usually needs
                    # to delegate a diagnostic expert — BUT don't stall
                    # on hypotheses just created by commit_hypotheses
                    # (this or the previous round).  They naturally have
                    # no evidence yet and the LLM should have a chance
                    # to delegate before we intervene.
                    if (not has_expert_evidence
                            and self._model_call_count
                            - self._last_commit_round > 1):
                        ai_msgs = [
                            m for m in (response.result if isinstance(response.result, list) else [response.result])
                            if hasattr(m, "tool_calls")
                        ]
                        if not _response_ends_loop(response):
                            # Check whether ALL tool calls in this
                            # response are ledger-management tools.
                            # report_beliefs (SBR v2) counts as
                            # non-substantive here too: a self-report
                            # produces no expert evidence, so a
                            # beliefs+select_path round is still a
                            # ledger-idle stall.
                            all_ledger = True
                            for m in ai_msgs:
                                for tc in (m.tool_calls or []):
                                    name = getattr(tc, "name", "") if hasattr(tc, "name") else tc.get("name", "")
                                    if (name not in _LEDGER_TOOLS
                                            and name not in _BELIEF_TOOLS):
                                        all_ledger = False
                                        break
                                if not all_ledger:
                                    break
                            if all_ledger:
                                logger.warning(
                                    "Safety: verify ledger-only stall "
                                    "(round %d, active=%s, "
                                    "no expert evidence) — appending "
                                    "reminder inline",
                                    self._model_call_count, active_id,
                                )
                                # Append a reminder directly into the
                                # AIMessage content instead of using
                                # Command(goto="model").  goto re-entry
                                # has been observed to cause the model
                                # response to be silently dropped,
                                # leaving the session permanently hung.
                                result_list = (
                                    response.result
                                    if isinstance(response.result, list)
                                    else [response.result]
                                )
                                for m in result_list:
                                    if hasattr(m, "content") and isinstance(m.content, str):
                                        m.content = m.content.rstrip() + (
                                            "\n\n⛔ [系统提醒] 当前活跃假设 "
                                            f"{fmt_hid(active_id)} 尚无专家验证证据。"
                                            "请在本轮委派 host-expert "
                                            "（task, subagent_type=host-expert）"
                                            "验证此假设后，再调用 "
                                            "record_finding 记录结论。"
                                        )
                                        break

        # ── Post-response: detect REPORT-phase stall ──
        # Two-level escalation:
        #   1st stall → force continuation via SystemMessage
        #   2nd+ stall → ensure report exists AND emit synthetic
        #     write_file events so the frontend sees a visible node.
        ledger = self._current_ledger
        if ledger and _phase(ledger) == "report":
            if _response_ends_loop(response):
                # Check if write_file was already called (via tool call)
                # or report is already present (from record_finding auto-gen)
                report_exists = bool(ledger.get("report"))
                # ── G10 salvage: prefer LLM-authored report text ──
                # The LLM ended REPORT without write_file but produced a
                # substantial report-structured text (e.g. it rewrote the
                # auto-generated stub but emitted the rewrite as content).
                # Use that text as the report when none exists yet, or
                # when the on-disk one is an auto-generated stub — richer
                # LLM-authored content always wins over the stub.  Never
                # overwrite an LLM-authored report (the new text might be
                # the short user summary).  No extra model call, avoiding
                # the hang risk noted in the level-1 comment below.
                _salvage_text = strip_diagnosis_status_blocks(
                    _extract_response_text(response)
                )
                if _is_substantial_report_text(_salvage_text) and (
                        not report_exists
                        or ledger.get("_report_auto_generated")):
                    ledger["report"] = _salvage_text
                    ledger["_report_auto_generated"] = False
                    ledger["_forced_terminal"] = True
                    _write_report_file(
                        self.report_path, _salvage_text,
                        self._AGENT_DATA_REAL,
                    )
                    self._persist_ledger(ledger)
                    logger.warning(
                        "Safety: REPORT salvage (round %d) — LLM text "
                        "report (%d chars) replaces %s",
                        self._model_call_count, len(_salvage_text),
                        "auto-generated stub" if report_exists
                        else "missing report",
                    )
                    report_exists = True
                if not report_exists:
                    rounds_in_report = (
                        self._model_call_count
                        - max(self._report_phase_start_round, 1)
                    )
                    if rounds_in_report < 2:
                        # ── Level 1: nudge — the LLM writes the report ──
                        # Design principle (v2.5): the fault report is
                        # LLM-authored whenever the LLM can still respond;
                        # the ledger stub is reserved for "LLM unable to
                        # call".  A first REPORT stall means the LLM ended
                        # its turn without write_file — not that it cannot
                        # write.  Inject a harmless read-only ls call to
                        # keep the loop alive (Command(goto) unsupported)
                        # plus a directive demanding write_file next turn.
                        # If that model call hangs/fails, the G7'
                        # retry/fallback produces the stub — the same
                        # worst case as the old stub-first design, so the
                        # historical hang concern is fully absorbed.
                        logger.warning(
                            "Safety: REPORT stall level-1 (round %d) — "
                            "nudging LLM to call write_file (loop kept "
                            "alive, no stub yet)",
                            self._model_call_count,
                        )
                        import uuid as _uuid_r1
                        synthetic = [{
                            "name": "ls",
                            "id": f"call_safety_{_uuid_r1.uuid4().hex[:12]}",
                            "args": {"path": "/agent_data"},
                        }]
                        response = ModelResponse(result=[AIMessage(
                            content=(
                                "⛔ [系统提醒] REPORT 阶段禁止纯文本收尾。\n"
                                "你必须在下一轮调用 write_file 将完整诊断"
                                f"报告写入 {self.report_path}。\n"
                                "禁止调用任何诊断工具，禁止输出纯文本分析。"
                            ),
                        )])
                        _inject_tool_calls(response, synthetic)
                    else:
                        # ── Level 2: Auto-generate as last resort ──
                        report_md = self._generate_report_from_ledger(ledger)
                        ledger["report"] = report_md
                        ledger["_forced_terminal"] = True
                        _write_report_file(self.report_path, report_md, self._AGENT_DATA_REAL)
                        self._persist_ledger(ledger)
                        logger.warning(
                            "Safety: REPORT-phase stall level-2 "
                            "(no write_file after %d rounds in REPORT, "
                            "round %d) — auto-generated report (%d chars)",
                            rounds_in_report, self._model_call_count,
                            len(report_md),
                        )
                        report_exists = True

                # ── REPORT-phase termination ──
                # When the auto-generated report already exists on disk,
                # do NOT inject synthetic write_file tool_calls to keep
                # the graph loop alive.  The verify-phase safety valve
                # already injected write_file once, so the report has been
                # written to disk and the frontend has seen the write_file
                # node.  Further model calls only waste time — the LLM
                # has no remaining "verifying" hypotheses (all were
                # auto-finalized by the safety valve) and has nothing
                # useful to add.
                #
                # Just emit synthetic frontend events for observability
                # and let the graph loop END naturally.  The
                # _chat_event_stream finalization code will emit "done".
                if report_exists:
                    _emit_synthetic_write_file_event(
                        self.report_path,
                        ledger.get("report", ""),
                        self._model_call_count,
                    )
                    logger.info(
                        "REPORT phase complete: report exists (%d chars), "
                        "round %d — letting graph loop end naturally",
                        len(ledger.get("report", "")),
                        self._model_call_count,
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
                and _phase(ledger) == "understand"
                and not ledger.get("hypotheses")
            ):
                if _response_ends_loop(response):
                    # Extract context from ledger or environment for
                    # subagent delegation parameters used in the
                    # SystemMessage reminder below.
                    _ledger = self._current_ledger or {}
                    _cluster = _ledger.get("entity_name", "")
                    _hostname = _ledger.get("hostname", "")
                    _st = self._start_time
                    _et = self._end_time
                    _time_hint = (
                        f"时间窗口: {_st} ～ {_et}"
                        if _st and _et else
                        f"时间窗口: {_st}"
                        if _st else
                        "时间窗口: 最近 6 小时"
                    )
                    _k8s_desc = (
                        f"查询并分析 {_cluster} 集群 Argus K8s 指标时序"
                        f"（query_argus_nodes: NotReady/Pod重启/驱逐/Pending"
                        f"，query_argus_services: API延迟/etcd/DNS）。{_time_hint}。"
                        if _cluster else
                        "查询并分析 K8s 集群 Argus 指标时序"
                        "（节点状态、Pod 重启、API 延迟、DNS 延迟）"
                    )
                    _host_desc = (
                        f"查询并分析 {_hostname} 主机 Argus 指标时序"
                        f"（CPU/内存/磁盘/网络 1min 粒度）。{_time_hint}。"
                        if _hostname else
                        "查询并分析主机 Argus 监控指标时序"
                        "（CPU、内存、磁盘 IO、网络）"
                    )

                    logger.warning(
                        "Safety: round-1 understand-phase zero-tools "
                        "stall — injecting synthetic Argus delegation "
                        "tool_calls",
                    )
                    # ── _inject_tool_calls pattern (NOT Command(goto)) ──
                    # Command(goto="model") from wrap_model_call is rejected
                    # by langchain's _build_commands (NotImplementedError),
                    # which hung a session silently for 10+ minutes on
                    # 2026-07-19.  The proven pattern: build a NEW
                    # ModelResponse whose AIMessage carries synthetic
                    # task() calls — the graph router then continues to
                    # the tools node naturally.  Host-only scenarios have
                    # no k8s-argus-expert subagent — inject host only.
                    import uuid as _uuid
                    synthetic: list[dict] = []
                    if ledger.get("entity_type") != "host":
                        synthetic.append({
                            "name": "task",
                            "id": f"call_safety_{_uuid.uuid4().hex[:12]}",
                            "args": {
                                "subagent_type": "k8s-argus-expert",
                                "description": _k8s_desc,
                            },
                        })
                    synthetic.append({
                        "name": "task",
                        "id": f"call_safety_{_uuid.uuid4().hex[:12]}",
                        "args": {
                            "subagent_type": "host-argus-expert",
                            "description": _host_desc,
                        },
                    })
                    response = ModelResponse(result=[AIMessage(
                        content=(
                            "[系统安全阀] 第1轮回复未包含工具调用"
                            "（可能为模型输出格式异常），系统已自动"
                            "委派 Argus 专家采集监控数据。"
                        ),
                    )])
                    _inject_tool_calls(response, synthetic)
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
                    ledger["_report_auto_generated"] = True
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
        # Reset round_emitted here — this is the NORMAL return path (LLM
        # produced tool_calls or graph END).  The next before_model will
        # represent a genuinely new round and should emit round_transition.
        self._round_emitted = False
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

        # ── Malformed file-tool call observability ──
        # The streaming layer cannot warn on empty args (they may arrive
        # in later chunks).  Here args are final: a file tool missing
        # required params means the model emitted a malformed tool call
        # (observed 2026-07-30 session: write_file({}) in REPORT phase,
        # wasting a full round).  Warn only — the normal schema
        # validation error is what the LLM recovers from.
        if tool_name in _FILE_TOOL_REQUIRED_ARGS:
            _missing = [
                k for k in _FILE_TOOL_REQUIRED_ARGS[tool_name]
                if not (tool_args or {}).get(k)
            ]
            if _missing:
                logger.warning(
                    "%s called with missing/empty args %s (round=%d) — "
                    "model emitted malformed tool call; schema validation "
                    "error will be returned to the LLM",
                    tool_name, _missing, self._model_call_count,
                )

        # ── write_file gate: enforce "verify before report" ──
        # The state machine requires the quantified exit criteria
        # (S1 confidence ∧ S2 gain<cost, or a fast lane) before entering
        # REPORT phase.  This gate prevents the LLM from short-circuiting
        # the process by calling write_file directly from evaluate/verify.
        #
        # Pass conditions (any of):
        #   1. check_exit_conditions — root cause confirmed / hypotheses
        #      exhausted / evidence saturated (natural REPORT entries);
        #   2. ledger["_forced_terminal"] — a system-terminal valve
        #      (max-rounds / stall / empty-data) forced REPORT as
        #      graceful degradation;
        #   3. ledger["report"] — a report was already generated by the
        #      system, so a rewrite is idempotent and harmless.
        if tool_name == "write_file":
            ledger = self._current_ledger
            # Gate ALWAYS applies — including the zero-hypothesis state:
            # check_exit_conditions returns False there, so an UNDERSTAND-
            # phase write_file can no longer short-circuit the whole
            # state machine into the REPORT terminal lock (§7 T12).
            # Guardrail paths stay open: _forced_terminal is set before
            # any synthetic write_file injection (G7), and system-side
            # stub writers never go through this tool.
            if ledger:
                should_exit, reason, _ = check_exit_conditions(ledger)
                if not should_exit and not (
                    ledger.get("_forced_terminal") or ledger.get("report")
                ):
                    if not reason:
                        reason = "诊断尚未建立任何假设"
                    logger.warning(
                        "write_file blocked by exit-condition gate: %s "
                        "(phase=%s, round=%d, path=%s)",
                        reason,
                        _phase(ledger),
                        self._model_call_count,
                        tool_args.get("file_path")
                        or tool_args.get("path") or "",
                    )
                    return ToolMessage(
                        content=(
                            f"⛔ 暂时无法生成诊断报告: {reason}\n"
                            "（REPORT 仅在根因确认、假设穷尽或证据饱和时开放）"
                        ),
                        tool_call_id=tool_call_id,
                    )
                # ── Canonicalize the report path (v2.5) ──
                # The report path is system-generated and handed to the
                # LLM, which merely carries it back — LLM path
                # reproduction is unreliable (scenario 29: hash-suffix
                # typo `bda8640e`→`bda860e` silently discarded a complete
                # LLM-authored report, system shipped the stub instead).
                # Any write_file passing the gate above IS the report
                # write, so coerce its target unconditionally; the LLM
                # only supplies content.  Downstream capture
                # (_paths_match backfill, report_content SSE event, stub
                # suppression) then works unchanged.
                if self.report_path:
                    _provided = (tool_args.get("file_path")
                                 or tool_args.get("path") or "")
                    if _provided and not _paths_match(
                            _provided, self.report_path):
                        logger.warning(
                            "write_file path coerced to canonical report "
                            "path: %s -> %s (round=%d)",
                            _provided, self.report_path,
                            self._model_call_count,
                        )
                    for _k in ("file_path", "path"):
                        if _k in tool_args:
                            tool_args[_k] = self.report_path

        # ── REPORT-phase gate: prevent restarting diagnosis after
        # a root cause has been confirmed.  Creating new hypotheses
        # or backtracking after confirmation would create confusing
        # frontend trees (write_file → commit_hypotheses → ...).
        #
        # Exception: if ALL root hypotheses are exhausted (refuted /
        # dead-end) and NO root cause has been confirmed, allow
        # commit_hypotheses / backtrack — the first attempt failed
        # and the LLM should be able to propose a new round.
        if tool_name in ("commit_hypotheses", "backtrack"):
            ledger = self._current_ledger
            if ledger and _phase(ledger) == "report":
                cids = ledger.get("root_cause_hypothesis_ids", [])
                if cids:
                    # At least one confirmed root cause — no restart
                    logger.warning(
                        "%s blocked in REPORT phase (root cause "
                        "confirmed, round=%d)",
                        tool_name, self._model_call_count,
                    )
                    return ToolMessage(
                        content=(
                            f"⛔ 当前处于 REPORT 阶段，根因已确认。"
                            f"禁止调用 {tool_name}。"
                            "请调用 write_file 生成诊断报告。"
                        ),
                        tool_call_id=tool_call_id,
                    )

        # ── Phase tool gating (state-machine-v2.md §7, Q4 硬门控) ──
        # Reject out-of-phase ledger/diagnostic actions with a corrective
        # message.  read_file/write_todos/ls/grep/glob are always allowed
        # (read-only scaffolding).  Only Coordinator is gated — subagents
        # own their expert toolsets.
        if not self._is_subagent:
            ledger = self._current_ledger
            if ledger is not None:
                _phase_now = _phase(ledger)
                _gate = _PHASE_TOOL_GATE.get(_phase_now)
                if _gate is not None and tool_name in _gate:
                    logger.warning(
                        "%s blocked by phase gate in %s (round=%d)",
                        tool_name, _phase_now, self._model_call_count,
                    )
                    return ToolMessage(
                        content=(
                            f"⛔ {tool_name} 在 {_phase_now.upper()} 阶段不可用"
                            f"（当前阶段允许: {_gate_hint(_phase_now)}）。"
                        ),
                        tool_call_id=tool_call_id,
                    )

        # ── Capture invocation order BEFORE any async work ──
        # LangGraph executes multiple tool calls from a single LLM
        # response concurrently.  Completion order (and thus
        # record_round insertion order) may not match the LLM's
        # intended ordering.  This sequence counter preserves the
        # original invocation order.
        self._round_record_seq += 1
        invocation_seq = self._round_record_seq

        # ── P1: Block coordinator from calling expert-only tools ──
        # query_argus_* tools are exclusively available to Argus expert
        # subagents (host-argus-expert / k8s-argus-expert).  If the
        # coordinator tries to call them directly, return a redirect.
        # Subagents skip this check — they own these tools.
        #
        # Must run BEFORE cross-agent dedup: otherwise the Coordinator
        # could bypass this check by hitting a subagent's cached result
        # in shared_backend.
        if not self._is_subagent:
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
                        "task(subagent_type='host-argus-expert', description='查询主机Argus指标时序')\n"
                        "- K8s集群指标(节点/Pod/API/DNS) → "
                        "task(subagent_type='k8s-argus-expert', description='查询K8s Argus指标时序')"
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
                subagent_type = tool_args.get("subagent_type", "")
                desc = tool_args.get("description", "")

                # ── Unknown subagent fast-fail (Coordinator only) ──
                # A missing/unresolvable subagent_type otherwise falls
                # through to the task tool and wastes a round on an
                # opaque failure (observed 2026-07-23 session 7805961b:
                # round-1 two delegations to subagent '?').  Reject
                # upfront with the valid expert list so the LLM can
                # correct the call in the same round.
                if (not self._is_subagent and self._valid_subagents
                        and subagent_type not in self._valid_subagents):
                    logger.warning(
                        "task() blocked: unknown subagent_type=%r "
                        "(valid: %s, round=%d)",
                        subagent_type or "<missing>",
                        ", ".join(self._valid_subagents),
                        self._model_call_count,
                    )
                    _experts = "\n".join(
                        f"- {name}" for name in self._valid_subagents
                    )
                    return ToolMessage(
                        content=(
                            f"⛔ 未知的专家类型 subagent_type={subagent_type!r}。\n"
                            "可用专家（必须从中选择一个作为 subagent_type）：\n"
                            f"{_experts}\n"
                            "请修正 subagent_type 后重新调用 task()。"
                        ),
                        tool_call_id=tool_call_id,
                    )

                # ── S2 gain<cost delegation gate (state-machine-v2.md §6) ──
                # A delegation whose expected information gain per unit cost
                # falls below κ is hard-blocked with a quantified reason.
                # Only applies to VERIFY-phase delegations targeting a
                # known hypothesis — UNDERSTAND-phase Argus data collection
                # (no hypothesis yet) is always allowed.
                _gate_hid = _parse_hypothesis_id_from_description(
                    desc, ledger.get("hypotheses", {}),
                )
                if _gate_hid and ledger.get("hypotheses"):
                    _gate_node = ledger["hypotheses"].get(_gate_hid)
                    if (_gate_node is not None
                            and _gate_node.get("status") in ("pending", "inconclusive")):
                        from diagnostics.agent.ledger import (
                            delegation_value, KAPPA_STOP,
                        )
                        _v = delegation_value(_gate_node)
                        if _v < KAPPA_STOP:
                            logger.warning(
                                "Delegation blocked by gain<cost gate: "
                                "%s value=%.3f < κ=%.2f (delegated=%d, "
                                "inconclusive=%d, round=%d)",
                                _gate_hid, _v, KAPPA_STOP,
                                _gate_node.get("_delegate_count", 0),
                                _gate_node.get("_inconclusive_count", 0),
                                self._model_call_count,
                            )
                            return ToolMessage(
                                content=(
                                    f"⛔ 委派被信息增益门控拦截：假设 {fmt_hid(_gate_hid)} "
                                    f"当前验证动作价值 {_v:.3f} < κ={KAPPA_STOP}"
                                    f"（已委派 {_gate_node.get('_delegate_count', 0)} 次、"
                                    f"inconclusive {_gate_node.get('_inconclusive_count', 0)} 次，"
                                    "重复验证的期望信息增益低于成本）。\n"
                                    "请选择：\n"
                                    "- record_finding 对该假设给出结论（confirmed/refuted/inconclusive）\n"
                                    "- select_path 切换到其他未决假设\n"
                                    "- 若无 confirmed 根因且换批预算未用尽：commit_hypotheses 换方向\n"
                                    "- 若退出判据已满足：系统将自动进入 REPORT"
                                ),
                                tool_call_id=tool_call_id,
                            )

                # ── Auto-heal: delegation implies verification focus ──
                # The Coordinator may skip select_path and delegate
                # verification of a pending/deferred/inconclusive
                # hypothesis directly.  Without this heal, active_path
                # stays pinned to a stale (possibly refuted) hypothesis,
                # auto-collected evidence falls back to the wrong node,
                # and the VERIFY-phase guidance never targets the real
                # hypothesis.  Parse the ORIGINAL description (before the
                # context wrap below) and activate the target.
                target_hid = _gate_hid
                if target_hid:
                    target_node = ledger["hypotheses"].get(target_hid, {})
                    if target_node.get("status") in (
                        "pending", "inconclusive",
                    ):
                        activate_hypothesis(ledger, target_hid)
                        self._persist_ledger(ledger)
                        logger.info(
                            "Auto-activated %s via task() delegation "
                            "(select_path skipped, round %d)",
                            target_hid, self._model_call_count,
                        )

                ctx = _build_subagent_context(ledger, subagent_type=subagent_type)
                # Idempotent wrap: if the Coordinator already pasted the
                # session baseline into its description (learned from
                # wrapped delegations visible in message history), skip
                # re-wrapping to avoid duplicated context blocks.
                if ctx and "## 诊断会话基线" not in desc:
                    # System-resolved session context (user message, time
                    # range, entity identifiers) must take priority over
                    # the Coordinator's description.  Placing the system
                    # context FIRST gives it higher attention weight and
                    # ensures authoritative parameter values are used.
                    tool_args["description"] = (
                        f"{ctx}\n\n---\n"
                        f"[Coordinator 委派指令]\n{desc}"
                    )
                    try:
                        request.tool_call["args"] = tool_args
                    except (TypeError, AttributeError, KeyError):
                        pass  # request is immutable; proceed without injection

        # Emit precise tool timing events via stream_writer
        sw = getattr(request.runtime, "stream_writer", None)
        import time as _ttime
        _tool_start = _ttime.monotonic()
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

        # ── Offload to shared backend for cross-round access ──
        # Writes the full tool result to the filesystem so the LLM
        # can use read_file / grep to retrieve data from previous
        # rounds instead of re-calling the same tool.
        # Depends on ToolDedupMiddleware for same-round/cross-agent dedup.
        _OFFLOAD_SKIP = _SCAFFOLDING_TOOLS | _LEDGER_TOOLS | {"task"}
        if self._backend and tool_name not in _OFFLOAD_SKIP and isinstance(result, ToolMessage):
            content = result.content if isinstance(result.content, str) else str(result.content)
            if content:
                cache_key = _make_cache_key(tool_name, tool_args)
                safe_name = _sanitize_path_component(tool_name)
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
                    file_path = ""
                    logger.debug("Offload write skipped for %s (backend error)", tool_name)
                # Store metadata in ledger for context injection
                lines = content.splitlines()
                if self._current_ledger is not None:
                    self._current_ledger.setdefault("tool_results", {})[cache_key] = {
                        "path": file_path,
                        "round": self._model_call_count,
                        "preview": content[:200].replace("\n", " "),
                        "lines": len(lines),
                    }

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
                    # Diagnostic data arrived — counts as verify-phase
                    # progress for the verify-stuck safety valve.
                    self._last_evidence_round = self._model_call_count
                    # ── Hypothesis-aware evidence routing ──
                    # Auto-collected tool outputs are recorded with
                    # supports=None (undetermined): the raw output does
                    # not by itself prove or refute the hypothesis.
                    # Only record_finding (coordinator assessment) writes
                    # explicit True/False verdicts.
                    if tool_name == "task":
                        target_hid = _parse_hypothesis_id_from_description(
                            tool_args.get("description", ""),
                            ledger.get("hypotheses", {}),
                        )
                        if target_hid and target_hid in ledger.get("hypotheses", {}):
                            node = ledger["hypotheses"][target_hid]
                            node["evidence"].append(new_evidence(
                                source, summary, supports=None, tool_call_id=tool_call_id,
                            ))
                            if tool_name not in node["verification_tools"]:
                                node["verification_tools"].append(tool_name)
                        else:
                            add_evidence_to_active(
                                ledger, source, summary,
                                supports=None,
                                tool_call_id=tool_call_id,
                                tool_name=tool_name,
                            )
                    else:
                        add_evidence_to_active(
                            ledger, source, summary,
                            supports=None,
                            tool_call_id=tool_call_id,
                            tool_name=tool_name,
                        )

            # Build key_findings from tool output.
            # Normal outputs are truncated (kept as a concise "key finding"),
            # but failing tool calls (LangChain ToolNode errors) keep their
            # full message so the root cause is traceable in post-hoc review.
            key_findings = ""
            if output:
                key_findings = output.replace("\n", " ").strip()
                is_error = key_findings.startswith("Error invoking tool") or \
                    "Error:" in key_findings
                if is_error:
                    if len(key_findings) > 2000:
                        key_findings = key_findings[:2000] + " …(truncated)"
                elif len(key_findings) > 200:
                    key_findings = key_findings[:200] + " …(truncated)"

            # Track delegated experts for coverage detection
            delegated = None
            if tool_name == "task":
                expert = (tool_args.get("subagent_type")
                          or tool_args.get("subagent_name")
                          or tool_args.get("name"))
                if expert:
                    delegated = [expert]

            record_round(
                ledger, derive_phase(ledger),
                tools_called=[tool_name],
                action_summary=tool_args.get("description", "") if tool_name == "task" else tool_name,
                key_findings=key_findings,
                _seq=invocation_seq,
                delegated_experts=delegated,
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
            # select_path / backtrack also count as diagnostic progress
            # (the LLM is actively navigating the hypothesis tree).
            elif tool_name in ("select_path", "backtrack"):
                self._last_select_round = self._model_call_count
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

            # Persist per-hypothesis delegation count on the node —
            # drives the freshness decay F in the gain<cost model
            # (delegation_value in ledger.py).  Count only successful
            # returns; tool failures never reach this post-handler.
            if hid != "?":
                _node = ledger.get("hypotheses", {}).get(hid)
                if _node is not None:
                    _node["_delegate_count"] = (
                        _node.get("_delegate_count", 0) + 1)
                    self._persist_ledger(ledger)

        # Capture report content when write_file writes to the pre-generated
        # report path — emit for frontend answer-body without text parsing.
        if tool_name == "write_file" and self.report_path:
            file_path = tool_args.get("file_path") or tool_args.get("path") or ""
            if file_path and _paths_match(file_path, self.report_path):
                content = tool_args.get("content") or tool_args.get("text") or ""
                if content:
                    # Backfill report content into the ledger so that
                    # result.json carries the full report text alongside
                    # the structured hypothesis data.  Clear the
                    # auto-generated stub mark (G10) only when the content
                    # is NOT the stub itself: guardrail-injected synthetic
                    # write_file calls (G7 timeout / verify-stall fallback)
                    # carry the auto-generated stub verbatim — clearing the
                    # mark on such an echo would disguise the stub as
                    # LLM-authored, permanently disabling the G10
                    # overwrite invitation and the REPORT-stall salvage.
                    if ledger:
                        _is_stub_echo = (
                            ledger.get("_report_auto_generated")
                            and content == ledger.get("report")
                        )
                        ledger["report"] = content
                        if not _is_stub_echo:
                            ledger["_report_auto_generated"] = False

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
                        if status in ("pending", "inconclusive") and node.get("selected"):
                            # The active (selected) hypothesis at report time.
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
                            if has_coordinator_support and status == "pending":
                                record_finding(
                                    ledger, hid, "confirmed",
                                    "诊断报告已生成，系统根据已有协调员确认证据自动确认。",
                                    node.get("probability", 75),
                                )
                            else:
                                node["status"] = "inconclusive"
                                node["_inconclusive_count"] = (
                                    node.get("_inconclusive_count", 0) + 1)
                                node["verdict_reason"] = (
                                    "诊断报告已生成，该假设验证未得出明确结论，"
                                    "系统标记为未完成。"
                                )
                                node["selected"] = False
                                node["evidence"].append(new_evidence(
                                    source="coordinator",
                                    summary=node["verdict_reason"],
                                    supports=False,
                                ))
                            finalized_any = True
                        elif status == "pending" and not node.get("deferred"):
                            # Never-verified, never-shelved hypothesis:
                            # close as refuted by the system so the report
                            # does not cite it as an open question.
                            record_finding(
                                ledger, hid, "refuted",
                                "诊断报告已生成，核心假设已确认，该假设未被验证，系统自动关闭。",
                                node.get("probability", 5),
                            )
                            node["terminal_reason"] = "auto_finalized"
                            finalized_any = True
                        elif status in ("pending", "inconclusive") and node.get("deferred"):
                            # Deferred (shelved) hypotheses were never
                            # verified.  The report can still cite them as
                            # root causes (observed 2026-07-19 session
                            # cd94408a: H6 shelved but written as a
                            # confirmed root cause).  Mark inconclusive so
                            # the ledger accurately reflects "not
                            # verified" after the report is written.
                            node["status"] = "inconclusive"
                            node["_inconclusive_count"] = (
                                node.get("_inconclusive_count", 0) + 1)
                            node["verdict_reason"] = (
                                "诊断报告已生成，该假设搁置后未完成验证，"
                                "系统标记为未验证。"
                            )
                            node["selected"] = False
                            node["evidence"].append(new_evidence(
                                source="coordinator",
                                summary=node["verdict_reason"],
                                supports=False,
                            ))
                            finalized_any = True
                    # Always persist after write_file so that report content
                    # is saved to the ledger JSON regardless of whether
                    # auto-finalize ran.  Without this, the report field is
                    # lost when all hypotheses were already in terminal state.
                    # (Phase no longer written — derived on demand.)
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

        # ── Record tool execution duration ──
        _tool_duration = round(_ttime.monotonic() - _tool_start, 3)
        self._tool_metrics.append({
            "round": self._model_call_count,
            "tool_name": tool_name,
            "duration_s": _tool_duration,
        })

        return result

    # ── Safety mechanisms: max-round, loop detection, stagnation fallback ──

    def _build_safety_warnings(self, ledger: DiagnosisLedger) -> tuple[str, bool]:
        """Build safety directive messages injected into the system prompt.

        Returns ``(warnings_text, mutated)``.  ``mutated`` is True when a
        valve changed ledger state during this pass (auto-verdict via
        record_finding / forced REPORT / argus degrade) — the caller then
        re-renders the ledger context, which was rendered BEFORE these
        mutations and would otherwise contradict the valve directives.

        Safety mechanisms prevent infinite loops and premature termination:

        1. **Max rounds (P1)**: Progress-aware round cap — grants a one-shot
           grace when diagnosis is still advancing; forces REPORT only after
           progress stalls past the cap (or the grace is exhausted).
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
        mutated = False  # set when a valve changes ledger state below

        # ── SBR belief-ledger consistency corrections (one-shot) ──
        # Stored by the post-response SBR guard (§12); rendered here so
        # the correction sits in the safety block at the prompt tail.
        # Popping makes them one-shot — they describe a divergence the
        # LLM is expected to act on THIS round; the guard re-fires next
        # round if it persists.  Not a ledger mutation that affects the
        # context block (the key is never rendered there), so the
        # caller's re-render trigger (`mutated`) stays untouched.
        _sbr_pending = ledger.pop("_sbr_corrections", None)
        if _sbr_pending:
            warnings.extend(_sbr_pending)

        # ── P1: Maximum round limit — progress-aware, then synthesis + REPORT ──
        # The round cap is a stagnation backstop, not a blunt cutoff: when
        # the diagnosis is still making progress (recent record_finding /
        # commit_hypotheses / select_path / new evidence), grant a one-shot
        # grace extension instead of interrupting mid-verification.  Only
        # fire when the process has gone quiet — no ledger progress in the
        # last _PROGRESS_WINDOW rounds — or after the grace was consumed.
        # ── Scenario-aware round cap ──
        # "Complex" = the diagnosis already needed a retry root batch —
        # signals the straightforward path failed.  Such sessions get a
        # tighter cap: history shows rounds beyond ~24 in this state are
        # unproductive wandering.  (v2.7 flat model: the depth≥1 clause
        # was removed with sub-hypotheses.)
        is_complex = bool(ledger.get("_root_commit_count", 0) > 1)
        cap = _MAX_ROUNDS_COMPLEX if is_complex else _MAX_ROUNDS
        grace = _MAX_ROUNDS_COMPLEX_GRACE if is_complex else _MAX_ROUNDS_GRACE

        # ── G1 最大轮次安全阀（state-machine-v2.md §8）：进展感知停滞兜底 ──
        if round_num >= cap and not ledger.get("_forced_terminal"):
            last_progress = max(
                self._last_finding_round,
                self._last_commit_round,
                self._last_select_round,
                self._last_evidence_round,
            )
            progressing = (
                last_progress > 0
                and round_num - last_progress <= _PROGRESS_WINDOW
            )
            if progressing and not self._max_rounds_grace_used:
                self._max_rounds_grace_used = True
                warnings.append(
                    f"⚠ [系统提醒] 已接近诊断轮次上限（第{round_num}轮"
                    f"/上限{cap}轮），检测到诊断仍在推进"
                    f"（最近进展在第{last_progress}轮）。\n"
                    f"已给予 {grace} 轮宽限：请在 "
                    f"{cap + grace} 轮内完成当前验证"
                    "并生成诊断报告，优先收敛已有假设而非开启新方向。"
                )
                logger.info(
                    "Safety: max rounds (%d, complex=%s) reached but diagnosis "
                    "progressing (last progress round %d) — granted %d-round "
                    "grace", round_num, is_complex, last_progress, grace,
                )
            elif round_num >= cap + (
                grace if self._max_rounds_grace_used else 0
            ):
                _force_report(ledger)
                ledger["_forced_terminal"] = True
                self._report_phase_start_round = round_num
                mutated = True
                # Replace the blunt "STOP NOW" directive with a comprehensive
                # diagnostic synthesis that helps the LLM produce a quality
                # report from everything learned so far.
                synthesis = _build_diagnostic_synthesis(ledger, cap)
                warnings.append(synthesis)
                logger.warning(
                    "Safety: max rounds (%d, complex=%s, grace_used=%s) reached "
                    "with no recent progress — injected diagnostic synthesis, "
                    "forcing REPORT phase",
                    round_num, is_complex, self._max_rounds_grace_used,
                )

        # ── Understand-phase stagnation safety valve ──
        # Runs independently of _MIN_ROUND_FOR_SAFETY.  Differentiate
        # between:
        #   (a) Diagnostic data WAS collected → force commit_hypotheses
        #       (round >= 3 only — at round 2 the LLM commits in its
        #       response on the normal path, so a round-2 directive is a
        #       false "系统强制" injection observed in every session)
        #   (b) Diagnostic data was NOT collected → force data collection
        #       (round >= 2 — catches round-1 text simulation early)
        #   (c) Partial Argus coverage → force the missing expert (round >= 2)
        #   (d) All experts returned empty data → short-circuit to REPORT
        # Without this valve, the LLM may output analysis as plain text
        # instead of calling commit_hypotheses, causing the loop to exit.
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
                # ── Dual-expert coverage check (container/K8s scenarios) ──
                # If only one type of Argus expert was delegated, the LLM
                # likely missed the other.  Force delegation of
                # the missing expert instead of prematurely forcing
                # commit_hypotheses.
                #
                # NOTE: Coordinator does NOT have Argus tools directly —
                # detection uses delegated_experts (populated from
                # task(subagent_type=...) calls), NOT tools_called.
                experts_delegated: set[str] = set()
                has_delegated = False
                for r in rounds_history:
                    experts_delegated.update(r.get("delegated_experts", []))
                    if "task" in r.get("tools_called", []):
                        has_delegated = True

                has_host = "host-argus-expert" in experts_delegated
                has_k8s = "k8s-argus-expert" in experts_delegated
                is_host_only = ledger.get("entity_type") == "host"

                if ledger.get("_argus_unavailable"):
                    # Argus platform failure (v2.5 degrade): coverage and
                    # delegation interventions are pointless — argus is
                    # unavailable.  Just keep nudging commit from user
                    # input; the normal flow continues with domain
                    # experts collecting evidence in VERIFY.
                    if round_num >= 3 and _phase(ledger) == "hypothesize":
                        warnings.append(
                            "⚠ [系统提示] Argus 监控平台故障，监控数据不"
                            "可用。\n请基于用户描述的故障现象调用 "
                            "commit_hypotheses 提出假设，"
                            "后续专家诊断将继续验证并修正假设。"
                        )
                # Only intervene when an expert WAS delegated but one
                # domain is still uncovered — this indicates a container
                # scenario where both experts are expected.
                # Host-only scenarios skip this check entirely.
                elif has_delegated and not is_host_only and has_host and not has_k8s:
                    warnings.append(
                        "⚠ [系统强制] 已采集主机 Argus 数据但缺少集群级 "
                        "K8s Argus 数据（query_argus_nodes + "
                        "query_argus_services）。\n"
                        "禁止在本轮调用 commit_hypotheses。\n"
                        "你必须先调用 task(subagent_type=\"k8s-argus-expert\", description=...) "
                        "委派 K8s Argus 专家采集集群指标。\n"
                        "收到 K8s Argus 专家的分析摘要后再提交假设。"
                    )
                    logger.warning(
                        "Safety: partial Argus coverage (host only, "
                        "round %d) — forcing k8s-argus-expert delegation",
                        round_num,
                    )
                elif has_delegated and not is_host_only and has_k8s and not has_host:
                    warnings.append(
                        "⚠ [系统强制] 已采集 K8s Argus 数据但缺少主机级 "
                        "Argus 数据（CPU/内存/磁盘/网络）。\n"
                        "禁止在本轮调用 commit_hypotheses。\n"
                        "你必须先调用 task(subagent_type=\"host-argus-expert\", description=...) "
                        "委派主机 Argus 专家采集主机指标。\n"
                        "收到主机 Argus 专家的分析摘要后再提交假设。"
                    )
                    logger.warning(
                        "Safety: partial Argus coverage (k8s only, "
                        "round %d) — forcing host-argus-expert delegation",
                        round_num,
                    )
                else:
                    # Data collected but no hypotheses
                    _argus_state = _classify_argus_data(ledger)
                    if _argus_state == "failure" or (
                            _argus_state == "param_missing"
                            and ledger.get("_argus_redelegated")):
                        # Argus platform failure (v2.5): argus returned
                        # empty data / errors — degrade to hypotheses
                        # from user input.  T1 coverage gate is bypassed
                        # via _argus_unavailable; the normal
                        # VERIFY→EVALUATE→REPORT flow continues and
                        # domain experts can still collect evidence.
                        if not ledger.get("_argus_unavailable"):
                            ledger["_argus_unavailable"] = True
                            self._persist_ledger(ledger)
                            mutated = True
                            logger.warning(
                                "Safety: argus platform failure "
                                "(round %d, state=%s) — degrading to "
                                "user-input hypotheses (T1 bypass)",
                                round_num, _argus_state,
                            )
                        warnings.append(
                            "⚠ [系统提示] Argus 监控平台故障（返回空数据"
                            "或报错），监控数据不可用。\n"
                            "请基于用户描述的故障现象调用 "
                            "commit_hypotheses 提出假设，"
                            "后续专家诊断将继续验证并修正假设。"
                        )
                    elif _argus_state == "all_normal":
                        # Metrics genuinely normal — record faithfully
                        # and continue the normal flow: the user still
                        # reported a fault, so hypotheses are built from
                        # user input + the recorded normal baseline.
                        if round_num >= 3 and _phase(ledger) == "hypothesize":
                            warnings.append(
                                "ℹ [系统提示] Argus 指标全部正常（已如实"
                                "记录）。\n用户仍报告了故障现象——请结合"
                                "用户输入与已记录的正常基线调用 "
                                "commit_hypotheses 提出假设。"
                            )
                    elif round_num >= 3 and _phase(ledger) == "hypothesize":
                        # Coverage gate already opened (phase derives to
                        # hypothesize) but the LLM still hasn't committed —
                        # a genuine stagnation signal.  Round-2 exemption:
                        # on the normal path the LLM commits in its round-2
                        # response, so a round-2 directive is always a
                        # false positive.  When the gate has NOT opened yet
                        # (still understand), the partial-coverage branch
                        # above owns the corrective message instead — telling
                        # the LLM to commit here would be rejected by the
                        # phase tool gate.
                        warnings.append(
                            "⛔ [系统强制] 数据采集已完成，当前已是第"
                            f"{round_num}轮，你仍未提交任何假设。\n"
                            "禁止继续调用诊断工具。\n"
                            "立即基于已有 Argus 数据调用 commit_hypotheses，"
                            "否则诊断将提前终止。"
                        )
                        logger.warning(
                            "Safety: hypothesize stagnation with data (round %d, "
                            "no hypotheses) — forcing commit",
                            round_num,
                        )
            else:
                # No data collected.  _coverage_ready treats "never
                # delegated" as ready (ledger.py), so at round>=2 the
                # derived phase is already hypothesize — the old text
                # ("禁止调用 commit_hypotheses，必须委派 Argus") ALWAYS
                # contradicted the phase guidance.  Only demand
                # delegation while genuinely in UNDERSTAND; otherwise
                # nudge committing from user input (same semantics as
                # the argus-degrade path).
                if _phase(ledger) == "understand":
                    warnings.append(
                        "⚠ [系统强制] 当前是第"
                        f"{round_num}轮，你尚未提交任何假设，"
                        "且未调用任何诊断工具采集数据。\n"
                        "禁止输出纯文本分析，禁止调用 commit_hypotheses。\n"
                        "你必须在本轮调用 task() 委派 Argus 专家"
                        "（task(subagent_type=\"host-argus-expert\", description=...) + "
                        "task(subagent_type=\"k8s-argus-expert\", description=...)）"
                        "采集主机和集群的监控指标数据。\n"
                        "这是诊断的第一步——没有数据就无法形成假设。"
                    )
                    logger.warning(
                        "Safety: understand stagnation without data (round %d, "
                        "no hypotheses, no diagnostic tools) — forcing "
                        "data collection",
                        round_num,
                    )
                elif round_num >= 3:
                    warnings.append(
                        "⚠ [系统提示] 当前已是第"
                        f"{round_num}轮，尚未委派专家采集监控数据。\n"
                        "可调用 task() 委派 Argus 专家采集数据，或直接基于"
                        "用户描述的故障现象调用 commit_hypotheses 提出假设，"
                        "后续专家诊断将继续验证并修正假设。"
                    )

        # ── G5 委派饱和（state-machine-v2.md §8）：防止重复委派相同专家验证相同假设 ──
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
            and round_num < _MAX_ROUNDS + _MAX_ROUNDS_GRACE
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
                round_num, _phase(ledger),
            )

        # ── P2/P3: Stagnation & loop detection (only after min round) ──
        # Independent if-block (not elif) so it runs regardless of
        # delegation saturation check above.
        if round_num >= _MIN_ROUND_FOR_SAFETY:
            current_phase = _phase(ledger)
            has_hypotheses = bool(ledger.get("hypotheses"))

            # ── G3 VERIFY 委派无结论（state-machine-v2.md §8）：连续 3 次 task 无 record_finding ──
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
                            _force_report(ledger)
                            self._report_phase_start_round = round_num
                            warnings.append(
                                f"⚠ [系统安全阀] {exit_reason}\n"
                                "系统已自动将活跃假设标记为不确定。\n"
                                "必须立即进入REPORT阶段，调用 write_file 基于已有证据生成诊断报告。"
                            )
                        else:
                            phase = _phase(ledger)
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
                        mutated = True
                        logger.warning(
                            "Safety: auto-inconclusive for %s (round %d, "
                            "consecutive_task=%d)",
                            active_id, round_num,
                            self._consecutive_task_count,
                        )
                else:
                    # ── P2: Loop warning without auto-recording ──
                    # Suggest only phase-legal actions (§7): record_finding
                    # is gated outside VERIFY and write_file is gated until
                    # exit — the previous text ("调用 record_finding …或
                    # 直接生成报告") recommended actions the phase gate
                    # rejects in every phase this branch can fire in.
                    _p2_actions = {
                        "understand": "继续委派 Argus 专家完成数据采集",
                        "hypothesize": "调用 commit_hypotheses 提交假设",
                        "evaluate": "调用 select_path 选择下一步验证方向",
                        "report": "调用 write_file 生成诊断报告",
                    }
                    _p2_hint = _p2_actions.get(
                        current_phase, "按当前阶段指引推进诊断")
                    warnings.append(
                        f"⚠ [系统警告] 已连续{self._consecutive_task_count}次"
                        "委派专家但未记录验证结论。\n"
                        "避免重复委派专家查询相同数据。\n"
                        f"当前处于 {current_phase.upper()} 阶段：{_p2_hint}。"
                    )
                    logger.warning(
                        "Safety: %d consecutive task() calls without "
                        "record_finding (round %d, phase=%s)",
                        self._consecutive_task_count, round_num, current_phase,
                    )

        # ── Verify-phase stuck detection ──
        # Safety net: the active hypothesis has expert evidence but the
        # LLM has made no verification progress (no record_finding AND no
        # new diagnostic evidence) for _VERIFY_STUCK_GAP rounds.  Acts
        # THROUGH the state machine — auto-record a verdict for the stuck
        # hypothesis, then let derive_phase pick the next phase.  It never
        # forces REPORT while no root cause is confirmed: the write_file
        # gate would reject the report, trapping the LLM between
        # contradictory directives.  Cooldown prevents back-to-back fires.
        if (
            _phase(ledger) == "verify"
            and ledger.get("hypotheses")
            and round_num - self._verify_stuck_last_fired >= _VERIFY_STUCK_COOLDOWN
        ):
            active_path = ledger.get("active_path", [])
            active_id = active_path[-1] if active_path else None
            if active_id and active_id in ledger.get("hypotheses", {}):
                node = ledger["hypotheses"][active_id]
                has_expert = any(
                    e.get("source", "").startswith("expert:")
                    for e in node.get("evidence", [])
                )
                # A fresh select_path/backtrack restarts the verification
                # clock — the LLM just committed to a recovery plan for
                # this hypothesis and must get the full gap window to
                # execute it (aligned with the P1 max-rounds valve, which
                # already counts _last_select_round as progress; observed
                # 2026-07-23 session 7805961b: valve fired 1 round after
                # select_path(H2), preempting the confirmation attempt).
                stall_ref = max(self._last_finding_round,
                                self._last_evidence_round,
                                self._last_select_round)
                # ── Delegation-in-flight exclusion (G4 tightening) ──
                # A task() issued THIS round or last round means the
                # expert result may not have come back yet — the "stuck"
                # window has not genuinely elapsed.  Count the stall from
                # the last delegation round, not from the last finding.
                if (has_expert
                        and node.get("status") in ("pending", "inconclusive")
                        and round_num - max(stall_ref, self._last_task_round) >= _VERIFY_STUCK_GAP):
                    has_coordinator_support = any(
                        e.get("supports") and e.get("source") == "coordinator"
                        for e in node.get("evidence", [])
                    )
                    if has_coordinator_support:
                        auto_verdict = "confirmed"
                        record_finding(
                            ledger, active_id, "confirmed",
                            f"[系统自动] 已获得专家验证数据且协调员已确认"
                            f"（第{round_num}轮），系统自动确认。",
                            node["probability"],
                        )
                    else:
                        auto_verdict = "inconclusive"
                        record_finding(
                            ledger, active_id, "inconclusive",
                            f"[系统自动] 已获得专家验证数据但未得到协调员确认"
                            f"（第{round_num}轮），系统标记为未完成。",
                            node["probability"],
                        )
                        # record_finding already marks the node
                        # inconclusive and clears selected (5-state
                        # model); ensure the node is no longer the
                        # verify focus so derive_phase can advance now.
                        ledger["hypotheses"][active_id]["selected"] = False
                    # Let the state machine decide the next phase — REPORT
                    # only when exit conditions are genuinely met (the
                    # write_file gate passes then); otherwise continue the
                    # hypothesize → verify → evaluate loop.  Phase is
                    # derived (not persisted).
                    new_phase = _phase(ledger)
                    _stuck_guidance = {
                        "report": (
                            "⛔ 诊断已满足报告条件。必须立即调用 write_file "
                            f"将诊断报告写入 {self.report_path}。\n"
                            "禁止调用任何诊断工具、委派专家、record_finding、"
                            "commit_hypotheses。"
                        ),
                        "hypothesize": (
                            "请调用 commit_hypotheses 提交新一批假设"
                            f"（第 {ledger.get('_commit_count', 0) + 1}"
                            f"/{MAX_COMMIT_CALLS} 次）——"
                            "必须基于已排除证据换方向推断，"
                            "禁止与已排除假设重复或仅换措辞。"
                        ),
                        "evaluate": (
                            "请调用 select_path 选择下一个待验证假设，"
                            "继续诊断循环。"
                        ),
                        "backtrack": (
                            "请调用 backtrack 回溯到可恢复的假设，继续验证。"
                        ),
                    }
                    guidance = _stuck_guidance.get(
                        new_phase,
                        "请继续验证当前活跃假设，获得结论后立即调用 "
                        "record_finding。",
                    )
                    warnings.append(
                        f"⛔ [系统安全阀] 假设 {fmt_hid(active_id)} 已获得专家数据但 "
                        f"{round_num - stall_ref} 轮无验证进展"
                        f"（第{round_num}轮），系统已自动标记为 "
                        f"{auto_verdict}。\n{guidance}"
                    )
                    self._persist_ledger(ledger)
                    self._consecutive_task_count = 0
                    self._same_delegate_count = 0
                    self._last_delegate_key = ""
                    self._last_finding_round = round_num
                    self._verify_stuck_last_fired = round_num
                    mutated = True
                    logger.warning(
                        "Safety: verify-phase stuck for %s (round %d, no "
                        "progress since round %d) — auto-%s, phase→%s",
                        active_id, round_num, stall_ref, auto_verdict,
                        new_phase,
                    )

        # ── REPORT phase write_file safety valve ──
        # If the LLM entered REPORT phase but didn't call write_file,
        # inject a progressively stronger directive.
        current_phase = _phase(ledger)
        if (current_phase == "report"
                and not ledger.get("report")):
            # Track when REPORT phase started
            if self._report_phase_start_round == 0:
                self._report_phase_start_round = round_num
            # Fire on the FIRST REPORT round so the LLM never wastes
            # cycles on record_finding — all pending hypotheses are
            # already auto-finalized by the Phase guard.
            warnings.append(
                "⛔ [系统强制] 已进入 REPORT 阶段"
                f"（第{self._report_phase_start_round}轮起）。\n"
                "⛔ 不要输出文本 — 你的唯一任务是立即调用 write_file。\n"
                "系统已自动终结所有挂起假设，你无需再调用 "
                "record_finding / commit_hypotheses。\n"
                "禁止调用任何诊断工具、委派专家、record_finding、"
                "commit_hypotheses。\n"
                "禁止输出分析、总结或复盘文字。\n"
                f"必须立即调用 write_file 将诊断报告写入 {self.report_path}。\n"
                "报告内容基于上方 <diagnosis_ledger> 中的证据链和根因。"
            )
            if round_num - self._report_phase_start_round >= 1:
                logger.warning(
                    "Safety: REPORT phase round %d without write_file "
                    "(started round %d) — re-injecting mandatory "
                    "write_file directive (no auto-write)",
                    round_num, self._report_phase_start_round,
                )
        elif current_phase != "report":
            # Reset trackers when leaving REPORT phase (e.g. backtrack)
            self._report_phase_start_round = 0

        return ("\n\n".join(warnings) if warnings else ""), mutated

    # ── Tool: commit_hypotheses ──

    def _make_commit_hypotheses_tool(self) -> StructuredTool:
        mw = self

        async def _run(
            hypotheses: list[HypothesisSpec],
            focus_summary: str = "",
        ) -> str:
            ledger = mw._current_ledger or new_ledger()

            # Convert Pydantic objects to plain dicts (args_schema parses JSON)
            specs = [h.model_dump() if hasattr(h, "model_dump") else dict(h) for h in hypotheses]

            try:
                created = add_hypotheses(ledger, specs)
            except ValueError as e:
                return f"假设提交失败: {e}"

            # Update phase and track creation round
            mw._last_commit_round = mw._model_call_count

            # Build confirmation
            lines = [f"已提交 {len(created)} 个假设:"]
            for h in created:
                sel = " ★" if h["selected"] else ""
                lines.append(f"  {fmt_hid(h['id'])} [{h['status']} p={h['probability']}%]{sel} {h['statement']}")
            _budget_left = MAX_COMMIT_CALLS - ledger.get("_commit_count", 0)
            if _budget_left > 0:
                lines.append(
                    f"（提交预算剩余 {_budget_left}/{MAX_COMMIT_CALLS} 次）"
                )
            else:
                lines.append(
                    f"（提交预算已用尽 {MAX_COMMIT_CALLS}/{MAX_COMMIT_CALLS} 次；"
                    "后续如需细化假设，请用 record_finding(statement_update=...)）"
                )

            mw._persist_ledger(ledger)
            mw._current_ledger = ledger

            return "\n".join(lines)

        return StructuredTool.from_function(
            name="commit_hypotheses",
            description=(
                "提交诊断假设（HYPOTHESIZE阶段必须调用）。每次最多3个假设，按概率降序。"
                "本工具只创建待验证假设，不产生任何结论；确认或证伪已有假设必须调用 "
                "record_finding，重复提交不会确认任何假设。"
                "假设为扁平结构（ID 为整数编号如 H1、H2），不支持在某假设下提交子假设；"
                "如需更具体的表述，用 record_finding(statement_update=...) 修正。"
                "整个诊断最多提交2次（首批、换批共用此预算），总计不超过6个假设。"
                "预算用尽后如需细化，用record_finding(statement_update=...)修正已有假设表述。"
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

            # Normalize the LLM-supplied ID first (v2.7 flat-model
            # validation) so all checks below compare canonical IDs.
            try:
                selected_hypothesis_id = resolve_hypothesis_id(
                    ledger, selected_hypothesis_id)
            except ValueError as e:
                return f"路径选择失败: {e}"

            # Convert Pydantic objects to plain dicts
            dep_dicts = [d.model_dump() if hasattr(d, "model_dump") else dict(d)
                         for d in (deprioritized or [])]

            # ── Idempotent select: already the active focus ──
            # commit_hypotheses auto-selects the highest-probability
            # hypothesis, so a select_path on that same hypothesis is a
            # no-op that must NOT churn state (a full select_path would
            # re-deprioritize its siblings for no benefit).  Explicit
            # deprioritized reasons are still applied — they express
            # genuine Coordinator intent.
            hypotheses = ledger.get("hypotheses", {})
            target = hypotheses.get(selected_hypothesis_id, {})
            active_id = (ledger.get("active_path") or [None])[-1]
            if (selected_hypothesis_id == active_id
                    and target.get("status") in ("pending", "inconclusive")
                    and target.get("selected")):
                for dep in dep_dicts:
                    _n = _parse_hypothesis_num(dep.get("id", ""))
                    dep_id = str(_n) if _n is not None else ""
                    if dep_id in hypotheses and dep_id != selected_hypothesis_id:
                        hypotheses[dep_id]["deferred"] = True
                        hypotheses[dep_id]["selected"] = False
                        hypotheses[dep_id]["deferred_reason"] = dep.get("reason", "")
                mw._persist_ledger(ledger)
                mw._current_ledger = ledger
                return (
                    f"{fmt_hid(selected_hypothesis_id)} 已是当前验证焦点"
                    "（commit 时已自动选中最高概率假设），无需重复 select_path。\n"
                    "请直接调用 task() 委派专家验证该假设。"
                )

            try:
                select_path(ledger, selected_hypothesis_id, rationale, dep_dicts)
            except ValueError as e:
                return f"路径选择失败: {e}"


            selected = ledger["hypotheses"][selected_hypothesis_id]
            mw._persist_ledger(ledger)
            mw._current_ledger = ledger

            return (
                f"已选择路径: {fmt_hid(selected_hypothesis_id)} {selected['statement']}\n"
                f"理由: {rationale}\n"
                f"活动路径: {' → '.join(fmt_hid(h) for h in ledger['active_path'])}"
            )

        return StructuredTool.from_function(
            name="select_path",
            description=(
                "选择1条最接近根因的假设路径深入（EVALUATE阶段必须调用）。"
                "未选中的假设自动搁置（deferred），可后续 backtrack 恢复。"
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

            # Normalize the LLM-supplied ID first (v2.7 flat-model
            # validation) so all guards below compare canonical IDs.
            try:
                hypothesis_id = resolve_hypothesis_id(ledger, hypothesis_id)
            except ValueError as e:
                return f"记录失败: {e}"

            # Capture old statement for root_causes sync
            old_statement = ""
            if statement_update and hypothesis_id in ledger.get("hypotheses", {}):
                hnode = ledger["hypotheses"][hypothesis_id]
                # Defect C: refuted hypotheses must not have their statement
                # modified — the original hypothesis statement should be
                # preserved for historical accuracy.
                if hnode.get("status") == "refuted":
                    return (
                        f"⚠️ 假设 {fmt_hid(hypothesis_id)} 已被证伪(status=refuted)，"
                        "禁止修改假设表述(statement_update)。"
                        "请使用新的证据描述发现，不要修改已排除假设的原始表述。"
                    )
                old_statement = hnode.get("statement", "")

            # Defect D: Terminal-state guard (v2.7.1 relaxed).
            # Verdict FLIPS remain forbidden — they prevent the LLM from
            # overwriting a correct verdict (e.g. record_finding(H1,
            # refuted) after record_finding(H1, confirmed)).  But a
            # confirmed node MAY be re-recorded with verdict=confirmed
            # to strengthen probability / refine the statement / attach
            # new evidence: the flat model has no deepening path, so
            # this is the only channel to rescue a weak confirmation
            # (confirmed@p<80 never satisfies S1's p≥80 bar — observed
            # 2026-07-30 session e7fa8b74 exit lockout).  refuted nodes
            # stay fully closed.
            if hypothesis_id in ledger.get("hypotheses", {}):
                _hnode = ledger["hypotheses"][hypothesis_id]
                _terminal = _hnode.get("status")
                if _terminal == "confirmed" and verdict != "confirmed":
                    return (
                        f"⚠️ 假设 {fmt_hid(hypothesis_id)} 已处于终态"
                        f"(status=confirmed, "
                        f"p={_hnode.get('probability')}%)，"
                        f"禁止翻转为 {verdict}。"
                        "若证据强化或表述需修正，可再次调用 "
                        "record_finding(verdict='confirmed', "
                        "probability_update=..., statement_update=...)；"
                        "若确需推翻已确认结论，请用 backtrack 恢复其他假设"
                        "继续验证（confirmed 结论不可直接翻转）。"
                    )
                if _terminal == "refuted":
                    return (
                        f"⚠️ 假设 {fmt_hid(hypothesis_id)} 已处于终态"
                        f"(status=refuted, "
                        f"p={_hnode.get('probability')}%)，"
                        "不允许重复记录验证结论。"
                        "该假设的验证已关闭，请基于其他未决假设继续诊断；"
                        "全部假设终态后系统会自动进入 REPORT。"
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

            # ── Root-cause tracking (v2.7 flat model) ──
            # Every confirmed hypothesis IS a root cause — there are no
            # sub-hypotheses, so no topmost/refinement disambiguation is
            # needed.  Multi-root scenarios simply accumulate confirmed
            # nodes.
            cids: list[str] = ledger.setdefault("root_cause_hypothesis_ids", [])
            causes: list[str] = ledger.setdefault("root_causes", [])
            hypotheses = ledger.get("hypotheses", {})
            for hid, node in hypotheses.items():
                if node.get("status") != "confirmed":
                    continue
                if hid in cids:
                    continue  # already tracked
                cids.append(hid)
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

            # ── Cleanup: drop non-confirmed entries ──
            # Ensures refuted/deprioritized hypotheses never appear as
            # root causes.
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

            # ── Exit is decided SOLELY by check_exit_conditions (§6.3) ──
            # Removed the legacy "propagation-aware" override: its
            # suppression string ("部分根因") has not appeared in any
            # check_exit_conditions reason since the v2.1 quantified exit
            # model, so the guard was dead and the override fired on EVERY
            # record_finding while any confirmed node existed in the tree
            # (cids registration has no p≥80 threshold) — forcing REPORT
            # over significant residual uncertainty (multi-root
            # truncation), pending sub-hypotheses (deepening truncation),
            # and even sub-80 confirmations.  The v2.4 deep-confirmation
            # fallback inside check_exit_conditions covers the override's
            # intended case through the legitimate S1/S2 path.
            if should_exit:
                _force_report(ledger)
            # else: phase is derived — partial-root / normal cases land on
            # evaluate/verify automatically, no snapshot write needed.

            mw._persist_ledger(ledger)
            mw._current_ledger = ledger

            if should_exit:
                exit_hint = (
                    f"\n⚠ {reason} — 进入REPORT阶段。\n"
                    "⛔ 你必须在下一轮调用 write_file 将诊断报告写入"
                    f" {mw.report_path}。\n"
                    "禁止调用任何诊断工具，禁止输出纯文本分析。"
                    "立即基于 <diagnosis_ledger> 中的证据链生成报告并调用 write_file。"
                )
                # ── Do NOT auto-generate report here ──
                # The safety valve in _build_safety_warnings fires on the
                # next round when ledger["report"] is still empty, giving
                # the LLM a chance to call write_file and produce a proper
                # report.  If the LLM still doesn't call write_file, the
                # REPORT stall fallback (level 1/2 escalation) handles
                # auto-generation as a last resort.
                #
                # NOTE: deliberately do NOT pre-set _report_phase_start_round
                # here.  The REPORT-stall valve (G6) timestamps the FIRST
                # round where the derived phase actually renders as report
                # (i.e. the next before_model call) — pre-setting it to
                # THIS round would fire the "without write_file" warning on
                # the very first REPORT round, before the LLM had any
                # chance to comply (observed 2026-07-20 conntrack session:
                # warning at round 8 after record_finding triggered exit
                # at round 7).
            elif (_phase(ledger) == "hypothesize"
                    and ledger.get("_root_commit_count", 0) > 0):
                # All prior root hypotheses hard-failed and the retry
                # budget is still open — nudge the LLM to submit a
                # fresh batch in a new direction.
                exit_hint = (
                    f"\n⚠ {reason}\n"
                    "⛔ 请调用 commit_hypotheses 提交新一批假设"
                    f"（第 {ledger.get('_commit_count', 0) + 1}"
                    f"/{MAX_COMMIT_CALLS} 次，最后一次）——"
                    "必须基于已排除证据换方向推断，"
                    "禁止与已排除假设重复或仅换措辞。"
                )
            else:
                exit_hint = ""
            stmt_hint = f"\nℹ 假设表述已修正为: {statement_update}" if statement_update else ""
            return (
                f"已记录验证结果: {fmt_hid(hypothesis_id)} → {verdict} (p={probability_update}%)\n"
                f"{evidence_summary}{stmt_hint}{exit_hint}"
            )

        return StructuredTool.from_function(
            name="record_finding",
            description=(
                "记录假设验证结论（VERIFY 阶段验证后必须调用；EVALUATE 阶段亦可调用——"
                "confirmed 强化、次要根因确认、statement_update 精化）。"
                "verdict: confirmed(已证实，可同时存在多个确认的根因/加剧因素)"
                " | refuted(证据明确证伪，该假设不成立)"
                " | inconclusive(证据不足)。"
                "注意：多个假设可以同时为confirmed（多根因场景），refuted仅用于证据明确否定的假设。"
                "⚠ 若假设仅**部分不成立**（因果链中某一环节被证伪，但核心机制已被证据确认"
                "——例如'I/O饱和→leader切换→API延迟'中切换未发生、但饱和与延迟均真实），"
                "应调用 confirmed + statement_update=剔除不成立环节后的表述，"
                "而非整体 refuted——整体 refuted 会把已确认的真实机制从根因列表中丢弃，"
                "导致台账根因与报告结论不一致。"
                "假设为扁平结构（无子假设），细化假设只能通过本工具的 statement_update 完成。"
                "⚠ 确认根因时 probability_update 建议≥80：退出判据要求至少1个 p≥80 的 "
                "confirmed 假设，低于 80 的 confirmed 不被视为已确认根因。"
                "已 confirmed 的假设允许再次调用本工具强化置信度/修正表述/补充证据"
                "（verdict 必须仍为 confirmed，禁止翻转；这是已确认假设唯一的精化通道）；"
                "refuted 假设不可再记录。"
                "所有root假设均到达终态且满足退出判据时自动进入REPORT阶段。"
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
                # No backtrack target.  If the root-batch budget is not
                # exhausted, route to HYPOTHESIZE for a fresh batch
                # instead of forcing REPORT.
                if can_commit_root_hypotheses(ledger):
                    # Phase derives to hypothesize via T8 (all roots
                    # hard-failed + budget left) — no snapshot write.
                    mw._persist_ledger(ledger)
                    mw._current_ledger = ledger
                    return (
                        "没有可回溯的假设，但假设提交预算未用尽"
                        f"（已用 {ledger.get('_commit_count', 0)}"
                        f"/{MAX_COMMIT_CALLS} 次）。\n"
                        "已进入 HYPOTHESIZE 阶段。\n"
                        "⛔ 请调用 commit_hypotheses 提交新一批假设——"
                        "必须基于已排除证据换方向推断，"
                        "禁止与已排除假设重复或仅换措辞。"
                    )
                ledger["exhausted"] = True
                _force_report(ledger)
                mw._persist_ledger(ledger)
                mw._current_ledger = ledger
                # NOTE: _report_phase_start_round intentionally NOT
                # pre-set — G6 timestamps the first derived REPORT round
                # itself (see record_finding for rationale).
                return (
                    "回溯失败: 没有可回溯的假设，且假设提交预算已用尽"
                    f"（{MAX_COMMIT_CALLS} 次）。\n"
                    "已进入 REPORT 阶段（标注假设穷尽）。\n"
                    "⛔ 你必须在下一轮调用 write_file 将诊断报告写入"
                    f" {mw.report_path}。\n"
                    "禁止调用任何诊断工具，立即基于 <diagnosis_ledger> "
                    "中的证据链生成报告。"
                )

            new_active = ledger["hypotheses"][new_active_id]
            mw._persist_ledger(ledger)
            mw._current_ledger = ledger

            return (
                f"已回溯到: {new_active_id} {new_active['statement']}\n"
                f"活动路径: {' → '.join(ledger['active_path'])}\n"
                f"当前阶段: {_phase(ledger)}"
            )

        return StructuredTool.from_function(
            name="backtrack",
            description=(
                "回溯到最近的搁置（deferred）或 pending/inconclusive 假设重新验证"
                "（EVALUATE 阶段当前层全部 refuted 时调用）。"
                "如果没有可回溯的假设，自动进入 REPORT 阶段。"
            ),
            coroutine=_run,
        )

    # ── Tool: report_beliefs (SBR v2, state-machine-v2.md §12) ──

    def _make_report_beliefs_tool(self) -> StructuredTool:
        mw = self

        async def _run(
            phase: str = "",
            beliefs: str = "",
            root_cause_candidate: str = "",
            menu_choice: str = "",
        ) -> str:
            ledger = mw._current_ledger
            if ledger is None:
                return "错误: 诊断台账不存在"

            # Parse the flat string args into the sbr dict consumed by
            # the consistency guard.  All parsing is lenient — an
            # unparseable field simply means "no opinion".
            sbr: dict = {}
            p = phase.strip().lower()
            if p in _SBR_KNOWN_PHASES:
                sbr["phase"] = p
            b = parse_beliefs_field(beliefs)
            if b:
                sbr["beliefs"] = b
            rc = parse_root_cause_field(root_cause_candidate)
            if rc:
                sbr["root_cause"] = rc
            if menu_choice.strip():
                sbr["menu_choice"] = menu_choice.strip()

            # Persist the raw self-report for the ledger-context mirror
            # (rendered into the NEXT round's prompt tail by
            # render_ledger_context — highest-salience position).
            ledger["_sbr_latest"] = {
                "round": mw._model_call_count,
                "phase": p or phase.strip(),
                "beliefs": beliefs.strip(),
                "root_cause_candidate": root_cause_candidate.strip(),
                "menu_choice": menu_choice.strip(),
            }

            lines = [
                "已记录认知状态。"
                "（每轮行动前都请先调用 report_beliefs 自报——"
                "它让系统能在你偏离台账时当场提示。）"
            ]
            # Deterministic belief-ledger consistency checks (C0-C3).
            # Sibling tool calls of the same round (stashed post-response
            # in awrap_model_call) suppress rules the model is already
            # resolving right now.  Corrections ride the tool result —
            # the channel the model demonstrably reads and acts on.
            # Advisory by design: a checker failure degrades to "no
            # opinion", never to a tool error.
            try:
                corrections = _check_belief_consistency(
                    sbr, ledger, mw._round_tool_calls or [],
                )
            except Exception as _chk_exc:  # noqa: BLE001 — advisory only
                logger.debug("SBR consistency check skipped on error: %s",
                             _chk_exc)
                corrections = []
            if corrections:
                lines.extend(msg for _rid, msg in corrections)
                _sbr_log = ledger.setdefault("_sbr_log", [])
                _sbr_log.append({
                    "round": mw._model_call_count,
                    "rules": [rid for rid, _ in corrections],
                })
                del _sbr_log[:-50]  # bounded audit trail
                logger.warning(
                    "SBR guard: %s (round=%d, phase=%s)",
                    ",".join(rid for rid, _ in corrections),
                    mw._model_call_count, _phase(ledger),
                )

            mw._persist_ledger(ledger)
            mw._current_ledger = ledger
            return "\n".join(lines)

        return StructuredTool.from_function(
            name="report_beliefs",
            description=(
                "自报当前认知状态（每轮例行，先于其他动作；可与其他工具同轮并行调用）。"
                "四个扁平字符串字段：phase（当前阶段 understand/hypothesize/verify/"
                "evaluate/report）；beliefs（各假设判断，如 '1=refuted@5, 3=pending@20'）；"
                "root_cause_candidate（根因@置信度，如 'DockerHub限速@95'，尚无填 none）；"
                "menu_choice（本轮动作对应的相位菜单选项及一句话理由）。"
                "自报不改变台账——确认/证伪仍须经 record_finding 正式提交。"
                "系统会将自报与台账做一致性校准，发现偏差时在本工具结果中提示纠偏。"
            ),
            coroutine=_run,
            args_schema=ReportBeliefsInput,
        )

    # ── Auto-report generation (P11 safety net) ──

    @staticmethod
    def _generate_report_from_ledger(ledger: DiagnosisLedger) -> str:
        """Generate a diagnostic report from ledger evidence.

        Used as a last-resort safety net when the LLM exits the REPORT
        phase without calling write_file after being given a second
        chance (level-2 escalation), and by the server-side disconnect
        fallback (app.py CancelledError handler) — hence a staticmethod
        callable without a middleware instance.

        Extracts: root cause, evidence chain, round timeline, expert
        delegation summary, and excluded hypotheses — producing a
        report comparable in structure to an LLM-written one.
        """
        # Never clobber an LLM-authored report (v2.5): when the ledger
        # already carries a non-stub report, return it unchanged.  The
        # stub is strictly a last-resort fallback for "LLM unable to
        # call" — LLM-authored content always wins.
        _existing = ledger.get("report")
        if _existing and not ledger.get("_report_auto_generated"):
            return _existing

        import datetime as _dt

        # Mark the report as system-generated (not LLM-authored) so the
        # REPORT-phase guidance can invite the LLM to overwrite it via
        # write_file, and the REPORT-stall handler can salvage a
        # substantial LLM text response in its place (G10).
        ledger["_report_auto_generated"] = True

        hypotheses: dict = ledger.get("hypotheses", {})
        rounds_data: list = ledger.get("rounds", [])
        root_causes: list = ledger.get("root_causes", [])
        lines: list[str] = []

        # ── Header ──
        lines.append("# 故障诊断报告\n")
        lines.append(
            f"**诊断日期**：{_dt.datetime.now().strftime('%Y-%m-%d')}"
        )
        lines.append("> ⚠ 本报告由系统自动生成（LLM 未在 REPORT 阶段调用 write_file）。"
                     "内容基于诊断台账中的结构化证据。\n")

        # ── Fault overview ──
        if root_causes:
            lines.append(f"**故障概述**：{root_causes[0]}")

        # ── Confidence ──
        confirmed = [
            h for h in hypotheses.values()
            if h.get("status") == "confirmed"
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

        # ── Key findings timeline (from round history) ──
        if rounds_data:
            lines.append("## 关键发现时间线\n")
            sorted_rounds = sorted(
                rounds_data, key=lambda r: (r.get("round", 0),
                                            r.get("_seq", 0)),
            )
            for r in sorted_rounds:
                experts = r.get("delegated_experts") or []
                expert_label = f" [{experts[0]}]" if experts else ""
                kf = (r.get("key_findings") or "").strip()
                if "task" not in r.get("tools_called", []):
                    continue  # skip non-expert rounds
                # Truncate long findings
                kf_short = kf
                if kf_short:
                    lines.append(
                        f"- **第{r['round']}轮{expert_label}**: {kf_short}"
                    )
            lines.append("")

        # ── Evidence chain (from confirmed hypotheses) ──
        lines.append("## 证据链\n")
        if confirmed:
            for h in sorted(
                confirmed, key=lambda x: x.get("probability", 0),
                reverse=True,
            ):
                hid = h.get("id", "?")
                prob = h.get("probability", 0)
                is_root = hid in set(ledger.get("root_hypothesis_ids", []))
                lines.append(f"### {fmt_hid(hid)}{' [根因]' if is_root else ''}"
                             f" — 置信度 {prob}%\n")
                lines.append(f"{h.get('statement', '')}\n")
                for e in h.get("evidence", []):
                    src = e.get("source", "")
                    supp = ("支持" if e.get("supports") is True
                            else "反驳" if e.get("supports") is False
                            else "未定性")
                    summary = (e.get("summary") or "")[:400]
                    if summary:
                        lines.append(f"- **[{src}]**({supp}): {summary}")
                rationale = h.get("rationale", "")
                if rationale:
                    lines.append(f"\n依据: {rationale}")
                lines.append("")
        else:
            lines.append("（无已确认假设，无可提取证据链。）\n")

        # ── Root cause analysis ──
        if root_causes:
            lines.append("## 根因分析\n")
            for i, rc in enumerate(root_causes, 1):
                lines.append(f"{i}. {rc}")
            lines.append("")

        # ── Excluded / shelved hypotheses ──
        _TERMINAL_REASON_CN = {
            "evidence_saturated": "证据饱和",
            "timeout_unreached": "未及验证",
            "auto_finalized": "系统自动关闭",
        }
        refuted = [
            h for h in hypotheses.values()
            if h.get("status") == "refuted"
        ]
        shelved = [
            h for h in hypotheses.values()
            if h.get("deferred") and h.get("status") != "refuted"
        ]
        if refuted or shelved:
            lines.append("## 排除的假设\n")
            for h in refuted:
                status_cn = _TERMINAL_REASON_CN.get(
                    h.get("terminal_reason", ""), "已排除")
                rationale = (
                    (h.get("verdict_reason") or h.get("rationale") or ""))[:200]
                lines.append(
                    f"- [{status_cn}] {h.get('statement', '')}"
                    f"（概率{h.get('probability', 0)}%）"
                    f"{' — ' + rationale if rationale else ''}"
                )
            for h in shelved:
                reason = (
                    (h.get("deferred_reason") or h.get("rationale") or ""))[:200]
                lines.append(
                    f"- [未验证(搁置)] {h.get('statement', '')}"
                    f"（概率{h.get('probability', 0)}%）"
                    f"{' — ' + reason if reason else ''}"
                )
            lines.append("")

        # ── Experts used ──
        experts_used: set[str] = set()
        for r in rounds_data:
            experts_used.update(r.get("delegated_experts", []))
        if experts_used:
            lines.append("## 诊断方法\n")
            lines.append(f"已委派专家: {', '.join(sorted(experts_used))}")
            lines.append("")

        # ── Repair suggestions ──
        lines.append("## 修复建议\n")
        lines.append("（请根据根因分析结果制定具体修复方案。）\n"
                     "建议优先级：P0 紧急缓解 → P1 短期修复 → P2 长期预防。")
        lines.append("")

        # ── Limitations ──
        lines.append("## 限制说明\n")
        lines.append("本报告由系统自动生成，基于诊断台账中的结构化证据。"
                     "LLM 未在 REPORT 阶段调用 write_file，"
                     "报告内容可能缺少手工撰写的叙述细节。")

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
