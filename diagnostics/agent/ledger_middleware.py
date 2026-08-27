"""Diagnosis ledger middleware — hypothesis-driven diagnosis orchestration.

This middleware:
1. Injects a compact ledger summary into the system message each LLM call
2. Provides three hypothesis management tools (propose_hypotheses, select_path, record_finding)
3. Auto-records evidence from diagnostic tool calls
4. Persists the ledger to the filesystem on key changes

"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import pathlib
import re
from collections.abc import Awaitable, Callable

from langchain.agents.middleware import hook_config
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
    MAX_PROPOSE_CALLS,
    MAX_TOTAL_HYPOTHESES,
    ARGUS_PARAM_MISSING_PATTERNS,
    DiagnosisLedger,
    DiagnosisLedgerState,
    _coverage_ready,
    _economically_dead,
    _parse_hypothesis_num,
    _phase_guidance,
    _required_argus,
    activate_hypothesis,
    expert_verdict_conflict,
    add_evidence_to_active,
    add_hypotheses,
    backtrack,
    can_append_hypothesis,
    can_propose_root_hypotheses,
    check_exit_conditions,
    compute_next_action,
    compute_report_guidance,
    delegation_value_for,
    derive_phase,
    finalize_pending_for_report,
    fmt_hid,
    is_confirmed_root,
    ledger_to_json,
    new_evidence,
    new_ledger,
    record_finding,
    record_round,
    render_derived_report_appendix,
    render_failure_digest,
    render_ledger_context,
    render_unrecorded_evidence_appendix,
    resolve_hypothesis_id,
    select_path,
    unrecorded_evidence_hypotheses,
)
from diagnostics.agent.ledger import _argus_conflict_signal  # noqa: F401  (C1/C2 argus 冲突信号，design document §8 G13 context)
from deepagents.middleware._utils import append_to_system_message

from diagnostics.agent.topology_render import build_argus_compact_view, build_host_views, expert_view, render_json_block

logger = logging.getLogger(__name__)


# ── V2 phase helpers (design document §3) ──
# The ledger no longer persists ``current_phase``; the phase is derived
# on demand.  Two flavours are needed inside this middleware:
#
#   _phase(ledger)          — derived phase, honouring the forced-REPORT
#                             marker below (guardrail equivalent of the
#                             old proposed phase).
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

    Also records _exit_path="forced" so natural exits (E1/E2/E3 via
    check_exit_conditions, which leave _forced_terminal unset and record
    _exit_path="natural") stay distinguishable from guardrail-forced
    ones in post-hoc analysis (design document §3 step 1).
    """
    ledger["_forced_terminal"] = True
    ledger["_exit_path"] = "forced"


# ── Phase tool gating (design document §7) ──
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

# ── L4 hostname normalization gateway (design document §8.1⑥) ──
# Host-level Argus metric tools — their `hostname` parameter contract is a
# physical host name (monitoring endpoint semantics).  The gateway in
# awrap_tool_call normalizes node IPs (node_name) passed by the LLM into
# host names using the in-session topology.  No mock whitelist is involved
# (the scope guard middleware was removed — see README), so the mechanism
# holds for the mock backend and any future production backend alike.
_HOST_ARGUS_TOOLS: frozenset[str] = frozenset({
    "query_argus_cpu", "query_argus_memory",
    "query_argus_disk", "query_argus_network",
})


def _looks_like_ipv4(value: str) -> bool:
    """True when the value is shaped like an IPv4 address (detects an LLM
    passing a node IP as the hostname tool parameter)."""
    parts = value.split(".")
    return len(parts) == 4 and all(
        p.isdigit() and 0 <= int(p) <= 255 for p in parts
    )

# ── Phase tool ALLOWLIST (design document §7, v2.9.8) ──
# Tools are gated by a POSITIVE per-phase whitelist (default-deny): a tool
# not in the phase's business whitelist — and not in the global read-only
# scaffolding set — is rejected.  Previously this was a DENYLIST
# (_PHASE_TOOL_GATE) with a missing "evaluate" key (no phase gate at all)
# and every new tool defaulted to allowed in all phases; v2.9.8 aligns with
# §7 "whitelist filter" and default-deny security (see §11).
_READONLY_SCOPES: frozenset[str] = frozenset({
    "read_file", "ls", "grep", "glob", "write_todos",
})

_PHASE_ALLOWLIST: dict[str, frozenset[str]] = {
    # UNDERSTAND collects data only — proposing hypotheses before the
    # coverage gate opens would skip HYPOTHESIZE entirely.
    "understand": frozenset({"task"}),
    # HYPOTHESIZE only proposes; no findings/paths/delegations yet
    # (§7 whitelist: propose_hypotheses + read_file).  Letting task()
    # through here would pull VERIFY behaviour forward and expose the
    # G5 delegation-value gate outside its designed phase.
    "hypothesize": frozenset({"propose_hypotheses"}),
    # VERIFY delegates + records findings; no proposes/paths.
    "verify": frozenset({"task", "record_finding"}),
    # EVALUATE picks direction; recording a finding is ALSO legal here
    # (v2.8.1: record_finding is an evidence-ledgering EVENT, not a phase
    # milestone — Statecharts shared-event semantics, valid in VERIFY as
    # the T3 transition and in EVALUATE as an internal self-transition).
    # Semantic guards stay in the tool handler (Defect D anti-flip, refuted
    # frozen, ID validation), so allowing it here opens no abuse surface.
    # task (auto-heal T5') is also legal here (§7).
    # write_file is deliberately NOT in the allowlist — the dedicated
    # write_file gate (check_exit_conditions / _forced_terminal / report)
    # is the correct guard for "may I write the report now"; gating it by
    # phase would kill the legitimate escape hatch where exit conditions
    # are met while the derived phase still reads "evaluate".
    "evaluate": frozenset({"select_path", "propose_hypotheses", "backtrack",
                           "record_finding", "task"}),
    # REPORT only writes the report; diagnosis tools are locked out.
    "report": frozenset(),
}

_PHASE_ALLOWED_HINT: dict[str, str] = {
    "understand": "task(*-argus-expert), read_file",
    "hypothesize": "propose_hypotheses",
    "verify": "task(*-expert), record_finding",
    "evaluate": "select_path / propose_hypotheses / backtrack / record_finding / task",
    "report": "write_file",
}


def _gate_hint(phase: str) -> str:
    return _PHASE_ALLOWED_HINT.get(phase, phase)

# Tools that are scaffolding (not diagnostic). "task" is included because
# expert delegation results are recorded by the Coordinator via record_finding.
_SCAFFOLDING_TOOLS = frozenset({
    "write_file", "read_file", "edit_file",
    "write_todos", "read_todos",
    "ls", "glob", "grep",
})

# Ledger management tools (handled by this middleware)
_LEDGER_TOOLS = frozenset({
    "propose_hypotheses", "select_path", "record_finding", "backtrack",
})

# ── Safety mechanism thresholds ──
_MAX_ROUNDS = 40                    # G1 轮次上限（简单场景，design document §8）：超过此轮次强制 REPORT
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
_STAGNATION_THRESHOLD = 3           # G3 停滞阈值（design document §8）：连续 task() 无 record_finding 判定为 VERIFY 委派无结论
_MIN_ROUND_FOR_SAFETY = 3           # Don't activate safety checks before this round
_VERIFY_STUCK_GAP = 3               # Rounds without verdict/evidence progress → verify-stuck
_VERIFY_STUCK_COOLDOWN = 3          # Min rounds between two verify-stuck interventions
# ── F2 bounded-block degrade (v3.12.0, design document §8 G17-E2
# context, 2026-08-12 scenario 37) ──
# Max consecutive G17-E2 confirmed blocks before the guard degrades to
# pass-with-disclosure.  Prevents a record_finding→G17-E2 deadlock when
# the model keeps retrying the verdict instead of delegating a deep
# expert (the F1 conflict override re-opens that delegation, so this
# limit is only reached on refusal).  Symmetric with C2 — both conflict
# guards share the same bound.
_E2_BLOCK_LIMIT = 4
_MODEL_CALL_TIMEOUT = int(os.getenv("DIAGNOSTICS_MODEL_TIMEOUT", "300"))  # seconds per LLM call attempt
_REPORT_PHASE_TIMEOUT = int(os.getenv("DIAGNOSTICS_REPORT_TIMEOUT", "600"))  # report phase generates a long report — longer timeout per attempt

# ── G7' model-call retry (design document §8) ──
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


def _parse_expert_json(text: str) -> dict | None:
    """Parse a structured expert return (deepagents response_format JSON).

    Returns the parsed dict, or None when *text* is not a structured
    JSON object (degraded text-format path).  Guards against non-dict
    JSON (lists/strings) and partial/truncated payloads.
    """
    if not text:
        return None
    stripped = text.strip()
    if not (stripped.startswith("{") and stripped.endswith("}")):
        return None
    try:
        data = json.loads(stripped)
    except (json.JSONDecodeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _expert_verdict_from_structured(data: dict) -> str | None:
    """Read the verdict field of a structured deep-expert return.

    Returns 'confirmed'/'refuted' only — 'inconclusive' normalizes to
    None, matching the text-path semantics of ``_infer_verdict_from_text``
    (no decisive signal → caller decides inconclusive).
    """
    verdict = data.get("verdict")
    if verdict in ("confirmed", "refuted"):
        return verdict
    return None


def _format_expert_summary(output: str) -> str:
    """Format a structured expert return (JSON) as readable evidence text.

    Falls back to the raw output when it is not a structured JSON object
    (degraded text-format path).  The formatted text keeps the field
    labels used by the text-signal regexes (``假设验证:`` / clarification
    marker) so downstream consumers stay compatible.
    """
    data = _parse_expert_json(output)
    if data is None:
        return output or ""
    lines: list[str] = []

    # Argus experts: clarification marker leads (contract §8 G2 / §9).
    clarification = data.get("clarification") or ""
    if clarification:
        lines.append(clarification)

    verdict = data.get("verdict")
    if verdict:
        lines.append(f"假设验证: {verdict}")
    for label, key in (
        ("突变时间点", "mutation_points"),
        ("异常排序", "anomaly_ranking"),
        ("关键证据", "key_evidence"),
        ("负证据", "negative_evidence"),
        ("并发异常", "concurrent_anomalies"),
        ("跨域关联", "cross_domain"),
    ):
        items = data.get(key) or []
        if items:
            lines.append(f"{label}: " + "; ".join(str(i) for i in items))
    for label, key in (
        ("根因判断", "root_cause"),
        ("初步判断", "preliminary_judgment"),
        ("置信度", "confidence"),
    ):
        value = data.get(key) or ""
        if value:
            lines.append(f"{label}: {value}")
    return "\n".join(lines)


def _infer_verdict_from_text(text: str) -> str | None:
    """Analyze expert output text for explicit verdict signals.

    Returns 'confirmed', 'refuted', or None if no clear signal found.
    Used by verify-stall detection to make reliable auto-decisions
    instead of blindly defaulting to inconclusive.

    Structured returns (deepagents response_format JSON) are checked
    first via the ``verdict`` field; the text-signal regexes are the
    degraded path for text-format expert returns.
    """
    if not text:
        return None
    # Structured JSON first — the verdict field is authoritative.
    data = _parse_expert_json(text)
    if data is not None:
        verdict = _expert_verdict_from_structured(data)
        if verdict is not None:
            return verdict
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
# and trigger early REPORT instead of forcing propose_hypotheses.

# Argus outcome classification patterns (design document §8 G2, v2.5).
# Three semantically distinct outcomes must NOT be conflated:
# v3.11.1: param-missing 模式表收敛至 ledger 模块 SSOT（含契约化澄清
# 标记 ARGUS_CLARIFICATION_MARKER 与回退旧措辞），此处仅保留别名以
# 保持 _classify_argus_data 函数体不变。
_ARGUS_PARAM_MISSING_PATTERNS = ARGUS_PARAM_MISSING_PATTERNS
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
        # Structured argus returns (deepagents response_format JSON):
        # classification fields take precedence over text patterns.
        data = _parse_expert_json(kf)
        if data is not None:
            clarification = data.get("clarification") or ""
            if any(p in clarification for p in _ARGUS_PARAM_MISSING_PATTERNS):
                saw_param_missing = True
                continue
            # Structured data present (mutation points / ranking /
            # negative evidence / judgment) → real diagnostic data.
            if (data.get("mutation_points") or data.get("anomaly_ranking")
                    or data.get("negative_evidence")
                    or data.get("preliminary_judgment")
                    or data.get("concurrent_anomalies")
                    or data.get("cross_domain")):
                # All-normal structured return (ranking all ✅, no
                # mutations) still counts as real data, but surfaces the
                # all_normal classification for observability.
                _mutations = data.get("mutation_points") or []
                _ranking = data.get("anomaly_ranking") or []
                if not _mutations and _ranking and all(
                        "✅" in str(i) for i in _ranking):
                    saw_all_normal = True
                else:
                    return ""  # at least one expert returned real data
                continue
            # Empty structured object — no signal; fall through to text.
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


# ── Argus coverage intervention helpers (design document §9, v2.9.7) ──
# All argus-coverage interventions (G2 coverage valve, re-delegation,
# stall injections, interception messages) share ONE scene-aware source of
# truth: _required_argus (Serverless four-view / host single / dedicated
# dual).  Previously each injection point hardcoded the host/k8s dual
# expert pair and mis-fired on Serverless sessions (forcibly injecting the
# forbidden k8s-argus-expert) — see design document §11 (2026-08-05).

def _argus_coverage_gap(ledger: dict) -> list[str]:
    """Current scene's missing argus experts (sorted; SSOT _required_argus)."""
    required = _required_argus(ledger)
    delegated: set[str] = set()
    for r in ledger.get("rounds", []):
        delegated.update(r.get("delegated_experts", []))
    return sorted(required - delegated)


def _argus_delegation_desc(expert: str, entity: str, time_hint: str) -> str:
    """Scene-aware delegation description for an argus expert."""
    if expert == "k8s-argus-expert":
        return (
            f"查询并分析 {entity} 集群 Argus K8s 指标时序"
            f"（query_argus_k8s_cluster: API延迟/etcd/DNS/节点就绪，"
            f"query_argus_k8s_node/workload/pod: 节点/工作负载/Pod 维度，"
            f"query_argus_k8s_etcd: etcd 指标）。{time_hint}。"
            if entity else
            "查询并分析 K8s 集群 Argus 指标时序"
            "（集群概览、节点状态、工作负载、Pod 重启、etcd）"
        )
    if expert == "serverless-argus-expert":
        return (
            f"查询并分析 {entity} 逻辑集群 Argus 指标时序"
            f"（API延迟/Deployment状态更新/Pod事件）。{time_hint}。"
            if entity else
            "查询并分析逻辑集群 Argus 指标时序"
            "（API 延迟、状态更新、Pod 事件）"
        )
    if expert == "kmc-argus-expert":
        return (
            f"查询并分析 {entity} KMC 控制面 Argus 指标时序"
            f"（apiserver CPU/API延迟/连接数/队列）。{time_hint}。"
            if entity else
            "查询并分析 KMC 控制面 Argus 指标时序"
            "（apiserver CPU、API 延迟）"
        )
    if expert == "sci-argus-expert":
        return (
            f"查询并分析 {entity} SCI 数据面 Argus 指标时序"
            f"（节点/Pod/CNI/驱逐）。{time_hint}。"
            if entity else
            "查询并分析 SCI 数据面 Argus 指标时序"
            "（节点、Pod、CNI、驱逐）"
        )
    # host-argus-expert (default)
    return (
        f"查询并分析主机 {entity} 的 Argus 指标时序"
        f"（CPU/内存/磁盘/网络 1min 粒度）。{time_hint}。"
        if entity else
        "查询并分析主机 Argus 监控指标时序（CPU、内存、磁盘 IO、网络）"
    )


def _build_argus_delegation_calls(
    experts: list[str], entity: str, time_hint: str
) -> list[dict]:
    """Synthetic task() calls for the given argus experts (scene-aware)."""
    import uuid as _uuid
    synthetic: list[dict] = []
    for expert in experts:
        synthetic.append({
            "name": "task",
            "id": f"call_safety_{_uuid.uuid4().hex[:12]}",
            "args": {
                "subagent_type": expert,
                "description": _argus_delegation_desc(expert, entity, time_hint),
            },
        })
    return synthetic


def _render_argus_delegation_hint(ledger: dict, missing: list[str]) -> str:
    """Force-delegation warning listing the current scene's missing experts."""
    names = "、".join(missing)
    first = missing[0]
    return (
        f"⚠ [系统强制] 当前场景已采集 Argus 数据但缺少 Argus 专家（{names}）。\n"
        "禁止在本轮调用 propose_hypotheses。\n"
        f"你必须先调用 task(subagent_type=\"{first}\", description=...) "
        f"委派 {first} 采集对应指标。\n"
        "收到分析摘要后再提出假设。"
    )


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
              "propose_hypotheses", "select_path", "record_finding", "backtrack",
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
        lines.append("（未提出假设 — 诊断在数据采集阶段被中断）")
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
    it.  Guardrails use this single predicate (design document §8)
    instead of re-implementing the check per phase.
    """
    result_list = (response.result
                   if isinstance(response.result, list)
                   else [response.result])
    return not any(
        getattr(m, "tool_calls", None)
        for m in result_list
        if hasattr(m, "tool_calls")
    )


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
    themselves (design document §8 工程约束).
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


def _build_stable_ctx(ledger: dict, subagent_type: str = "") -> str:
    """Stable context layers — unchanged within a session (design §5.2/§7.3).

    Layers: session baseline, entity identifiers, Serverless topology anchor
    + per-role view JSON, frontend param_overrides.  Cached per subagent by
    the caller (diagnosis middleware) since the topology is a read-only
    session fact; only the dynamic layers (evidence) are recomputed.
    """
    lines: list[str] = []

    # ── Layer 1: Session baseline (all subagents) ──
    # NOTE: the raw user message is kept for symptom context only —
    # parameter values in it are superseded by the pre-resolved layers, so the
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
    # gpu-expert removed in v3.9.0 — GPU merged into the unified host-expert.
    _host_experts = ("host-argus-expert", "host-expert")
    _k8s_experts = ("k8s-argus-expert", "k8s-expert")

    entity_hints: list[str] = []
    if subagent_type in _host_experts and hostname:
        entity_hints.append(f"  - hostname: **{hostname}**")
    if subagent_type in _k8s_experts and entity_name and entity_type == "kubernetes":
        entity_hints.append(f"  - cluster_name: **{entity_name}**")
        entity_hints.append("  - 实体类型: kubernetes")
    # Argus monitor_name anchor — queried from user input BEFORE agent
    # creation and stored in param_overrides (dedicated/host scenes);
    # Serverless scenes get it from topology mapping instead (§2b below).
    _monitor_name = (ledger.get("param_overrides") or {}).get("monitor_name", "")
    if _monitor_name and subagent_type in (*_host_experts, *_k8s_experts):
        entity_hints.append(f"  - monitor_name: **{_monitor_name}**")

    if entity_hints:
        lines.append(
            "## 系统预解析的诊断实体"
            "（工具参数必须使用以下值；若 Coordinator 描述或用户输入中的"
            "实体名/时间窗口与此处不一致，一律以此处为准）:\n"
            + "\n".join(entity_hints) + "\n"
        )

    # ── Layer 2b: Serverless topology (design document §5.2/§7.3) ──
    # Per-role context (Orchestrator pattern — each sub-agent receives only the
    # topology slice it needs):
    #   argus experts → compact+落点 view (namespace/workload/pod/node 参数来源)
    #   deep experts  → full view JSON (Pod details kept) + cluster_name +
    #                   naming rules — the view is the authoritative structure
    #   host experts  → host_views (physical nodes aggregated, with roles)
    # Injected only when topology present.
    _topology = ledger.get("topology") or {}
    _mapping = _topology.get("mapping", {})
    _sls_argus = ("serverless-argus-expert", "kmc-argus-expert", "sci-argus-expert")
    _sls_deep = ("serverless-expert", "kmc-expert", "sci-expert")
    _view_key = {
        "serverless-expert": "serverless_views",
        "kmc-expert": "kmc_views",
        "sci-expert": "sci_views",
    }
    _argus_view_key = {
        "serverless-argus-expert": "serverless_views",
        "kmc-argus-expert": "kmc_views",
        "sci-argus-expert": "sci_views",
    }
    _monitor_key = {
        "serverless-argus-expert": "serverless_monitor_name",
        "kmc-argus-expert": "kmc_monitor_name",
        "sci-argus-expert": "sci_monitor_name",
    }
    _cluster_key = {
        "serverless-expert": "serverless_cluster",
        "kmc-expert": "kmc_cluster",
        "sci-expert": "sci_cluster",
    }
    _anchor: list[str] = []
    if _mapping:
        if subagent_type in _sls_argus:
            _anchor.append(f"  - monitor_name: **{_mapping.get(_monitor_key[subagent_type], '')}**")
        if subagent_type in _sls_deep:
            _anchor.append(f"  - cluster_name: **{_mapping.get(_cluster_key[subagent_type], '')}**")
        # host experts: physical-node dimension — inject host_views so
        # host-argus can query host-level metrics by hostname and host-expert
        # can investigate node pressure (design document §8.1⑥ / §7.3).
        if subagent_type in _host_experts:
            _host_views = build_host_views(_topology)
            if _host_views:
                if subagent_type == "host-argus-expert":
                    # hostname 参数契约是主机名（监控端点语义，design document §8.1⑥）。
                    # 只注入主机名清单——双值并置（host_name@node_name）会让模型自行
                    # 选字段而误填节点 IP（2026-08-06 观测：8 次 query_argus_* 的
                    # hostname 全为 10.30.x 节点 IP）。此清单是 query_argus_* 的
                    # hostname 参数唯一取值来源，无噪音。
                    _hostnames = [r["host_name"] or r["node_name"] for r in _host_views]
                    _anchor.append(
                        "  - 可查询主机名（query_argus_* 的 hostname 参数仅取自此列表，"
                        "值为主机名，禁止使用节点 IP）: "
                        + ", ".join(_hostnames)
                    )
                else:
                    # 深度主机专家（host-expert / gpu-expert）可能需要 IP 作网络定位
                    # 参考：host_name / node_name 分键，注明各自用途，避免模型混淆。
                    import json as _json
                    _nodes = "; ".join(
                        _json.dumps(
                            {k: r.get(k) for k in ("host_name", "node_name", "cluster", "roles")},
                            ensure_ascii=False, separators=(",", ":"),
                        )
                        for r in _host_views
                    )
                    _anchor.append(
                        "  - 物理节点（hostname 参数用 host_name；node_name 仅网络定位参考）: "
                        + _nodes
                    )
        _k8s_n = _mapping.get("k8s_name", "")
        _mid = _mapping.get("master_id", "")
        if _k8s_n and _mid:
            _anchor.append(
                f"  - 命名规则: 控制面 ns=`{_k8s_n}-{_mid}`；工作负载 ns=`burst-ns-{_mid}`；"
                "SCI Pod=`burst-<serverless_ns>-<serverless_pod_name>`"
            )
    if _anchor:
        lines.append(
            "## Serverless 拓扑锚点"
            "（工具参数必须使用以下值；命名规则用于解析 SCI Pod/命名空间）:\n"
            + "\n".join(_anchor) + "\n"
        )

    # Deep experts: full-view JSON injection (design document §7.3) — the
    # expert resolves tool parameters (physical cluster / Pod / node) directly
    # from its own view; the view keeps Pod-level details.
    if subagent_type in _sls_deep:
        _view_name = _view_key[subagent_type]
        if _topology.get(_view_name):
            lines.append(
                render_json_block(
                    f"Serverless 拓扑视图（{_view_name}）",
                    expert_view(_topology, _view_name),
                )
            )

    # Argus experts: compact+落点 view injection (§5A.5/5A.6/5A.7) — the argus
    # expert resolves namespace/workload/pod/node tool parameters directly from
    # the slice; shared etcd comes as an etcd_views block for serverless-argus.
    if subagent_type in _sls_argus:
        _aview_name = _argus_view_key[subagent_type]
        if _topology.get(_aview_name):
            lines.append(
                render_json_block(
                    f"Serverless 拓扑视图（{_aview_name}）",
                    build_argus_compact_view(_topology, _aview_name),
                )
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

    return "\n".join(lines)


def _build_dynamic_ctx(ledger: dict) -> str:
    """Dynamic context layers — evolve as the diagnosis progresses.

    Tools already called, per-hypothesis evidence, expert analysis results.
    Recomputed on every delegation.
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

    return "\n".join(lines)


def _build_subagent_context(ledger: dict, subagent_type: str = "",
                            max_chars: int = 24000,
                            stable_cache: dict | None = None) -> str:
    """Assemble the subagent context for task() delegation.

    Stable layers (session baseline, topology anchor/view, param overrides)
    are cached per subagent type via *stable_cache* (the diagnosis middleware
    instance — session-scoped) since the topology is a read-only session fact;
    dynamic layers (evidence) are recomputed on every delegation.
    """
    stable = _build_stable_ctx(ledger, subagent_type=subagent_type)
    if stable_cache is not None and subagent_type:
        cached = stable_cache.get(subagent_type)
        if cached is None:
            stable_cache[subagent_type] = stable
        else:
            stable = cached
    dynamic = _build_dynamic_ctx(ledger)

    parts: list[str] = []
    if stable:
        parts.append(stable)
    if dynamic:
        parts.append(dynamic)
    context = "\n".join(parts)
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
                     "_root_propose_count", "_propose_count"}
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
    # statement 必填——实证：模型曾漏填第 2 个假设的 statement 触发
    # Field required 错误浪费轮次（场景25 换批提出）。
    statement: str = Field(
        description="假设陈述（必填，每个假设都必须提供，勿遗漏；"
                    "简洁陈述因果链，如『X 导致 Y』）",
    )
    probability: int = Field(description="可能性 0-100", ge=0, le=100)
    rationale: str = Field(default="", description="假设依据")


class DeprioritizedSpec(BaseModel):
    id: str = Field(description="被降级的假设ID")
    reason: str = Field(description="降级原因")


class ProposeHypothesesInput(BaseModel):
    hypotheses: list[HypothesisSpec] = Field(
        description="假设列表（最多3个，按概率降序）",
    )
    focus_summary: str = Field(
        default="",
        description="本轮假设形成的依据摘要",
    )


class SelectPathInput(BaseModel):
    # 字段名固定为 selected_hypothesis_id（勿用 path/name 等别名）——
    # 实证：模型曾只传 {"path": "H2"} 触发 Field required 错误浪费轮次。
    selected_hypothesis_id: str = Field(
        description="选择的假设ID（必填；字段名固定为 selected_hypothesis_id，"
                    "不要用 path/name 等别名）",
    )
    rationale: str = Field(description="选择理由（必填）")
    deprioritized: list[DeprioritizedSpec] = Field(
        default_factory=list,
        description="未选中假设的降级原因",
    )


class RecordFindingInput(BaseModel):
    hypothesis_id: str = Field(description="验证的假设ID")
    verdict: str | None = Field(
        default=None,
        description="验证结论，只能取三者之一: confirmed(已证实) | "
                    "refuted(已排除) | inconclusive(证据不足)。"
                    "禁止使用变体（如 partially confirmed）；"
                    "若仅部分成立，用 confirmed + statement_update 修正表述。"
                    "若仅需修正其他未决假设的表述而不落结论"
                    "（迭代假设更新），留空并提供 statement_update。",
        pattern="^(confirmed|refuted|inconclusive)$",
    )
    evidence_summary: str = Field(default="", description="验证证据摘要")
    probability_update: int | None = Field(
        default=None,
        validation_alias=AliasChoices("probability_update", "probability"),
        description="更新后的概率 0-100；纯表述修正（verdict 留空）时忽略",
        ge=0, le=100,
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
        is_subagent: bool = False,
        entity_type: str = "",
        hostname: str = "",
        entity_name: str = "",
        start_time: str = "",
        end_time: str = "",
        param_overrides: dict | None = None,
        topology: dict | None = None,
        topology_unavailable: bool = False,
        valid_subagents: list[str] | None = None,
    ) -> None:
        self.ledger_path = ledger_path
        self.report_path = report_path
        self._model = model
        self._backend = backend
        self._is_subagent = is_subagent
        self._topology = topology or {}
        self._topology_unavailable = topology_unavailable
        self._entity_type = entity_type
        self._hostname = hostname
        self._entity_name = entity_name
        self._start_time = start_time
        self._end_time = end_time
        self._param_overrides = param_overrides or {}
        # Valid subagent names for task() validation (Coordinator only).
        # None/empty disables the check (tests, subagent instances).
        self._valid_subagents = tuple(valid_subagents or ())
        # Stable subagent-context cache (session-scoped): topology is a
        # read-only session fact, so the stable layers are built once per
        # subagent type and reused across delegations (design §7.3).
        self._stable_ctx_cache: dict[str, str] = {}
        # Agent memory (AGENTS.md), loaded once and injected before the
        # ledger block so the current diagnosis state stays at the
        # prompt tail.  Subagents skip injection.
        self._agent_memory_block = (
            "" if is_subagent else _load_agent_memory(self._AGENT_DATA_REAL)
        )
        self._current_ledger: DiagnosisLedger | None = None
        self._model_call_count: int = 0
        # Stall guidance pending transfer: set by post-response stall
        # detection (awrap_model_call) when it wants to force the loop
        # onward; consumed by after_model, which moves it into the
        # `_stall_guidance` state field and issues jump_to="model".
        self._pending_guidance: str | None = None
        # Safety mechanism tracking
        self._consecutive_task_count: int = 0  # global: stagnation detection
        self._same_delegate_count: int = 0     # per-key: delegation saturation
        self._last_delegate_key: str = ""      # "expert:H1" etc.
        self._last_finding_round: int = 0
        self._last_evidence_round: int = 0  # round when diagnostic evidence last arrived
        self._last_task_round: int = 0
        # Single-focus batch validation (design document
        # parallel-delegation §4.5): hypothesis ID of the first task()
        # call admitted this round; a same-round task targeting a
        # different hypothesis is the parallel-group abuse vector and is
        # blocked.  Synchronous check-and-set in awrap_tool_call is
        # atomic under asyncio cooperative scheduling (no await between
        # check and set).  Reset whenever the round changes.
        self._round_focus_hid: str | None = None
        self._round_focus_round: int = -1
        # Two distinct REPORT-phase anchors (design document §8 G6,
        # 2026-08-09): ``_report_phase_start_round`` = the FIRST round
        # where the LLM actually received REPORT guidance and had a
        # write_file decision chance (set by before_model).  It must
        # never be pre-set by post-response guardrails — pre-setting it
        # to a round where the LLM had no chance to comply fires the
        # "without write_file" warning on the very first REPORT decision
        # round (observed 2026-08-09 session 59073d58: warning at round 7
        # "started round 6" where round 6 was a silent VERIFY stall).
        # ``_report_forced_enter_round`` = the round where a guardrail
        # FORCED entry into REPORT (verify/evaluate stall, G7, max-rounds
        # post-response paths); consumed only by the post-response
        # REPORT-stall level computation, never by the before_model
        # warning.
        self._report_phase_start_round: int = 0
        self._report_forced_enter_round: int = 0
        self._verify_stall_count: int = 0  # consecutive verify-phase stalls
        self._verify_stuck_last_fired: int = -100  # cooldown for verify-stuck valve
        self._evaluate_stall_count: int = 0  # consecutive evaluate-phase stalls
        # (v3.6.0: E2' block counter removed — the gate now releases with
        # disclosure instead of escalating blocks, design document §6.3)
        # v3.8.0: generic exit-gate block counter — consecutive write_file
        # blocks with S1 unsatisfied (no confirmed root cause); the 3rd
        # block triggers the deterministic honest-negative exit (design
        # document §6.3).
        self._exit_block_count: int = 0
        self._det_exit_grace_used: bool = False  # v3.10.1: one-shot R3 grace
        self._round_emitted: bool = False  # prevent duplicate round_transition on goto re-entry
        self._last_propose_round: int = -1  # round when propose_hypotheses last ran
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
        # Filesystem path prefix for offloaded tool results.
        self._tool_results_prefix = "/tool_results"
        # Ledger management tools — only needed by Coordinator; subagents
        # share the same ledger object but should not expose management tools.
        self.tools: list = (
            []
            if is_subagent
            else [
                self._make_propose_hypotheses_tool(),
                self._make_select_path_tool(),
                self._make_record_finding_tool(),
                self._make_backtrack_tool(),
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
            # Share the session's read-only topology so the L4 hostname
            # normalization gateway (design document §8.1⑥) can resolve
            # node IPs → host names inside subagents.  Without this, a
            # subagent's _topology stays empty and `_resolve_host_name`
            # falls back to an empty map (IPs are not normalized; they
            # hit production Argus as wrong endpoints).
            topology=coordinator._topology,
        )
        instance._current_ledger = coordinator._current_ledger
        # Inherit the Coordinator's round count so any future code that
        # references _model_call_count on the subagent instance starts
        # from the correct baseline.  before_model() is a no-op for
        # subagents, so this value won't be mutated.
        instance._model_call_count = coordinator._model_call_count
        return instance

    def _resolve_host_name(self, value: str) -> str:
        """Resolve a node IP (node_name) to its physical host name (host_name)
        using the in-session topology's host views.

        Also returns the value itself when it is already a known host name
        (idempotent, harmless).  Returns "" for unknown values.  Backed by
        the topology (mock and future production alike), NOT by any mock
        whitelist — the scope guard middleware was removed (README).
        (design document §8.1⑥)
        """
        if not value:
            return ""
        mapping = getattr(self, "_node_ip_to_host", None)
        if mapping is None:
            mapping = {}
            _topo = (self._current_ledger or {}).get("topology") or self._topology
            for _rec in build_host_views(_topo):
                _hn = (_rec.get("host_name") or "").strip()
                _nn = (_rec.get("node_name") or "").strip()
                if _hn:
                    mapping.setdefault(_hn, _hn)
                if _hn and _nn and _nn != _hn:
                    mapping[_nn] = _hn
            self._node_ip_to_host = mapping
        return mapping.get(value, "")

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
        # Assembled expert directory snapshot (design document
        # parallel-delegation §5 D1): the POST-FILTER subagent list is
        # the ground truth of what task() can dispatch to — the
        # complementary-expert computation must never suggest an
        # unassembled expert.
        ledger["scene_experts"] = list(self._valid_subagents)
        # Serverless scene: topology (RelationGraph) injected at session init
        # and availability flag — consumed by the coverage gate (D1/D2) and
        # subagent context anchors (design document §5.2/§7.2).
        ledger["topology"] = dict(self._topology)
        ledger["_topology_unavailable"] = self._topology_unavailable
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

    # ── Stall recovery: after_model issues jump_to="model" ──
    # The post-response stall valves (awrap_model_call) write their guidance
    # into self._pending_guidance instead of injecting a synthetic ls tool
    # call.  This hook moves that guidance into the `_stall_guidance` state
    # field and issues jump_to="model", which routes the loop back to the
    # model node (skipping the tools node) so the model re-decides with the
    # guidance injected into its system message next round.
    #
    # jump_to must come from after_model (NOT awrap_model_call): the
    # model→tools edge only includes the loop-entry destination when an
    # after_model node exists; without one, jump_to="model" routes to a
    # non-existent node (verified by a controlled test).
    @hook_config(can_jump_to=["model"])
    def after_model(self, state, runtime) -> dict | None:
        if self._pending_guidance:
            guidance = self._pending_guidance
            self._pending_guidance = None
            return {"jump_to": "model", "_stall_guidance": guidance}
        return None

    # ── Context injection ──

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelResponse | ExtendedModelResponse:
        # Subagent: skip ledger context injection and safety warnings.
        # The Coordinator already passes relevant context via task() description.
        if self._is_subagent:
            _resp = await handler(request)
            # ── Truncation observability (design document §8 G13 context,
            # 2026-08-11 scenario 38) ──
            # reasoning-stream truncation (finish_reason=length) silently
            # drops the subagent's structured return (content=0B → the
            # decisive evidence never reaches the Coordinator).  Warn so
            # the post-hoc analysis can distinguish "expert found nothing"
            # from "expert output was cut off".
            try:
                _rlist = (
                    _resp.result
                    if isinstance(_resp.result, list)
                    else [_resp.result]
                )
                for _m in _rlist:
                    _rm = getattr(_m, "response_metadata", None) or {}
                    if _rm.get("finish_reason") == "length":
                        logger.warning(
                            "Subagent model output truncated "
                            "(finish_reason=length, round %d) — structured "
                            "return may be incomplete; check delegation",
                            self._model_call_count,
                        )
                        break
            except Exception:
                pass  # observability only — never crash the subagent
            return _resp

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
        # fresh root-hypothesis batch may be proposed.
        if (_phase(ledger) == "report"
                or derive_phase(ledger) == "report"):
            legit_report = bool(
                check_exit_conditions(ledger)[0]
                or ledger.get("_forced_terminal")
                or ledger.get("report")
            )
            if legit_report and finalize_pending_for_report(ledger):
                # Natural exit (check_exit_conditions / report written):
                # do NOT set _forced_terminal — that marker is reserved
                # for guardrail-forced entries (design document §3 step
                # 1).  check_exit_conditions stays true after finalize
                # (confirmed/refuted are terminal), so derive_phase keeps
                # the session pinned to REPORT without the lock.  Record
                # the path for post-hoc analysis instead.
                if not ledger.get("_forced_terminal"):
                    ledger["_exit_path"] = "natural"
                self._persist_ledger(ledger)
                logger.info(
                    "Phase guard: finalized pending hypotheses for REPORT "
                    "(round %d, exit_path=%s)",
                    self._model_call_count,
                    ledger.get("_exit_path", "unknown"),
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

        # ── Stall guidance: consume the transient _stall_guidance set by
        # after_model on the previous round.  Injected into the SYSTEM message
        # (not the conversation) so the model sees a system directive instead
        # of a self-authored assistant turn.  Cleared in the normal return
        # path below (the same Command that persists the ledger).
        _stall_guidance = request.state.get("_stall_guidance") if request.state else None
        if _stall_guidance:
            combined_block = f"{combined_block}\n\n{_stall_guidance}" if combined_block else _stall_guidance

        # ── Observability: emit the injected guidance delta to the trace ──
        # The trace records tool calls/results and LLM output but not the
        # prompt side; without the stimulus, guardrail behavior (e.g.
        # whether the exit banner was actually shown) is unverifiable from
        # the trace alone.  Emit ONLY the per-round delta — phase guidance,
        # safety-valve warnings, evidence audit.  The ledger data body is
        # already captured by ledger snapshots; AGENTS.md is static.
        # Best-effort: observability must never become a failure source
        # (design document §8).
        try:
            _delta_parts: list[str] = []
            _guidance = _phase_guidance(
                derive_phase(ledger), ledger, self.report_path)
            if _guidance:
                _delta_parts.append("## 本步要求\n" + _guidance)
            if safety_block:
                _delta_parts.append("## 安全阀警告\n" + safety_block)
            if audit_block:
                _delta_parts.append("## 证据审计\n" + audit_block)
            if _delta_parts:
                _sw = getattr(
                    getattr(request, "runtime", None), "stream_writer", None)
                if _sw:
                    _sw({
                        "type": "prompt_injection",
                        "round": self._model_call_count,
                        "content": "\n\n".join(_delta_parts),
                    })
        except Exception:
            pass

        # ── Timeout protection: prevent single LLM call from hanging forever ──
        # REPORT phase generates a long markdown report — use a longer timeout.
        _call_request = request.override(system_message=append_to_system_message(
            request.system_message, combined_block)) if combined_block else request
        _effective_timeout = (
            _REPORT_PHASE_TIMEOUT
            if _phase(ledger) == "report"
            else _MODEL_CALL_TIMEOUT
        )
        # ── G7' transient-failure retry (design document §8) ──
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
                # G7 post-response forced entry: record the SYSTEM entry
                # round for level computation only; the before_model
                # warning anchor is set by the first REPORT before_model.
                self._report_forced_enter_round = self._model_call_count

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

        # ── G8 首轮文本模拟（design document §8）：round-1 text simulation ──
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
                    "🔧提出诊断假设",
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
                            # Branch A forced REPORT — post-response:
                            # record system-entry round only (level
                            # computation); warning anchor set by the
                            # next before_model.
                            self._report_forced_enter_round = self._model_call_count
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

                            # Extract expert output text for signal analysis.
                            # Structured returns (deepagents response_format
                            # JSON) are preferred — the verdict field is
                            # authoritative and survives formatting.
                            expert_text = ""
                            for ev in node.get("evidence", []):
                                src = ev.get("source", "")
                                if isinstance(src, str) and src.startswith("expert:"):
                                    _struct = ev.get("structured")
                                    if isinstance(_struct, dict):
                                        _v = _expert_verdict_from_structured(_struct)
                                        if _v is not None:
                                            expert_text = _v
                                            break
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
                                self._pending_guidance = (
                                    "你已收到专家验证结果但未调用 "
                                    "record_finding 记录结论。\n"
                                    f"请立即对假设 {fmt_hid(active_id)} "
                                    "调用 record_finding（confirmed / "
                                    "refuted / inconclusive 由你根据证据"
                                    "判定；证据不足就给 inconclusive）。"
                                    "禁止以纯文本结束本阶段。"
                                )
                                response = ModelResponse(result=[AIMessage(
                                    content="",
                                )])
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
                                # Branch B 3rd-stall forced REPORT —
                                # post-response: system-entry round only.
                                self._report_forced_enter_round = self._model_call_count
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
                            # Branch C (no recorded evidence) forced
                            # REPORT — post-response: system-entry round
                            # only; warning anchor set by before_model.
                            self._report_forced_enter_round = self._model_call_count
                            self._persist_ledger(ledger)

        # ── Post-response: detect UNDERSTAND-phase stall ──
        # When the LLM outputs plain text without calling propose_hypotheses
        # in UNDERSTAND phase (round >= 2, data collected, no hypotheses),
        # inject a SystemMessage to force the agent loop to continue.
        # Without this, the LLM can output "🔧提出诊断假设" as text
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
                        _required = list(_required_argus(ledger))
                        synthetic = _build_argus_delegation_calls(
                            _required, _entity, _time_hint)
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
                        # normal coverage/propose flow continues (如实
                        # 记录，继续推进).
                        # ── Coverage-aware stall handling ──
                        # If Argus coverage is incomplete (e.g. only
                        # k8s-argus-expert delegated, host-argus-expert
                        # missing), injecting propose_hypotheses is
                        # guaranteed to be blocked by the phase gate
                        # (understand phase tool whitelist, §7).
                        # Instead, force delegation of the missing expert
                        # so coverage becomes complete and the normal
                        # UNDERSTAND→HYPOTHESIZE transition (T1) can fire.
                        if not _coverage_ready(ledger):
                            missing = _argus_coverage_gap(ledger)
                            if missing:
                                logger.warning(
                                    "Safety: understand stall with "
                                    "incomplete coverage (round %d, "
                                    "missing: %s) — forcing expert "
                                    "delegation instead of "
                                    "propose_hypotheses",
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
                                synthetic = _build_argus_delegation_calls(
                                    missing, _cluster, _time_hint)
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
                                # — do NOT inject a placeholder propose: it
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
                                self._pending_guidance = (
                                    "监控数据采集状态异常（专家已委派但覆盖门控"
                                    "未就绪）。请查看已有工具调用结果：若数据已"
                                    "返回，请基于数据继续诊断流程。"
                                )
                                response = ModelResponse(result=[AIMessage(
                                    content="",
                                )])
                        else:
                            # Coverage is ready (or the argus-failure
                            # degrade bypassed T1) — nudge propose via a
                            # harmless read-only call.  Do NOT inject a
                            # placeholder propose_hypotheses: a placeholder
                            # root batch consumes batch budget and pollutes
                            # the tree (same principle as the G9 evaluate
                            # valve, design document §8).
                            logger.warning(
                                "Safety: understand stall (round %d) — "
                                "LLM output text without propose_hypotheses, "
                                "nudging propose via jump_to guidance",
                                self._model_call_count,
                            )
                            self._pending_guidance = (
                                "Argus 监控平台故障（空数据/报错），监控数据"
                                "不可用。请基于用户描述的故障现象立即调用 "
                                "propose_hypotheses 提出假设，后续专家诊断将"
                                "继续验证并修正。"
                                if ledger.get("_argus_unavailable")
                                else
                                "你已连续输出纯文本而未提出假设。请立即基于"
                                "已有数据调用 propose_hypotheses 提出假设"
                                "（最多3个，按概率降序，各附依据）。"
                            )
                            response = ModelResponse(result=[AIMessage(
                                content="",
                            )])

        # ── Post-response: EVALUATE-phase loop abandonment (guardrail G9) ──
        # Trigger: the LLM ends its EVALUATE turn without tool calls
        # (plain-text "conclusion" or empty response), which would exit
        # the agent loop while derive_phase still demands a direction
        # (i.e. check_exit_conditions said NO — there is no confirmed
        # root cause, so exiting here would be the premature closure the
        # exit model explicitly forbids, design document §6.3).
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
        #         propose_hypotheses (T8; no placeholder propose — that
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
                    and delegation_value_for(ledger, hid) >= KAPPA_STOP
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
                batch_open = can_propose_root_hypotheses(ledger)

                if (self._evaluate_stall_count >= 1
                        or (not actionable and not revivable
                            and not strengthenable and not batch_open)):
                    # ── Terminal: repeated abandonment, or nothing legal
                    # left to do.  Hand over to REPORT (report generation
                    # happens in the REPORT-phase handler below).
                    finalize_pending_for_report(ledger)
                    _force_report(ledger)
                    # G9 evaluate-stall terminal forced REPORT —
                    # post-response: system-entry round only.
                    self._report_forced_enter_round = self._model_call_count
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
                                  key=lambda h: delegation_value_for(ledger, h))
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
                        self._pending_guidance = (
                            "当前处于 EVALUATE 阶段但未调用工具。\n"
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
                        # propose_hypotheses itself — a placeholder
                        # hypothesis would consume batch budget and pollute
                        # the tree.
                        self._pending_guidance = (
                            "当前处于 EVALUATE 阶段但未调用工具，"
                            "本层假设已全部证伪或验证增益已低于成本"
                            "（经济死亡，禁止再委派验证）。\n"
                            f"假设提出预算仍开放（已用 "
                            f"{ledger.get('_propose_count', 0)}"
                            f"/{MAX_PROPOSE_CALLS} 次）。请立即调用 "
                            "propose_hypotheses 提出新一批假设"
                            "（必须基于已排除证据换方向，禁止重复或仅换措辞）。"
                        )
                        logger.warning(
                            "Safety: evaluate stall (round %d, propose %d/%d "
                            "open) — nudging propose_hypotheses via "
                            "jump_to guidance",
                            self._model_call_count,
                            ledger.get("_propose_count", 0),
                            MAX_PROPOSE_CALLS,
                        )
                    if self._pending_guidance:
                        # strengthen / T8：引导已写入 self._pending_guidance
                        # （走 system 通道），response 清空以丢弃 stall 纯文本
                        response = ModelResponse(result=[AIMessage(content="")])
                    else:
                        # revive deferred：select_path 是有意义的动作，保留
                        # content + tool_calls 注入
                        response = ModelResponse(result=[AIMessage(content=content)])
                        _inject_tool_calls(response, synthetic)
            else:
                # Real tool call made — the loop advances on its own.
                self._evaluate_stall_count = 0
                # ── F3: conflict-gate loop detection (v3.12.0, design
                # document §8 G17-E2/G20 context, 2026-08-12 scenario 37)
                # ──
                # A record_finding→G17-E2/C2 deadlock in EVALUATE (the
                # model keeps retrying the verdict instead of delegating a
                # deep expert) burns rounds and can hit the wall-clock
                # timeout.  Detect it: the active hypothesis's conflict
                # block counters have grown AND no deep-expert evidence
                # arrived since the last block — inject a synthetic
                # delegation hint that re-opens the deep path (F1 already
                # exempted it from the gain<cost gate, so the delegation
                # will succeed).  Only fires while the conflict is
                # unresolved (block_count keeps growing); once a deep
                # expert returns, the conflict clears and counters stop.
                if ledger and ledger.get("hypotheses"):
                    _ap = ledger.get("active_path") or []
                    _ahid = _ap[-1] if _ap else None
                    if _ahid and _ahid in ledger["hypotheses"]:
                        _an = ledger["hypotheses"][_ahid]
                        _bc = (_an.get("_g17e2_block_count", 0)
                               + _an.get("_c2_block_count", 0))
                        _has_deep = any(
                            str(e.get("source", "")).startswith("expert:")
                            and "-argus-expert" not in str(e.get("source", ""))
                            for e in _an.get("evidence", [])
                        )
                        _last_block_round = max(
                            _an.get("_g17_blocked_round") or 0,
                            _an.get("_c2_blocked_round") or 0,
                        )
                        # At least 2 consecutive blocks, no deep evidence
                        # since — the loop is spinning on the verdict.
                        if (_bc >= 2 and not _has_deep
                                and _last_block_round
                                >= self._model_call_count - 2):
                            _deep_hint = [e for e in
                                          (ledger.get("scene_experts") or [])
                                          if not e.endswith("-argus-expert")]
                            logger.warning(
                                "F3: conflict-gate loop on %s "
                                "(g17e2_blocks=%d c2_blocks=%d, round=%d) — "
                                "injecting deep-delegation hint",
                                fmt_hid(_ahid),
                                _an.get("_g17e2_block_count", 0),
                                _an.get("_c2_block_count", 0),
                                self._model_call_count,
                            )
                            _f3_hint = (
                                f"{fmt_hid(_ahid)} 的 confirmed/refuted "
                                f"判定已连续被冲突门控拦截——指标层证据存在"
                                f"（集群/节点/工作负载级）异常与目标实体正常并存，"
                                f"系统已开放深度专家委派通道（不受成本门控限制）。"
                                f"请立即委派深度专家"
                                f"{('（' + '、'.join(_deep_hint[:3]) + '）') if _deep_hint else ''}"
                                f"查目标实体日志/事件确认后再判定；"
                                f"深度专家证据到位后冲突自动清除、门控自动放行。"
                            )
                            # Force the loop onward via jump_to (after_model) —
                            # the hint rides the system channel, and the
                            # record_finding verdict is dropped (it would be
                            # re-blocked by the conflict gate).
                            self._pending_guidance = _f3_hint
                            response = ModelResponse(result=[AIMessage(
                                content="",
                            )])

        # ── Post-response: detect HYPOTHESIZE-phase stall (G12) ──
        # HYPOTHESIZE has two entry paths (design document §4/§5): T1
        # (first batch — coverage just became ready, the ledger has NO
        # hypotheses yet) and T8 (retry batch — the current batch is all
        # hard-failed and budget remains).  In BOTH, if the LLM outputs
        # plain text instead of calling propose_hypotheses, the agent loop
        # would exit silently with no report (2026-07-31 scenario-24
        # swap_thrashing: 2 rounds, zero hypotheses, no report, no ledger
        # persisted).  Nudge via jump_to guidance — do NOT inject
        # propose_hypotheses itself: a placeholder hypothesis would consume
        # batch budget and pollute the tree (same principle as the G9
        # evaluate valve, §8).  No round gate: the phase cannot occur
        # before round 2 (s₀=UNDERSTAND, T1 coverage needs ≥2 rounds), and
        # plain text always means the loop is about to END — so a nudge is
        # always correct.  This mirrors the sibling verify-stall valve
        # (which also fires with no round gate).
        ledger = self._current_ledger
        if (ledger and _phase(ledger) == "hypothesize"
                and not self._pending_guidance):
            # The `not self._pending_guidance` guard makes the stall valves
            # explicitly mutually exclusive.  The UNDERSTAND valve may degrade
            # argus failure (understand → hypothesize) and set
            # _pending_guidance in the same response; without this guard the
            # HYPOTHESIZE valve would then also fire (its _response_ends_loop
            # sees the now-empty response) and overwrite the argus-failure
            # guidance.  (The pre-jump_to implementation relied on the
            # understand valve injecting a tool call — which made
            # _response_ends_loop False — as an *implicit* mutual exclusion;
            # the empty-response + jump_to rewrite removed that, so the
            # exclusion is now explicit.)
            if _response_ends_loop(response):
                _has_prior = bool(ledger.get("hypotheses"))
                logger.warning(
                    "Safety: hypothesize-phase stall (round %d, propose "
                    "%d/%d, %s) — nudging propose_hypotheses via "
                    "jump_to guidance",
                    self._model_call_count,
                    ledger.get("_propose_count", 0),
                    MAX_PROPOSE_CALLS,
                    "retry-batch" if _has_prior else "first-batch",
                )
                if _has_prior:
                    _directive = (
                        "请立即调用 propose_hypotheses 提出新一批假设——"
                        "必须基于已排除证据换方向推断，"
                        "禁止与已排除假设重复或仅换措辞。"
                    )
                    # v3.11.0: structured failure digest (SSOT with the
                    # HYPOTHESIZE guidance, design document §9).
                    _directive += render_failure_digest(ledger) or ""
                else:
                    _directive = (
                        "你尚未提出任何假设。请立即调用 propose_hypotheses "
                        "提出首批竞争性假设（≤3 个，按概率降序，各附依据）——"
                        "禁止以纯文本结束本阶段。"
                    )
                self._pending_guidance = (
                    "当前处于 HYPOTHESIZE 阶段"
                    f"（假设提出预算已用 "
                    f"{ledger.get('_propose_count', 0)}"
                    f"/{MAX_PROPOSE_CALLS} 次）。\n"
                    f"{_directive}"
                )
                response = ModelResponse(result=[AIMessage(
                    content="",
                )])

        # ── Post-response: detect VERIFY ledger-only stall ──
        # When the LLM uses ledger tools (select_path, propose_hypotheses)
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
                    # on hypotheses just created by propose_hypotheses
                    # (this or the previous round).  They naturally have
                    # no evidence yet and the LLM should have a chance
                    # to delegate before we intervene.
                    if (not has_expert_evidence
                            and self._model_call_count
                            - self._last_propose_round > 1):
                        ai_msgs = [
                            m for m in (response.result if isinstance(response.result, list) else [response.result])
                            if hasattr(m, "tool_calls")
                        ]
                        if not _response_ends_loop(response):
                            # Check whether ALL tool calls in this
                            # response are ledger-management tools
                            all_ledger = True
                            for m in ai_msgs:
                                for tc in (m.tool_calls or []):
                                    name = getattr(tc, "name", "") if hasattr(tc, "name") else tc.get("name", "")
                                    if name not in _LEDGER_TOOLS:
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
                                        # G14 reminder (v3.7.0): no longer
                                        # hardcodes host-expert (scene-
                                        # specific) and now distinguishes
                                        # the confirmed evidence standard
                                        # from the refuted/inconclusive
                                        # path (G17 contract).
                                        m.content = m.content.rstrip() + (
                                            "\n\n⛔ [系统提醒] 当前活跃假设 "
                                            f"{fmt_hid(active_id)} 尚无专家验证证据。"
                                            "confirmed 判定须以专家验证结论为依据"
                                            "——请委派 task 验证此假设"
                                            "（按假设涉及领域选择对应专家；"
                                            "监控时序定向复核可委派对应 argus 专家）；"
                                            "若现有监控证据已明确证伪，可直接 "
                                            "record_finding 判 refuted；"
                                            "证据不足判 inconclusive。"
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
                _salvage_text = _extract_response_text(response)
                if _is_substantial_report_text(_salvage_text) and (
                        not report_exists
                        or ledger.get("_report_auto_generated")):
                    ledger["report"] = _salvage_text
                    ledger["_report_auto_generated"] = False
                    _force_report(ledger)
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
                    # Level computation anchors on the SYSTEM entry round
                    # (guardrail-forced entry, `_report_forced_enter_round`)
                    # when present; otherwise falls back to the before_model
                    # warning anchor (natural exit path, set by the first
                    # REPORT before_model).  Both never co-occur: a forced
                    # entry sets forced_enter_round in post-response and
                    # leaves phase_start_round=0 for the next before_model
                    # to set — keeping the two semantics (system entry vs
                    # LLM first decision chance) fully separated.
                    _lvl_base = (
                        self._report_forced_enter_round
                        or self._report_phase_start_round
                    )
                    rounds_in_report = (
                        self._model_call_count - max(_lvl_base, 1)
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
                        self._pending_guidance = (
                            "REPORT 阶段禁止纯文本收尾。\n"
                            "你必须在下一轮调用 write_file 将完整诊断"
                            f"报告写入 {self.report_path}。\n"
                            "禁止调用任何诊断工具，禁止输出纯文本分析。"
                        )
                        response = ModelResponse(result=[AIMessage(
                            content="",
                        )])
                    else:
                        # ── Level 2: Auto-generate as last resort ──
                        report_md = self._generate_report_from_ledger(ledger)
                        ledger["report"] = report_md
                        _force_report(ledger)
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
                    # the tools node naturally.  Scene-aware: inject the
                    # CURRENT scene's required argus set (SSOT
                    # _required_argus) instead of a hardcoded host/k8s pair.
                    _required = list(_required_argus(ledger))
                    synthetic = _build_argus_delegation_calls(
                        _required, _cluster or _hostname, _time_hint)
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
                command=Command(update={
                    "_diagnosis_ledger": self._current_ledger,
                    # Consume the stall guidance after injecting it this round.
                    "_stall_guidance": None,
                }),
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
                    # ── E2' competing-hypothesis escalation (design document §6.3) ──
                    # When write_file is blocked by an unverified competing
                    # hypothesis (max delegation_value >= κ), the LLM often
                    # loops on record_finding/select_path instead of delegating
                    # the blocking hypothesis — a "soft loop" within the round
                    # cap.  Escalate in 3 levels to break it deterministically
                    # (best practice: flexible guidance + reliable code
                    # backstop — "靠修改 Prompt 难彻底防循环，需确定性代码约束"):
                    #   L1 actionable directive (name hypothesis + action)
                    #   L2 strong directive (forbid retrying write_file)
                    #   L3 deterministic defer — shelve the blocking hypothesis
                    #      so the exit condition is satisfied and REPORT
                    #      proceeds (same outcome as G9, just sooner & targeted).
                    _is_e2 = ("可行动验证" in reason) or ("竞争性根因" in reason)
                    if _is_e2:
                        # ── E2' release-with-disclosure (design document
                        # §6.3, v3.6.0) ──
                        # S1 satisfied (root cause confirmed) but unverified
                        # competing hypotheses remain.  Multi-root-cause
                        # protection is achieved by deterministic
                        # DISCLOSURE, not by blocking the report: blocking
                        # created a soft loop whose failure mode is a
                        # timeout with ZERO output (2026-08-08 batch: 3
                        # blocked sessions, 3 timeouts, 0 reports — the
                        # fail-closed path destroyed confirmed findings);
                        # disclosure preserves the information (fail-open)
                        # — the system shelves the remaining hypotheses and
                        # injects a disclosure section into the report, so
                        # no second-root-cause possibility is silently
                        # dropped.  Proactive guidance (exit_banner /
                        # VERIFY directive) still recommends verification
                        # first; the LLM keeps the choice.
                        from diagnostics.agent.ledger import (
                            render_unverified_disclosure as _rud,
                        )
                        _disclosure = _rud(ledger)
                        _disclosed = []
                        for _h, _n in ledger.get("hypotheses", {}).items():
                            if (_n.get("status") in ("pending", "inconclusive")
                                    and not _n.get("deferred")):
                                _n["deferred"] = True
                                _n["selected"] = False
                                _n["deferred_reason"] = (
                                    "[系统] 报告收口：根因已确认，该竞争假设"
                                    "未完成验证，已在报告中披露（design "
                                    "document §6.3 E2′ 放行+披露，v3.6.0）"
                                )
                                _disclosed.append(fmt_hid(_h))
                        if _disclosure and isinstance(
                                tool_args.get("content"), str):
                            tool_args["content"] = (
                                tool_args["content"].rstrip() + _disclosure
                            )
                        self._persist_ledger(ledger)
                        logger.warning(
                            "write_file released with disclosure (round %d): "
                            "shelved %s, disclosure injected (%d chars)",
                            self._model_call_count, _disclosed,
                            len(_disclosure),
                        )
                        # fall through — write_file proceeds (path
                        # canonicalization, execution, report capture)
                    else:
                        # Non-E2' block (S1 not satisfied — no confirmed
                        # root cause).  v3.8.0: the block message consumes
                        # the SAME SSOT renderer as the proactive banner
                        # (A2), and a deterministic exit engages after 3
                        # consecutive blocks (A3): shelve all unresolved
                        # hypotheses and release the report with the
                        # disclosure section — an honest "root cause not
                        # confirmed" report is a legitimate deliverable
                        # (the anti-fabrication floor is unaffected: G17
                        # already guarantees no fabricated confirmed
                        # verdict exists for the report to cite).
                        self._exit_block_count += 1
                        if self._exit_block_count >= 3:
                            # R3 (v3.10.1): Before auto-shelving, check if
                            # any hypothesis has unrecorded expert evidence
                            # (compute_next_action critical/high).  If so,
                            # give ONE final chance instead of shelving —
                            # the LLM owes a record_finding, not a dead end.
                            if not self._det_exit_grace_used:
                                _grace_hint = None
                                for _h, _n in ledger.get(
                                        "hypotheses", {}).items():
                                    if _n.get("status") in (
                                            "pending", "inconclusive"):
                                        _ch = compute_next_action(ledger, _h)
                                        if _ch and _ch.urgency in (
                                                "critical", "high"):
                                            _grace_hint = (_h, _ch)
                                            break
                                if _grace_hint:
                                    self._det_exit_grace_used = True
                                    _gh, _gc = _grace_hint
                                    logger.warning(
                                        "write_file deterministic exit "
                                        "deferred (round %d): unrecorded "
                                        "expert evidence on %s — injecting "
                                        "final directive (urgency=%s)",
                                        self._model_call_count,
                                        fmt_hid(_gh), _gc.urgency,
                                    )
                                    return ToolMessage(
                                        content=(
                                            f"⚠ {fmt_hid(_gh)} "
                                            f"{_gc.reason}。"
                                            f"请立即 record_finding("
                                            f"{fmt_hid(_gh)}, ...) 落账——"
                                            "这是系统在自动搁置前的最后"
                                            "一次提醒。若仍不落账，系统将"
                                            "强制搁置该假设并生成报告。"
                                        ),
                                        tool_call_id=tool_call_id,
                                    )
                            from diagnostics.agent.ledger import (
                                render_unverified_disclosure as _rud3,
                            )
                            _shelved = []
                            for _h, _n in ledger.get(
                                    "hypotheses", {}).items():
                                if (_n.get("status")
                                        in ("pending", "inconclusive")
                                        and not _n.get("deferred")):
                                    _n["deferred"] = True
                                    _n["selected"] = False
                                    _n["deferred_reason"] = (
                                        "[系统] 确定性收口：连续 "
                                        f"{self._exit_block_count} 次尝试生成报告，"
                                        "诊断未确认根因，该假设搁置并在报告中披露"
                                        "（design document §6.3 通用出口，v3.8.0）"
                                    )
                                    _shelved.append(fmt_hid(_h))
                            _disc = _rud3(ledger)
                            if _disc and isinstance(
                                    tool_args.get("content"), str):
                                tool_args["content"] = (
                                    tool_args["content"].rstrip() + _disc
                                )
                            self._persist_ledger(ledger)
                            logger.warning(
                                "write_file deterministic exit (round %d, "
                                "%d blocks): shelved %s, releasing honest "
                                "no-root-cause report with disclosure",
                                self._model_call_count,
                                self._exit_block_count, _shelved,
                            )
                            # fall through — write_file proceeds
                        else:
                            from diagnostics.agent.ledger import (
                                render_exit_directive as _red2,
                            )
                            _directive = _red2(ledger, _phase(ledger))
                            # R2 (v3.10.1): If a hypothesis has unrecorded
                            # expert evidence, name it in the block message
                            # — the LLM owes a record_finding, not a retry.
                            _r2_hint = None
                            for _h, _n in ledger.get(
                                    "hypotheses", {}).items():
                                if _n.get("status") in (
                                        "pending", "inconclusive"):
                                    _ch = compute_next_action(ledger, _h)
                                    if _ch and _ch.action == "record_finding":
                                        _r2_hint = (_h, _ch)
                                        break
                            _r2_suffix = ""
                            if _r2_hint:
                                _rh, _rc = _r2_hint
                                _r2_suffix = (
                                    f"\n⚠ {fmt_hid(_rh)} 已有专家验证证据"
                                    f"但尚未 record_finding 落账——"
                                    f"请先 record_finding("
                                    f"{fmt_hid(_rh)}, ...) 落账，"
                                    "系统确认后自动进入 REPORT。"
                                )
                            logger.warning(
                                "write_file blocked by exit-condition gate "
                                "(L%d): %s (phase=%s, round=%d, path=%s)",
                                self._exit_block_count,
                                reason,
                                _phase(ledger),
                                self._model_call_count,
                                tool_args.get("file_path")
                                or tool_args.get("path") or "",
                            )
                            return ToolMessage(
                                content=(
                                    _directive
                                    or (
                                        f"⛔ 暂时无法生成诊断报告: {reason}\n"
                                        "（REPORT 仅在根因确认、假设穷尽或"
                                        "证据饱和时开放）"
                                    )
                                ) + _r2_suffix,
                                tool_call_id=tool_call_id,
                            )
                # write_file passed the gate (natural exit, forced terminal,
                # E2' release-with-disclosure, or the v3.8.0 deterministic
                # honest-negative exit) — reset the generic block counter.
                self._exit_block_count = 0
                self._det_exit_grace_used = False
                # ── Stub-echo detection (v3.14.2 bugfix) — a synthetic
                # write_file (G7 timeout / verify-stall fallback) carries
                # the auto-generated stub verbatim.  Appendix injection
                # below would mutate it and break the downstream G10
                # stub-echo test (content != report → stub mark cleared →
                # overwrite invitation permanently disabled).  Detect the
                # stub echo BEFORE injection and skip appendices — the
                # stub needs none (G10 invites a real rewrite instead).
                _is_stub_echo_write = (
                    bool(ledger.get("_report_auto_generated"))
                    and isinstance(tool_args.get("content"), str)
                    and tool_args["content"] == ledger.get("report")
                )
                # ── Evidence-closure appendix (design document §6.3,
                # v3.11.0) — gathered-but-unrecorded expert evidence is
                # deterministically disclosed on EVERY gate-passing write
                # (natural exit included), so it can never be silently
                # dropped from the report (fail-open; MAST FM-3.1/FM-2.4).
                _appendix = render_unrecorded_evidence_appendix(ledger)
                if (_appendix
                        and not _is_stub_echo_write
                        and isinstance(tool_args.get("content"), str)
                        and "已采集未落账证据" not in tool_args["content"]):
                    tool_args["content"] = (
                        tool_args["content"].rstrip() + _appendix
                    )
                    logger.warning(
                        "write_file evidence-closure appendix injected "
                        "(round %d, %d chars)",
                        self._model_call_count, len(_appendix),
                    )
                # ── Ledger-derived sections (design document §9,
                # v3.14.0): evidence-layer labels, topology mapping,
                # delegated experts and refuted hypotheses are computed
                # from the ledger and injected on every gate-passing
                # write — structured ledger data never depends on LLM
                # transcription (fail-open, same channel as the
                # evidence-closure appendix above).
                _derived = render_derived_report_appendix(ledger)
                if (_derived
                        and not _is_stub_echo_write
                        and isinstance(tool_args.get("content"), str)
                        and "系统附录：台账派生数据" not in tool_args["content"]):
                    tool_args["content"] = (
                        tool_args["content"].rstrip() + _derived
                    )
                    logger.warning(
                        "write_file ledger-derived appendix injected "
                        "(round %d, %d chars)",
                        self._model_call_count, len(_derived),
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
        # frontend trees (write_file → propose_hypotheses → ...).
        #
        # Exception: if ALL root hypotheses are exhausted (refuted /
        # dead-end) and NO root cause has been confirmed, allow
        # propose_hypotheses / backtrack — the first attempt failed
        # and the LLM should be able to propose a new round.
        if tool_name in ("propose_hypotheses", "backtrack"):
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

        # ── Phase tool gating (design document §7, Q4 硬门控) ──
        # Reject out-of-phase ledger/diagnostic actions with a corrective
        # message.  read_file/write_todos/ls/grep/glob are always allowed
        # (read-only scaffolding).  Only Coordinator is gated — subagents
        # own their expert toolsets.
        if not self._is_subagent:
            ledger = self._current_ledger
            if ledger is not None:
                _phase_now = _phase(ledger)
                _allow = _PHASE_ALLOWLIST.get(_phase_now, frozenset())
                # write_file is governed by the dedicated exit-condition
                # gate, NOT the phase allowlist (§7/§11); read-only
                # scaffolding is exempt globally.
                if (tool_name != "write_file"
                        and tool_name not in _READONLY_SCOPES
                        and tool_name not in _allow):
                    logger.warning(
                        "%s blocked by phase allowlist in %s (round=%d)",
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
                "query_argus_k8s_cluster", "query_argus_k8s_node",
                "query_argus_k8s_workload", "query_argus_k8s_pod",
                "query_argus_k8s_etcd",
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
                        "请通过 task() 委派当前场景的 Argus 专家"
                        "（可用专家见 task 工具说明）采集对应指标。"
                    ),
                    tool_call_id=tool_call_id,
                )

        # ── L4: hostname 参数归一化（host-argus 网关）──
        # query_argus_* 的 hostname 参数契约是"主机名"（监控端点语义），但
        # LLM 可能误填节点 IP（node_name，如 10.30.1.12；2026-08-06 观测：
        # round-1 的 8 次 query_argus_* 的 hostname 全为 10.30.x 节点 IP）。
        # 此处用会话内拓扑的 node_name→host_name 映射做确定性归一化，并
        # 记录观测统计（design document §8.1⑥）。
        if tool_name in _HOST_ARGUS_TOOLS:
            _hn = (tool_args or {}).get("hostname")
            if _hn:
                _resolved = self._resolve_host_name(str(_hn))
                if _resolved and _resolved != _hn:
                    logger.warning(
                        "hostname normalized: %s -> %s (tool=%s, round=%d) — "
                        "LLM passed node IP instead of host name",
                        _hn, _resolved, tool_name, self._model_call_count,
                    )
                    tool_args["hostname"] = _resolved
                    _ledger_now = self._current_ledger
                    if _ledger_now is not None:
                        _obs = _ledger_now.setdefault("hostname_normalization", {})
                        _obs["normalized"] = _obs.get("normalized", 0) + 1
                elif not _resolved and _looks_like_ipv4(str(_hn)):
                    logger.warning(
                        "hostname IP not resolvable to host name: %s "
                        "(tool=%s, round=%d)",
                        _hn, tool_name, self._model_call_count,
                    )
                    _ledger_now = self._current_ledger
                    if _ledger_now is not None:
                        _obs = _ledger_now.setdefault("hostname_normalization", {})
                        _obs["unresolved_ip"] = _obs.get("unresolved_ip", 0) + 1

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

                # ── S2 gain<cost delegation gate (design document §6) ──
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
                        # View-aware value (design document
                        # parallel-delegation §8): F decays only on
                        # SAME-VIEW prior delegations — a complementary
                        # view is a new information source, not "the
                        # same evidence re-examined" (D6).
                        from diagnostics.agent.ledger import (
                            delegation_value_for, KAPPA_STOP,
                        )
                        # ── F1 conflict override (v3.12.0, design document
                        # §8 G17-E2/G20 context, 2026-08-12 scenario 37) ──
                        # When the metric-layer evidence for this hypothesis
                        # is CONFLICTED (argus anomaly + target-entity
                        # normal), E2/G20 deterministically REQUIRE a
                        # deep-expert verification before any verdict.  A
                        # deep expert is exactly the verification the guards
                        # demand — its delegation must NOT be killed by the
                        # soft gain<cost model (cost optimisation cannot
                        # override an evidence-standard requirement; the
                        # same override keeps the record_finding→E2 deadlock
                        # from forming).  Once the deep expert returns, the
                        # conflict clears and the economic model resumes.
                        _v = delegation_value_for(
                            ledger, _gate_hid, subagent_type)
                        _f1_conflict = _argus_conflict_signal(
                            ledger, _gate_hid)
                        _f1_is_deep = not subagent_type.endswith("-argus-expert")
                        if _v < KAPPA_STOP and not (
                                _f1_conflict and _f1_is_deep):
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
                                    "- 若无 confirmed 根因且换批预算未用尽：propose_hypotheses 换方向\n"
                                    "- 若退出判据已满足：系统将自动进入 REPORT"
                                ),
                                tool_call_id=tool_call_id,
                            )

                # ── Single-focus batch validation (design document
                # parallel-delegation §4.5) ──
                # A same-round parallel group must target ONE hypothesis.
                # The first ADMITTED task of the round (past the G5 gate
                # above) sets the focus; subsequent same-round tasks must
                # match it (cross-hypothesis is the parallel-abuse vector)
                # and must carry a parseable hypothesis ID.  Placed after
                # G5 so a gate-blocked task never claims the round focus;
                # placed before auto-heal so a focus-blocked task never
                # activates its hypothesis.  Check-and-set is synchronous
                # — atomic under asyncio cooperative scheduling.
                if not self._is_subagent:
                    if self._round_focus_round != self._model_call_count:
                        self._round_focus_round = self._model_call_count
                        self._round_focus_hid = None
                    if self._round_focus_hid is None:
                        if _gate_hid:
                            self._round_focus_hid = _gate_hid
                    else:
                        if not _gate_hid:
                            logger.warning(
                                "task() blocked by single-focus batch rule: "
                                "no hypothesis ID in a same-round batch "
                                "(focus=%s, round=%d)",
                                self._round_focus_hid, self._model_call_count,
                            )
                            return ToolMessage(
                                content=(
                                    "⛔ 并行委派须在 description 中明确假设 ID"
                                    "（如「验证假设 H2」），以便系统校验单焦点约束。"
                                ),
                                tool_call_id=tool_call_id,
                            )
                        if _gate_hid != self._round_focus_hid:
                            logger.warning(
                                "task() blocked by single-focus batch rule: "
                                "%s targets %s but round focus is %s (round=%d)",
                                subagent_type, _gate_hid,
                                self._round_focus_hid, self._model_call_count,
                            )
                            return ToolMessage(
                                content=(
                                    "⛔ 单焦点约束：同轮并行委派必须指向同一假设"
                                    f"（当前焦点 {fmt_hid(self._round_focus_hid)}）。"
                                    f"对 {fmt_hid(_gate_hid)} 的委派已被拦截——"
                                    "请验证完当前焦点假设后再切换。"
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

                ctx = _build_subagent_context(
                    ledger,
                    subagent_type=subagent_type,
                    stable_cache=self._stable_ctx_cache,
                )
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
                # Structured expert returns (deepagents response_format
                # JSON) are formatted into readable evidence text; the
                # raw JSON is preserved on the evidence entry under
                # ``structured`` for exact downstream parsing.
                _structured_expert = None
                if tool_name == "task":
                    _structured_expert = _parse_expert_json(output)
                    if _structured_expert is not None:
                        summary = _format_expert_summary(output)
                        # ── Verdict-evidence direction observation
                        # (v3.16.0, design document §8 G21 sibling):
                        # schema-valid but content-mismatched returns — a
                        # refuted verdict whose positive evidence outweighs
                        # its negative evidence (and vice versa) — are a
                        # known failure class.  Observation only, never
                        # blocks: downstream analysis correlates this tag
                        # with trace records.
                        _v = _structured_expert.get("verdict")
                        if _v in ("confirmed", "refuted"):
                            _ke = len(_structured_expert.get("key_evidence") or [])
                            _ne = len(_structured_expert.get("negative_evidence") or [])
                            _mismatch = (_v == "refuted" and _ke > _ne) or (
                                _v == "confirmed" and _ne > _ke
                            )
                            if _mismatch:
                                logger.warning(
                                    "suspect-verdict-mismatch: expert=%s verdict=%s "
                                    "key_evidence=%d negative_evidence=%d root_cause=%.120s",
                                    tool_args.get("subagent_type", "?"), _v,
                                    _ke, _ne,
                                    str(_structured_expert.get("root_cause") or ""),
                                )

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
                            _ev = new_evidence(
                                source, summary, supports=None, tool_call_id=tool_call_id,
                            )
                            if _structured_expert is not None:
                                _ev["structured"] = _structured_expert
                            node["evidence"].append(_ev)
                            # Track evidence round for state-driven guidance
                            # (design document §10, v3.10.1): compute_next_action
                            # uses _last_evidence_round vs _last_finding_round
                            # to detect "awaiting verdict" (expert returned but
                            # record_finding not yet called).
                            node["_last_evidence_round"] = self._model_call_count
                            if tool_name not in node["verification_tools"]:
                                node["verification_tools"].append(tool_name)
                        else:
                            add_evidence_to_active(
                                ledger, source, summary,
                                supports=None,
                                tool_call_id=tool_call_id,
                                tool_name=tool_name,
                                structured=_structured_expert,
                            )
                    else:
                        add_evidence_to_active(
                            ledger, source, summary,
                            supports=None,
                            tool_call_id=tool_call_id,
                            tool_name=tool_name,
                            structured=_structured_expert,
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
            delegated_for = None
            if tool_name == "task":
                expert = (tool_args.get("subagent_type")
                          or tool_args.get("subagent_name")
                          or tool_args.get("name"))
                if expert:
                    delegated = [expert]
                    # Structured delegation fact (design document
                    # parallel-delegation §5 D2): makes per-hypothesis
                    # delegated sets / parallel-group keys / audit
                    # derivable from the rounds history.
                    delegated_for = _parse_hypothesis_id_from_description(
                        tool_args.get("description", ""),
                        ledger.get("hypotheses", {}),
                    )

            record_round(
                ledger, derive_phase(ledger),
                tools_called=[tool_name],
                action_summary=tool_args.get("description", "") if tool_name == "task" else tool_name,
                key_findings=key_findings,
                _seq=invocation_seq,
                delegated_experts=delegated,
                delegated_for=delegated_for,
            )

        # After ledger management tools, persist and emit snapshot
        if tool_name in _LEDGER_TOOLS:
            # Track record_finding calls for stagnation detection
            if tool_name == "record_finding":
                self._consecutive_task_count = 0
                self._same_delegate_count = 0
                self._last_delegate_key = ""
                self._last_finding_round = self._model_call_count
            # propose_hypotheses marks the UNDERSTAND→VERIFY boundary;
            # reset stagnation counters so that UNDERSTAND-phase Argus task()
            # calls do not accumulate into the VERIFY-phase threshold.
            elif tool_name == "propose_hypotheses":
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
            # Group-level stall counting (design document
            # parallel-delegation §4.3): G3 models STALL — rounds spent
            # delegating without record_finding — so a same-round
            # parallel batch (one round of waiting) increments once, not
            # per task.  The value model's _delegate_count below stays
            # per-task (information-exhaustion semantics, path-invariant
            # between serial and parallel paths).
            if self._last_task_round != self._model_call_count:
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
                        # Disclosure backstop (design document §6.3,
                        # v3.6.0): a rewrite with fresh LLM content would
                        # otherwise silently drop the disclosure section
                        # injected at gate-release time.  Re-append it
                        # whenever unresolved hypotheses remain and the
                        # section is absent — the disclosure obligation
                        # attaches to the REPORT, not to a single write.
                        if not _is_stub_echo:
                            from diagnostics.agent.ledger import (
                                render_unverified_disclosure as _rud2,
                            )
                            _disc = _rud2(ledger)
                            if (_disc
                                    and "未验证竞争假设（系统披露）"
                                    not in content):
                                content = content.rstrip() + _disc
                            # Evidence-closure backstop (design document
                            # §6.3, v3.11.0): a rewrite with fresh content
                            # must not drop the unrecorded-evidence
                            # appendix either — the disclosure obligation
                            # attaches to the report, not a single write.
                            _apx = render_unrecorded_evidence_appendix(ledger)
                            if (_apx
                                    and "已采集未落账证据" not in content):
                                content = content.rstrip() + _apx
                            # Ledger-derived sections (design document
                            # §9, v3.14.0) — deterministic rendering on
                            # the rewrite path too.
                            _drv = render_derived_report_appendix(ledger)
                            if (_drv
                                    and "系统附录：台账派生数据" not in content):
                                content = content.rstrip() + _drv
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
           proposed, inject a strong directive to force propose_hypotheses.
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

        # ── P1: Maximum round limit — progress-aware, then synthesis + REPORT ──
        # The round cap is a stagnation backstop, not a blunt cutoff: when
        # the diagnosis is still making progress (recent record_finding /
        # propose_hypotheses / select_path / new evidence), grant a one-shot
        # grace extension instead of interrupting mid-verification.  Only
        # fire when the process has gone quiet — no ledger progress in the
        # last _PROGRESS_WINDOW rounds — or after the grace was consumed.
        # ── Scenario-aware round cap ──
        # "Complex" = the diagnosis already needed a retry root batch —
        # signals the straightforward path failed.  Such sessions get a
        # tighter cap: history shows rounds beyond ~24 in this state are
        # unproductive wandering.  (v2.7 flat model: the depth≥1 clause
        # was removed with sub-hypotheses.)
        is_complex = bool(ledger.get("_root_propose_count", 0) > 1)
        cap = _MAX_ROUNDS_COMPLEX if is_complex else _MAX_ROUNDS
        grace = _MAX_ROUNDS_COMPLEX_GRACE if is_complex else _MAX_ROUNDS_GRACE

        # ── G1 最大轮次安全阀（design document §8）：进展感知停滞兜底 ──
        if round_num >= cap and not ledger.get("_forced_terminal"):
            last_progress = max(
                self._last_finding_round,
                self._last_propose_round,
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
        #   (a) Diagnostic data WAS collected → force propose_hypotheses
        #       (round >= 3 only — at round 2 the LLM proposes in its
        #       response on the normal path, so a round-2 directive is a
        #       false "系统强制" injection observed in every session)
        #   (b) Diagnostic data was NOT collected → force data collection
        #       (round >= 2 — catches round-1 text simulation early)
        #   (c) Partial Argus coverage → force the missing expert (round >= 2)
        #   (d) All experts returned empty data → short-circuit to REPORT
        # Without this valve, the LLM may output analysis as plain text
        # instead of calling propose_hypotheses, causing the loop to exit.
        if (
            round_num >= 2
            and not ledger.get("hypotheses")
        ):
            rounds_history = ledger.get("rounds", [])
            _data_collection_tools = frozenset({
                "task",
                "query_argus_nodes", "query_argus_services",
                "query_argus_k8s_cluster", "query_argus_k8s_node",
                "query_argus_k8s_workload", "query_argus_k8s_pod",
                "query_argus_k8s_etcd",
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
                # ── Scene-aware Argus coverage check ──
                # The required Argus expert set is determined by the
                # CURRENT scene via _required_argus (Serverless
                # four-view, host single, dedicated container dual) —
                # the same single source of truth as the UNDERSTAND
                # coverage gate (D1) and the proactive coverage hint.
                # When coverage is incomplete, force delegation of the
                # missing expert instead of prematurely forcing
                # propose_hypotheses.
                #
                # NOTE: Coordinator does NOT have Argus tools directly —
                # detection uses delegated_experts (populated from
                # task(subagent_type=...) calls), NOT tools_called.
                # History: this block previously hardcoded the
                # host/k8s dual-expert pair, mis-firing on Serverless
                # sessions (forcing the forbidden k8s-argus-expert) —
                # see design document §11 (2026-08-05).
                experts_delegated: set[str] = set()
                has_delegated = False
                for r in rounds_history:
                    experts_delegated.update(r.get("delegated_experts", []))
                    if "task" in r.get("tools_called", []):
                        has_delegated = True

                is_host_only = ledger.get("entity_type") == "host"

                if ledger.get("_argus_unavailable"):
                    # Argus platform failure (v2.5 degrade): coverage and
                    # delegation interventions are pointless — argus is
                    # unavailable.  Just keep nudging propose from user
                    # input; the normal flow continues with domain
                    # experts collecting evidence in VERIFY.
                    if round_num >= 3 and _phase(ledger) == "hypothesize":
                        warnings.append(
                            "⚠ [系统提示] Argus 监控平台故障，监控数据不"
                            "可用。\n请基于用户描述的故障现象调用 "
                            "propose_hypotheses 提出假设，"
                            "后续专家诊断将继续验证并修正假设。"
                        )
                # Only intervene when an expert WAS delegated but the
                # scene's required Argus set is still incomplete.
                # Host-only scenarios skip this check entirely (the
                # scene has no cluster-level expert to force).
                elif (has_delegated and not is_host_only
                      and _argus_coverage_gap(ledger)):
                    _missing = _argus_coverage_gap(ledger)
                    _missing_name = _missing[0]
                    warnings.append(
                        _render_argus_delegation_hint(ledger, _missing))
                    logger.warning(
                        "Safety: partial Argus coverage (missing: %s, "
                        "round %d) — forcing %s delegation",
                        ", ".join(_missing), round_num,
                        _missing_name,
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
                            "propose_hypotheses 提出假设，"
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
                                "propose_hypotheses 提出假设。"
                            )
                    elif round_num >= 3 and _phase(ledger) == "hypothesize":
                        # Coverage gate already opened (phase derives to
                        # hypothesize) but the LLM still hasn't proposed —
                        # a genuine stagnation signal.  Round-2 exemption:
                        # on the normal path the LLM proposes in its round-2
                        # response, so a round-2 directive is always a
                        # false positive.  When the gate has NOT opened yet
                        # (still understand), the partial-coverage branch
                        # above owns the corrective message instead — telling
                        # the LLM to propose here would be rejected by the
                        # phase tool gate.
                        warnings.append(
                            "⛔ [系统强制] 数据采集已完成，当前已是第"
                            f"{round_num}轮，你仍未提出任何假设。\n"
                            "禁止继续调用诊断工具。\n"
                            "立即基于已有 Argus 数据调用 propose_hypotheses，"
                            "否则诊断将提前终止。"
                        )
                        logger.warning(
                            "Safety: hypothesize stagnation with data (round %d, "
                            "no hypotheses) — forcing propose",
                            round_num,
                        )
            else:
                # No data collected.  _coverage_ready treats "never
                # delegated" as ready (ledger.py), so at round>=2 the
                # derived phase is already hypothesize — the old text
                # ("禁止调用 propose_hypotheses，必须委派 Argus") ALWAYS
                # contradicted the phase guidance.  Only demand
                # delegation while genuinely in UNDERSTAND; otherwise
                # nudge proposing from user input (same semantics as
                # the argus-degrade path).
                if _phase(ledger) == "understand":
                    _required_names = "、".join(_required_argus(ledger))
                    warnings.append(
                        "⚠ [系统强制] 当前是第"
                        f"{round_num}轮，你尚未提出任何假设，"
                        "且未调用任何诊断工具采集数据。\n"
                        "禁止输出纯文本分析，禁止调用 propose_hypotheses。\n"
                        f"你必须在本轮调用 task() 委派 Argus 专家"
                        f"（{_required_names}）采集监控指标数据。\n"
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
                        "用户描述的故障现象调用 propose_hypotheses 提出假设，"
                        "后续专家诊断将继续验证并修正假设。"
                    )

        # ── G5 委派饱和（design document §8）：防止重复委派相同专家验证相同假设 ──
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

            # ── G3 VERIFY 委派无结论（design document §8）：连续 3 次 task 无 record_finding ──
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
                        "hypothesize": "调用 propose_hypotheses 提出假设",
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
                # clock — the LLM just proposed to a recovery plan for
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
                            "propose_hypotheses。"
                        ),
                        "hypothesize": (
                            "请调用 propose_hypotheses 提出新一批假设"
                            f"（第 {ledger.get('_propose_count', 0) + 1}"
                            f"/{MAX_PROPOSE_CALLS} 次）——"
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
                "record_finding / propose_hypotheses。\n"
                "禁止调用任何诊断工具、委派专家、record_finding、"
                "propose_hypotheses。\n"
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
            self._report_forced_enter_round = 0

        return ("\n\n".join(warnings) if warnings else ""), mutated

    # ── Tool: propose_hypotheses ──

    def _make_propose_hypotheses_tool(self) -> StructuredTool:
        mw = self

        async def _run(
            hypotheses: list[HypothesisSpec],
            focus_summary: str = "",
        ) -> str:
            ledger = mw._current_ledger or new_ledger()

            # Convert Pydantic objects to plain dicts (args_schema parses JSON)
            specs = [h.model_dump() if hasattr(h, "model_dump") else dict(h) for h in hypotheses]

            # Detect the append channel (T10′) BEFORE the call: the batch
            # gate is closed (active pending) while the append guard is
            # open.  add_hypotheses re-derives the same branch; this local
            # copy only drives the receipt wording.
            _is_append = (
                bool(ledger.get("hypotheses"))
                and not can_propose_root_hypotheses(ledger)
                and can_append_hypothesis(ledger)
            )

            try:
                created = add_hypotheses(ledger, specs)
            except ValueError as e:
                return f"假设调用失败: {e}"

            # Update phase and track creation round
            mw._last_propose_round = mw._model_call_count

            # Build confirmation
            lines = [f"已提出 {len(created)} 个假设:"]
            for h in created:
                sel = " ★" if h["selected"] else ""
                lines.append(f"  {fmt_hid(h['id'])} [{h['status']} p={h['probability']}%]{sel} {h['statement']}")
            if _is_append:
                lines.append(
                    "（证据驱动追加：新假设已并入当前批，不占用换批预算；"
                    f"假设总数 {len(ledger.get('hypotheses', {}))}"
                    f"/{MAX_TOTAL_HYPOTHESES}）"
                )
            else:
                _budget_left = MAX_PROPOSE_CALLS - ledger.get("_propose_count", 0)
                if _budget_left > 0:
                    lines.append(
                        f"（提出预算剩余 {_budget_left}/{MAX_PROPOSE_CALLS} 次）"
                    )
                else:
                    lines.append(
                        f"（提出预算已用尽 {MAX_PROPOSE_CALLS}/{MAX_PROPOSE_CALLS} 次；"
                        "后续如需细化假设，请用 record_finding(statement_update=...)）"
                    )

            # Next-step guidance (design document §9, v3.6.1): a bare
            # success confirmation leaves the model without a forward
            # signal and measurably invites self-repetition (2026-08-08
            # scenario-32 replay: the model re-emitted the same analysis
            # text plus an empty-args propose_hypotheses on the very next
            # round).  StateWright, positive form: a response should name
            # the available transition, not only report the result.
            _focus = next((h for h in created if h.get("selected")),
                          created[0] if created else None)
            if _focus is not None:
                lines.append(
                    f"下一步：已进入 VERIFY 阶段——请委派 task 验证焦点假设 "
                    f"{fmt_hid(_focus['id'])}（按假设涉及领域选择对应专家），"
                    "收到专家结论后用 record_finding 落账判定"
                    "（confirmed 须以专家验证结论为依据）。"
                )

            mw._persist_ledger(ledger)
            mw._current_ledger = ledger

            return "\n".join(lines)

        return StructuredTool.from_function(
            name="propose_hypotheses",
            description=(
                "提出诊断假设（HYPOTHESIZE阶段必须调用）。每次最多3个假设，按概率降序。"
                "每个假设必须包含 statement（必填，一句完整根因表述）、probability(0-100)、"
                "rationale 三个字段，缺一不可；statement 缺失会导致调用失败（预算不返还）。"
                "若多个候选描述的是同一故障的机制/因果链上下游（如\"内存超限 OOMKilled\""
                "与\"memory limit 配置过低\"互为表里），应合并为一条假设；"
                "仅当证据显示独立故障源并存时才提出多条。"
                "本工具只提出待验证的候选假设，不产生任何结论；确认或证伪已有假设必须调用 "
                "record_finding，重复提出不会确认任何假设。"
                "假设为扁平结构（ID 为整数编号如 H1、H2），不支持在某假设下提出子假设；"
                "如需更具体的表述，用 record_finding(statement_update=...) 修正。"
                "整个诊断最多提出2次（首批、换批共用此预算），总计不超过6个假设。"
                "预算用尽后如需细化，用record_finding(statement_update=...)修正已有假设表述。"
                "验证中途新证据指向当前假设集之外的全新方向时，可在 EVALUATE 阶段用本工具"
                "追加1个新假设（单次仅1个；追加不占用换批预算，但总假设数不超过6个）——"
                "区别于换批：换批是当前方向全部失败后的重开，追加是当前方向仍在验证中的补充。"
                "提出后系统自动聚焦概率最高的假设。"
            ),
            coroutine=_run,
            args_schema=ProposeHypothesesInput,
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
            # propose_hypotheses auto-selects the highest-probability
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
                    "（提出时已自动选中最高概率假设），无需重复 select_path。\n"
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
                "目标必须是 pending/inconclusive 假设（含 deferred 复活）；"
                "confirmed/refuted 终态假设不可选——confirmed 需细化请改用 "
                "record_finding(statement_update=...)，refuted 已证伪不可复活。"
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
            verdict: str | None = None,
            evidence_summary: str = "",
            probability_update: int | None = None,
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

            # ── B′: pure statement refinement (v3.14.1, iterative-
            # hypothesis-update) ──
            # verdict omitted + statement_update present = refine THIS
            # hypothesis's wording without formalizing a verdict — the
            # channel for updating OTHER pending hypotheses' statements
            # after verifying H1 (Belief-R: LLMs need scaffolding to
            # revise beliefs; design document §5).  Pure refinement is
            # zero-new-evidence and does not advance state, so it is
            # invisible to G5/S2/E3 economic-exit invariants — an
            # unbounded channel would let the LLM dodge convergence.
            # Bounded to 2 per hypothesis (gain converges after one
            # correction + one tolerance redo; cost 2×0.5 aligns with a
            # batch-propose, symmetric with E3's "3rd saturation").
            if verdict is None:
                _pure_node = ledger.get("hypotheses", {}).get(hypothesis_id)
                if _pure_node is None:
                    return f"错误: 假设 {fmt_hid(hypothesis_id)} 不存在"
                if _pure_node.get("status") != "pending":
                    return (
                        f"⚠ 纯表述修正仅针对 pending(未验证) 假设："
                        f"{fmt_hid(hypothesis_id)} 当前 status="
                        f"{_pure_node.get('status')}，请用正常 record_finding "
                        "落 verdict 推进状态。"
                    )
                if not statement_update:
                    return (
                        "错误: 纯表述修正必须提供 statement_update（修正后的表述）。"
                    )
                if _pure_node.get("_statement_only_updates", 0) >= 2:
                    return (
                        f"⛔ 假设 {fmt_hid(hypothesis_id)} 已纯表述修正 2 次，"
                        "继续修正不产生新信息。请落 verdict 推进状态："
                        "refuted（若已可排除）/ inconclusive（证据不足）/ "
                        "confirmed（有专家证据支持）。"
                    )
                record_finding(ledger, hypothesis_id, None, "", None,
                               "", statement_update)
                mw._persist_ledger(ledger)
                mw._current_ledger = ledger
                _cnt = _pure_node.get("_statement_only_updates", 1)
                return (
                    f"✅ 已修正 {fmt_hid(hypothesis_id)} 的表述"
                    f"（纯修正 {_cnt}/2 次）。"
                    "该假设仍为 pending，后续验证请据此新表述委派专家。"
                )

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

            # ── G17: confirmed requires expert-verification evidence ──
            # (design document §8 G17, v3.7.0 — evidence-standard gate,
            # reactive backstop of the proactive contract declared in the
            # tool description / VERIFY duty / propose receipt).  Rationale:
            # a confirmed verdict is ACTION-guiding (ops will fix based on
            # it), so it demands confirmatory-layer evidence (an expert's
            # verification conclusion), while refuted/inconclusive are
            # legitimate on screening-layer (monitoring) evidence alone —
            # screening→confirmatory asymmetry.  Exemptions: reinforcing
            # an already-confirmed node (it already carries expert
            # evidence by construction); scenes with no assembled experts
            # (deadlock prevention).
            if (verdict == "confirmed"
                    and hypothesis_id in ledger.get("hypotheses", {})):
                _g17_node = ledger["hypotheses"][hypothesis_id]
                if _g17_node.get("status") != "confirmed":
                    _has_expert = any(
                        str(e.get("source", "")).startswith("expert:")
                        for e in _g17_node.get("evidence", [])
                    )
                    _experts = ledger.get("scene_experts") or []
                    # ── G17-E2: conflicted argus evidence ≠ confirmatory ──
                    # (2026-08-11 scenario 38, design document §8 G17 —
                    # "confirmed demands confirmatory-layer evidence; the
                    # monitoring layer alone is insufficient", screening→
                    # confirmatory two-stage §12).  An argus-class expert
                    # IS an "expert" to the _has_expert check, but a
                    # CONFLICTED argus return (cluster/node anomaly +
                    # target-entity normal) is still screening-layer: the
                    # metric layer cannot see Pod event states, so it
                    # cannot confirm an event-state hypothesis.  Mirrors
                    # C2 (refuted guard) — symmetric deterministic
                    # backstop when the proactive directive (C1) was not
                    # followed.  Deep-delegation clears the conflict
                    # automatically (evidence is no longer all-argus), so
                    # the channel reopens — same soft-gate semantics.
                    _g17e2_conflict = (
                        _argus_conflict_signal(ledger, hypothesis_id)
                        if _has_expert else None
                    )
                    if _g17e2_conflict and _experts:
                        _deep_avail = [e for e in _experts
                                       if not e.endswith("-argus-expert")]
                        # ── F2 bounded-block degrade (v3.12.0, design
                        # document §8 G17-E2 context, 2026-08-12 scenario
                        # 37) ──
                        # A repeated confirmed attempt that keeps hitting
                        # G17-E2 (while deep delegation stays possible but
                        # the LLM keeps retrying the verdict) must not
                        # deadlock the loop.  After E2_BLOCK_LIMIT
                        # consecutive blocks, the guard DEGRADES: it lets
                        # the verdict through with a disclosure note (OpenAI
                        # SDK §12: a guardrail that only returns text lets
                        # the model retry forever — bound it).  The F1
                        # conflict override already re-opens deep
                        # delegation, so the degrade path is reached only
                        # when the model refuses to take that route.
                        _g17_node["_g17e2_block_count"] = (
                            _g17_node.get("_g17e2_block_count", 0) + 1
                        )
                        if _g17_node["_g17e2_block_count"] > _E2_BLOCK_LIMIT:
                            logger.warning(
                                "record_finding G17-E2 degraded after %d "
                                "blocks on %s (round=%d) — passing with "
                                "disclosure",
                                _g17_node["_g17e2_block_count"],
                                fmt_hid(hypothesis_id),
                                self._model_call_count,
                            )
                            return (
                                f"⚠ confirmed 判定已通过（G17-E2 冲突门控"
                                f"连续 {_g17_node['_g17e2_block_count']} 次拦截后"
                                f"降级放行）：{fmt_hid(hypothesis_id)} 的 argus "
                                f"证据仍存在指标层冲突（集群/节点/工作负载级异常"
                                f"与目标实体正常并存），本判定未经深度专家日志/事件"
                                f"确认，系统在报告中披露此冲突。"
                            )
                        logger.warning(
                            "record_finding blocked by G17-E2: confirmed on "
                            "%s with conflicted argus evidence (round=%d)",
                            fmt_hid(hypothesis_id),
                            self._model_call_count,
                        )
                        _g17_node["_g17_blocked_round"] = self._model_call_count
                        return (
                            f"⛔ confirmed 判定与现有监控证据冲突（证据标准门控）："
                            f"{fmt_hid(hypothesis_id)} 的 argus 指标层证据同时报告"
                            f"（集群/节点/工作负载级）异常与目标实体正常——指标层无法"
                            f"覆盖 Pod 事件状态（OOMKilled 在指标中不可见），该冲突"
                            f"证据不满足 confirmed 的确证层标准。"
                            f"请先委派深度专家"
                            f"{('（' + '、'.join(_deep_avail[:3]) + '）') if _deep_avail else ''}"
                            f"查目标实体日志/事件确认后再 record_finding confirmed；"
                            "深度专家证据到位后 confirmed 通道自动开放，不会再被拦截。"
                            "若现有监控证据已明确证伪，可直接判 refuted；"
                            "证据不足请判 inconclusive。"
                        )
                    if not _has_expert and _experts:
                        logger.warning(
                            "record_finding blocked by G17: confirmed on "
                            "%s without expert evidence (round=%d)",
                            fmt_hid(hypothesis_id),
                            self._model_call_count,
                        )
                        # Track G17 block for state-driven recovery guidance
                        # (design document §10, v3.10.1): compute_next_action
                        # uses this to detect the "G17 recovery path" and
                        # proactively reassure "confirmed 通道已开放" after
                        # the expert returns (counteracts Belief-R
                        # overcorrection).
                        _g17_node["_g17_blocked_round"] = self._model_call_count
                        return (
                            f"⛔ confirmed 判定需专家验证证据（证据标准门控）："
                            f"{fmt_hid(hypothesis_id)} 尚无专家验证结论。"
                            f"请委派 task 验证该假设（可用专家："
                            f"{', '.join(_experts)}；监控时序定向复核可委派对应 "
                            "argus 专家，description 带假设聚焦的具体问题），"
                            "收到专家结论后再 record_finding confirmed——"
                            "专家证据到位后 confirmed 通道自动开放，不会再被拦截。"
                            "若现有监控证据已明确证伪，可直接判 refuted；"
                            "证据不足请判 inconclusive。"
                        )

            # ── C2: argus-conflict refute guard (reactive backstop,
            # design document §8 G13 context, 2026-08-11 scenario 38) ──
            # Refuting an event-state hypothesis (OOMKilled/CrashLoopBackOff)
            # on metric-layer evidence alone is unsafe when the argus
            # evidence is CONFLICTED (cluster/node anomaly + target-entity
            # normal): the metric layer cannot see Pod event states, so the
            # "target normal" negative evidence does not actually refute.
            # The proactive directive (C1) already steers to a deep-dive;
            # this gate backstops a premature refuted verdict.  Soft gate:
            # first occurrence warns and blocks; a second call with the
            # same verdict after deep-delegation is allowed (state moved).
            if (verdict == "refuted"
                    and hypothesis_id in ledger.get("hypotheses", {})):
                _c2_conflict = _argus_conflict_signal(ledger, hypothesis_id)
                if _c2_conflict:
                    _c2_node = ledger["hypotheses"][hypothesis_id]
                    # ── F2 bounded-block degrade (symmetric with G17-E2,
                    # v3.12.0) ── same deadlock bound applies: after
                    # _E2_BLOCK_LIMIT consecutive C2 blocks the guard
                    # passes refuted with disclosure instead of looping.
                    _c2_node["_c2_block_count"] = (
                        _c2_node.get("_c2_block_count", 0) + 1
                    )
                    if _c2_node["_c2_block_count"] > _E2_BLOCK_LIMIT:
                        logger.warning(
                            "record_finding C2 degraded after %d blocks on "
                            "%s (round=%d) — passing with disclosure",
                            _c2_node["_c2_block_count"],
                            fmt_hid(hypothesis_id),
                            self._model_call_count,
                        )
                        return (
                            f"⚠ refuted 判定已通过（C2 冲突门控连续 "
                            f"{_c2_node['_c2_block_count']} 次拦截后降级放行）："
                            f"{fmt_hid(hypothesis_id)} 的 argus 证据仍存在指标层"
                            f"冲突（集群/节点/工作负载级异常与目标实体正常并存），"
                            f"本判定未经深度专家日志/事件确认，系统在报告中披露。"
                        )
                    logger.warning(
                        "record_finding blocked by C2: refuted on %s with "
                        "conflicted argus evidence (round=%d)",
                        fmt_hid(hypothesis_id),
                        self._model_call_count,
                    )
                    _c2_node["_c2_blocked_round"] = self._model_call_count
                    return (
                        f"⛔ refuted 判定与现有监控证据冲突（证据充分性门控）："
                        f"{fmt_hid(hypothesis_id)} 的 argus 指标层证据同时报告"
                        f"（集群/节点/工作负载级）异常与目标实体正常——指标层无法"
                        f"覆盖 Pod 事件状态（OOMKilled 在指标中不可见），该负证据"
                        f"不足以证伪事件类假设。请先委派深度专家（查目标实体日志/"
                        f"事件）确认后再判定；若深度专家已确认目标实体无异常，"
                        f"可再次 record_finding refuted（通道自动开放）。"
                    )
            # ── G21: expert-verdict conflict arbitration (reactive
            # backstop, design document §8, v3.16.0; 2026-08-21 scenario
            # 40 follow-up) ── When the SAME hypothesis already holds a
            # structured deep-expert return with an OPPOSING terminal
            # verdict, a new terminal record_finding would let the later
            # expert silently overwrite the earlier one (recency bias;
            # AGM belief revision requires an explicit retraction
            # reason).  Soft gate: three arbitration routes + an
            # explicit-overrule channel (statement_update carries the
            # retraction reason); bounded-block degrade symmetric with
            # C2/G17-E2 (_E2_BLOCK_LIMIT).
            if verdict in ("confirmed", "refuted"):
                _g21_conflict = expert_verdict_conflict(
                    ledger, hypothesis_id, verdict)
                if _g21_conflict:
                    _g21_node = ledger["hypotheses"][hypothesis_id]
                    if statement_update:
                        # Explicit-overrule channel: belief revision WITH
                        # a stated reason — allowed, counter reset.
                        _g21_node["_g21_block_count"] = 0
                        logger.info(
                            "record_finding G21 explicit overrule on %s "
                            "(new=%s, prior %s=%s, statement_update "
                            "carries retraction reason, round=%d)",
                            fmt_hid(hypothesis_id), verdict,
                            _g21_conflict["expert"],
                            _g21_conflict["prev_verdict"],
                            self._model_call_count,
                        )
                    else:
                        _g21_node["_g21_block_count"] = (
                            _g21_node.get("_g21_block_count", 0) + 1
                        )
                        if _g21_node["_g21_block_count"] > _E2_BLOCK_LIMIT:
                            logger.warning(
                                "record_finding G21 degraded after %d "
                                "blocks on %s (round=%d) — passing with "
                                "disclosure",
                                _g21_node["_g21_block_count"],
                                fmt_hid(hypothesis_id),
                                self._model_call_count,
                            )
                            return (
                                f"⚠ {verdict} 判定已通过（G21 专家结论冲突仲裁"
                                f"连续 {_g21_node['_g21_block_count']} 次拦截后"
                                f"降级放行）：{fmt_hid(hypothesis_id)} 存在未仲裁的"
                                f"专家结论冲突（{_g21_conflict['expert']} 判定 "
                                f"{_g21_conflict['prev_verdict']} 与本次判定相反），"
                                f"本判定未经冲突仲裁，系统在报告中披露。"
                            )
                        logger.warning(
                            "record_finding blocked by G21: %s verdict=%s "
                            "opposes prior %s verdict=%s (round=%d)",
                            fmt_hid(hypothesis_id), verdict,
                            _g21_conflict["expert"],
                            _g21_conflict["prev_verdict"],
                            self._model_call_count,
                        )
                        return (
                            f"⛔ 假设 {fmt_hid(hypothesis_id)} 已有专家终局结论与"
                            f"本次判定相反（专家结论冲突仲裁）："
                            f"{_g21_conflict['expert']} 此前判定 "
                            f"{_g21_conflict['prev_verdict']}"
                            f"（证据摘要：{_g21_conflict['summary']}…），"
                            f"而本次拟判 {verdict}。两个深度专家对同一假设给出"
                            f"相反终局结论属跨专家证据矛盾，直接落账将以新结论"
                            f"静默覆盖旧结论。请先仲裁：\n"
                            f"1. 复检矛盾数据源：委派第三方视角专家（或同一专家"
                            f"复核具体矛盾点，如两侧对同一物理量的观测差异）确认"
                            f"哪侧证据可靠；\n"
                            f"2. 以冲突披露收口：若无法仲裁，改判 inconclusive 并"
                            f"在 new_insights 披露两侧矛盾证据，报告将如实呈现"
                            f"未解冲突；\n"
                            f"3. 明示推翻理由：若确有把握推翻既有结论，携带 "
                            f"statement_update（含推翻理由，如\"新证据为直接测量，"
                            f"旧证据为间接观测\"）重新落账，通道立即开放。"
                        )
            # ── G18: ceremonial-repeat guard (design document §8,
            # v3.11.0) ──
            # A record_finding that (a) repeats the CURRENT status
            # (inconclusive→inconclusive / confirmed→confirmed), (b) has
            # seen NO new expert evidence since the last finding, and
            # (c) carries no statement_update, produces zero information
            # gain (MAST FM-1.3 step repetition, the single most frequent
            # multi-agent failure mode; LLMs do not self-correct without
            # external scaffolding — arXiv:2310.01798).  Unchecked, it
            # burns rounds and can game the E3 streak (three ceremonial
            # inconclusives would masquerade as "evidence saturation").
            # Graduated response (dedup-F2 philosophy): the first pass is
            # ALLOWED with a warning (a one-off probability refinement is
            # legitimate); the second is hard-blocked with the legal
            # action menu.  First-ever verdicts (pending→inconclusive)
            # and verdict CHANGES are never ceremonial (status moves).
            _g18_warn = ""
            _g18_node = ledger.get("hypotheses", {}).get(hypothesis_id)
            if (_g18_node is not None and not statement_update
                    and _g18_node.get("_last_finding_round") is not None):
                _same_verdict = (
                    (verdict == "inconclusive"
                     and _g18_node.get("status") == "inconclusive")
                    or (verdict == "confirmed"
                        and _g18_node.get("status") == "confirmed")
                )
                _no_new_evidence = (
                    _g18_node.get("_last_evidence_round", 0)
                    <= _g18_node.get("_last_finding_round", 0)
                )
                if _same_verdict and _no_new_evidence:
                    _cer_count = _g18_node.get("_ceremonial_count", 0) + 1
                    _g18_node["_ceremonial_count"] = _cer_count
                    if _cer_count >= 2:
                        logger.warning(
                            "record_finding blocked by G18 (ceremonial "
                            "repeat): %s verdict=%s, no new evidence since "
                            "last finding (round=%d)",
                            fmt_hid(hypothesis_id), verdict,
                            self._model_call_count,
                        )
                        return (
                            f"⛔ 重复落账无信息增量（仪式性验证防护）："
                            f"{fmt_hid(hypothesis_id)} 已处于 {verdict} 状态，"
                            "且自上次落账以来无新专家证据——重复记录相同判定"
                            "不产生任何新信息。\n"
                            "合法动作：\n"
                            "- 若认为仍有验证价值：委派互补视角专家获取新证据后再判定\n"
                            "- 若现有证据已支持其他结论：改用对应 verdict（confirmed/refuted）终结\n"
                            "- 若仅需修正表述：使用 statement_update 参数\n"
                            "- 若其他假设待处理：select_path 切换 / propose_hypotheses 追加"
                        )
                    _g18_warn = (
                        f"\n⚠ 注意：本次落账与当前状态一致（{verdict}→{verdict}）"
                        "且期间无新专家证据——属低信息增量操作；"
                        "再次无新证据重复落账将被系统拦截。"
                    )

            try:
                record_finding(
                    ledger, hypothesis_id, verdict,
                    evidence_summary, probability_update, new_insights,
                    statement_update,
                )
            except ValueError as e:
                return f"记录失败: {e}"

            # G21 arbitration counter resets on every successful verdict
            # (design document §8, v3.16.0): a completed verdict means the
            # conflict was resolved (or explicitly overruled).
            _g21_done = ledger.get("hypotheses", {}).get(hypothesis_id)
            if isinstance(_g21_done, dict) and "_g21_block_count" in _g21_done:
                _g21_done["_g21_block_count"] = 0

            # Check exit conditions
            should_exit, reason, confirmed_id = check_exit_conditions(ledger)

            # ── Root-cause tracking (v2.7 flat model) ──
            # A confirmed root cause requires BOTH confirmed AND p ≥ 80
            # (design document §6 S1 / record_finding contract) — shared
            # predicate is_confirmed_root.  A low-confidence confirmed
            # hypothesis (e.g. a synonymous hypothesis stamped confirmed
            # with p=0) is NOT a root cause; it stays in hypotheses for
            # the LLM to either strengthen (p≥80) or re-verify.
            cids: list[str] = ledger.setdefault("root_cause_hypothesis_ids", [])
            causes: list[str] = ledger.setdefault("root_causes", [])
            hypotheses = ledger.get("hypotheses", {})
            for hid, node in hypotheses.items():
                if not is_confirmed_root(node):
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
            # root causes.  Also drops low-confidence confirmed (p<80)
            # so the tracker stays aligned with S1 (is_confirmed_root).
            hypotheses_dict = ledger.get("hypotheses", {})
            cids[:] = [
                cid for cid in cids
                if is_confirmed_root(hypotheses_dict.get(cid, {}))
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
                # Natural exit (T4): the LLM's own record_finding satisfied
                # the exit conditions — do NOT set the guardrail terminal
                # marker.  _forced_terminal is reserved for guardrail-forced
                # entries (design document §3 step 1); the exit conditions
                # stay true after this finding, so derive_phase remains
                # pinned to REPORT without the lock.  Record the path for
                # post-hoc natural/forced classification (§10 _exit_path).
                ledger["_exit_path"] = "natural"
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
                # Evidence-closure proactive note (design document §6.3,
                # v3.11.0): name hypotheses whose expert evidence was
                # never formalized — the report body must cite it; the
                # system additionally injects the appendix at write time.
                _unrec = unrecorded_evidence_hypotheses(ledger)
                if _unrec:
                    _unames = "、".join(fmt_hid(_h) for _h, _ in _unrec)
                    exit_hint += (
                        f"\n⚠ {_unames} 有专家验证证据未落账——"
                        "报告证据链须如实引用这些证据（不得遗漏）；"
                        "系统将同时自动注入「已采集未落账证据」附录。"
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
                    and ledger.get("_root_propose_count", 0) > 0):
                # All prior root hypotheses hard-failed and the retry
                # budget is still open — nudge the LLM to submit a
                # fresh batch in a new direction.
                exit_hint = (
                    f"\n⚠ {reason}\n"
                    "⛔ 请调用 propose_hypotheses 提出新一批假设"
                    f"（第 {ledger.get('_propose_count', 0) + 1}"
                    f"/{MAX_PROPOSE_CALLS} 次，最后一次）——"
                    "必须基于已排除证据换方向推断，"
                    "禁止与已排除假设重复或仅换措辞。"
                )
            else:
                exit_hint = ""
            stmt_hint = f"\nℹ 假设表述已修正为: {statement_update}" if statement_update else ""
            # ── Evidence-impact re-evaluation hint (design document §9,
            # v3.10.0): a new finding may bear on the WHOLE differential,
            # not just the focus hypothesis (iterative hypothesis testing
            # — cue interpretation updates all candidates; LLMs do not
            # reliably perform this revision spontaneously).  The receipt
            # deterministically lists the remaining undecided hypotheses
            # with current probabilities and names the two legal actions
            # (cross-hypothesis ledgering / evidence-driven append).
            # Selective injection: silence when no other undecided
            # hypothesis exists, and never compete with an exit/retry
            # directive (exit_hint already names the next action).
            _impact_hint = ""
            if not exit_hint:
                _others = [
                    n for _hid, n in ledger.get("hypotheses", {}).items()
                    if _hid != hypothesis_id
                    and n.get("status") in ("pending", "inconclusive")
                ]
                if _others:
                    _items = "、".join(
                        f"{fmt_hid(n['id'])}（p={n.get('probability')}%"
                        f"{'，已搁置' if n.get('deferred') else ''}）"
                        for n in _others
                    )
                    _impact_hint = (
                        f"\nℹ 证据影响重估：本次证据若同时支持/排除其他未决假设"
                        f"（{_items}），可直接 record_finding 落账"
                        "（无需 select_path 切换）。"
                    )
                    if can_append_hypothesis(ledger):
                        _impact_hint += (
                            "若证据指向当前假设集之外的全新方向，"
                            "可 propose_hypotheses 追加 1 个新假设"
                            f"（总数 {len(ledger.get('hypotheses', {}))}"
                            f"/{MAX_TOTAL_HYPOTHESES}，不占换批预算）。"
                        )
            return (
                f"已记录验证结果: {fmt_hid(hypothesis_id)} → {verdict} (p={probability_update}%)\n"
                f"{evidence_summary}{stmt_hint}{exit_hint}{_impact_hint}{_g18_warn}"
            )

        return StructuredTool.from_function(
            name="record_finding",
            description=(
                "记录假设验证结论（VERIFY 阶段验证后必须调用；EVALUATE 阶段亦可调用——"
                "confirmed 强化、次要根因确认、statement_update 精化）。"
                "verdict: confirmed(已证实——须以对应领域专家验证结论为依据（expert 证据），"
                "仅有监控数据不足支撑 confirmed，无专家证据的 confirmed 将被系统拦截；"
                "以 argus 监控专家结论 confirmed 时，结论范围须限定于指标直接可观测的事实"
                "——机制性断言（为何发生）须委派深度专家确证，或在报告中标注为推断；"
                "可同时存在多个确认的根因/加剧因素)"
                " | refuted(证据明确证伪，该假设不成立——监控反证即可直接判定)"
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
                "纯表述修正（迭代假设更新）：仅修正某个未决(pending)假设的表述而不落结论时，"
                "可省略 verdict、只填 statement_update——用于验证其他假设后修正剩余未决假设"
                "的过时表述；每个假设最多纯修正 2 次（第 3 次须落 verdict）。"
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
                if can_propose_root_hypotheses(ledger):
                    # Phase derives to hypothesize via T8 (all roots
                    # hard-failed + budget left) — no snapshot write.
                    mw._persist_ledger(ledger)
                    mw._current_ledger = ledger
                    return (
                        "没有可回溯的假设，但假设提出预算未用尽"
                        f"（已用 {ledger.get('_propose_count', 0)}"
                        f"/{MAX_PROPOSE_CALLS} 次）。\n"
                        "已进入 HYPOTHESIZE 阶段。\n"
                        "⛔ 请调用 propose_hypotheses 提出新一批假设——"
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
                    "回溯失败: 没有可回溯的假设，且假设提出预算已用尽"
                    f"（{MAX_PROPOSE_CALLS} 次）。\n"
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
                # Structured expert returns are stored as JSON in
                # key_findings (kept for exact classification); render
                # them as readable text in the report timeline.
                if _parse_expert_json(kf) is not None:
                    kf_short = _format_expert_summary(kf).replace("\n", " ")
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
