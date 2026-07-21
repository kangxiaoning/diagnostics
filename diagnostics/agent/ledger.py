"""Diagnosis ledger data structures, rendering, and serialization.

The ledger is a structured diagnosis memory persisted in LangGraph state.
It holds the hypothesis tree, evidence chain, and diagnosis round history.

See: private/HYPOTHESIS_LEDGER_DESIGN.md
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from typing import Annotated, Any, Literal, NotRequired

from deepagents.graph import DeepAgentState
from langchain.agents.middleware.types import PrivateStateAttr


# ── Phase & status types ──

# ── Phase & status types ──
# 5-phase state machine (private/design/state-machine-v2.md):
# understand → hypothesize → verify → evaluate → report.
# backtrack/skill_verify are NOT phases: backtrack is an EVALUATE action,
# skill mode is delegation guidance inside VERIFY.
DiagnosisPhase = Literal[
    "understand", "hypothesize", "verify", "evaluate", "report",
]

# 5-state model (state-machine-v2.md §5, v2.1 精简):
# - verifying   → removed: the verification focus is active_path[-1]
# - deprioritized → demoted to the ``deferred`` boolean field on the node
# - dead_end    → merged into ``refuted`` with ``terminal_reason`` set
HypothesisStatus = Literal[
    "pending", "confirmed",
    "refuted",     # includes former dead_end (see terminal_reason)
    "inconclusive",  # evidence insufficient, backtrackable
]

# Why a hypothesis was closed without a verdict (refuted sub-classification):
#   "evidence_saturated" — 3 consecutive inconclusive verdicts (former dead_end)
#   "timeout_unreached"  — session interrupted before verification (former dead_end)
#   "auto_finalized"     — closed by a safety valve / report-time finalize
TerminalReason = Literal["evidence_saturated", "timeout_unreached", "auto_finalized"]

Verdict = Literal["confirmed", "refuted", "inconclusive"]

# ── Root-hypothesis batch budget ──
# The LLM may submit at most this many batches of ROOT hypotheses
# (parent_id=None).  A new batch is only allowed after ALL root
# hypotheses from prior batches are hard-failed (refuted/dead_end).
MAX_ROOT_COMMIT_BATCHES = 2

# ── Hypothesis-tree hard invariants (deterministic, code-enforced) ──
# These are BUSINESS invariants — they cannot be expressed by tool-input
# schemas (Pydantic validates a single call, not cumulative tree state),
# so they are enforced here at the data-structure layer.  Rationale and
# evidence: private/design/state-machine-v2.md §11; the 2026-07-20
# 32-round session (root layer grew to 6 via batch accumulation, depth
# reached 2 with unproductive H1.1.x exploration).
MAX_LAYER_SIZE = 3
"""Max sibling hypotheses sharing the same parent (cumulative, not per
commit call).  Root layer obeys the same limit per CURRENT batch —
a retry batch REPLACES the current root layer instead of appending."""

import os as _os
MAX_DEPTH = int(_os.getenv("DIAGNOSTICS_MAX_DEPTH", "2"))
"""Max hypothesis-tree depth.  0=root, 1=sub, 2=grandchild.  Depth-2
nodes are leaves by construction; deeper refinement must go through
record_finding (sharpen the statement) instead of a new sub-hypothesis."""

# Statuses that permanently close a hypothesis with a negative verdict.
# (dead_end merged into refuted in the 5-state model.)
_HARD_FAILED_STATUSES = frozenset({"refuted"})

# ── Quantified exit model (state-machine-v2.md §6, v2.1) ──
# 退出时机 = 继续验证的期望信息增益 < 成本，且当前把握足够。
# Value of a verification action: value(a) = EIG(a) / c(a), where
#   EIG(a) = U(h) × D(a,h) × F(a,h)
#     U(h)   residual uncertainty of the target hypothesis
#            = 4·p·(1-p), p = probability/100  (1.0 at p=50, ~0 at extremes)
#     D(a,h) discriminativeness: 1.0 targeted delegation / 0.5 indirect
#     F(a,h) freshness decay = 0.5^n_prior_delegations(h) (inconclusive
#            verdicts add an extra halving each — D6)
#   c(a)   cost in round-equivalents (D3): delegation 2.0 / state-op 0.5 /
#          batch commit 1.0 / direct query 0.3
# Exit requires BOTH:
#   S1 (confidence sufficient): a confirmed root cause with p ≥ 80 AND
#       residual uncertainty R < R_THRESHOLD, where
#       R = Σ U(h) over undecided h (deferred nodes excluded — D4)
#   S2 (gain < cost): max available action value < KAPPA_STOP
# Fast lanes (kept from v2): E2 exhaustion, E3 saturation, E4 quick path.
KAPPA_STOP = 0.15
"""Stop threshold on max action value (D2).  Calibrated so a targeted
delegation is allowed ~2 times on the same hypothesis (0.25 > κ) but a
3rd is blocked (0.125 < κ)."""

R_THRESHOLD = 0.8
"""Max residual uncertainty allowed for S1 (D4).  A single fresh p=50
hypothesis contributes U=1.0, so R<0.8 tolerates at most one partially
resolved side hypothesis (U≈0.64 at p=20/80)."""

COST_DELEGATION = 2.0
COST_STATE_OP = 0.5
COST_BATCH_COMMIT = 1.0
COST_QUERY = 0.3

# LLMs sometimes prefix hypothesis statements with the ledger ID they
# expect (e.g. "H1: 磁盘故障"), producing doubled prefixes in rendering
# ("H1 [refuted] H1: 磁盘故障") and guidance ("聚焦验证 H1: H1: ...").
# Strip such prefixes at commit time.  A trailing separator is required
# so legitimate content like "H100 交换机故障" is left untouched.
_STATEMENT_ID_PREFIX_RE = re.compile(r"^\s*H\d+(?:\.\d+)?\s*[:：.．、]\s*")


# ── TypedDicts (plain dicts at runtime, typed for clarity) ──

class Evidence(dict):
    """单条证据。"""


class HypothesisNode(dict):
    """假设树节点。"""


class RoundRecord(dict):
    """单个诊断步骤的记录。"""


class DiagnosisLedger(dict):
    """诊断台账 — Agent 的结构化诊断记忆。"""


# ── State schema ──

class DiagnosisLedgerState(DeepAgentState):
    """Extend deepagents state with a private diagnosis ledger field."""

    _diagnosis_ledger: Annotated[
        NotRequired[DiagnosisLedger | None],
        PrivateStateAttr,
    ]


# ── Factory functions ──

def new_ledger(entity_type: str = "", hostname: str = "",
               entity_name: str = "") -> DiagnosisLedger:
    """Create an empty diagnosis ledger.

    *entity_type* is used by the expert-coverage gate in
    ``_derive_default_phase`` to determine which Argus experts
    are available for delegation (``"host"`` → only host-argus-expert;
    default/container → both host-argus-expert and k8s-argus-expert).

    *hostname* is the resolved host node name for the target
    workload (container scenarios) or the user-provided hostname
    (host scenarios).  It is injected into the ``host-argus-expert``
    subagent context via ``_build_subagent_context`` so the subagent
    LLM knows which host to query without asking the Coordinator.

    *entity_name* is the cluster/host name from the frontend
    (e.g. ``"prod-us-east"``).  It is injected into the
    ``k8s-argus-expert`` subagent context so the subagent LLM
    knows which cluster to query without asking the Coordinator.
    """
    # NOTE: no ``current_phase`` field — the phase is derived on demand
    # by ``derive_phase`` (single source of truth).  Legacy ledger JSON
    # files that still carry the key are tolerated: readers must ignore it.
    return DiagnosisLedger(
        hypotheses={},
        root_hypothesis_ids=[],
        active_path=[],
        current_round=0,
        rounds=[],
        root_cause=None,               # backward-compat: first confirmed root cause
        root_cause_hypothesis_id=None,  # backward-compat
        root_causes=[],                # list[str] — all confirmed root causes
        root_cause_hypothesis_ids=[],  # list[str] — matching hypothesis IDs
        report=None,
        exhausted=False,
        skill_mode=False,
        skill_ids=[],
        tool_results={},               # cache_key → {path, round, preview, lines, is_failure}
        _inconclusive_streak=0,
        _backtrack_count=0,
        _root_commit_count=0,
        entity_type=entity_type,
        hostname=hostname,
        entity_name=entity_name,
    )


def new_hypothesis(
    hid: str,
    statement: str,
    probability: int,
    parent_id: str | None,
    depth: int,
    round_num: int,
    rationale: str = "",
) -> HypothesisNode:
    return HypothesisNode(
        id=hid,
        parent_id=parent_id,
        depth=depth,
        statement=statement,
        probability=max(0, min(100, probability)),
        status="pending",
        evidence=[],
        verification_tools=[],
        sub_hypothesis_ids=[],
        selected=False,
        rationale=rationale,
        created_at_round=round_num,
        deferred=False,          # v2.1: scheduling mark (ex-deprioritized)
        deferred_reason="",      # why the LLM shelved this hypothesis
        terminal_reason="",      # refuted sub-classification (ex-dead_end)
        _delegate_count=0,       # successful expert delegations targeting it
        _inconclusive_count=0,   # inconclusive verdicts on this node (D6)
    )


def new_evidence(
    source: str,
    summary: str,
    supports: bool | None,
    tool_call_id: str | None = None,
) -> Evidence:
    """Create an evidence entry.

    ``supports`` is tri-state: True (supports the hypothesis), False
    (refutes it), or None (undetermined — used for auto-collected tool
    outputs whose verdict is unknown without coordinator assessment).
    """
    return Evidence(
        source=source,
        summary=summary[:300],
        supports=supports,
        tool_call_id=tool_call_id,
        timestamp=datetime.now(UTC).isoformat(),
    )


# ── ID generation ──

def _norm_parent_id(parent_id: str | None) -> str | None:
    """Normalize parent_id so empty string is treated as None.

    LLMs may pass ``""`` for optional parameters instead of omitting them.
    This helper prevents KeyError on ``hypotheses[""]`` in all code paths
    that read ``parent_id`` from a hypothesis node.
    """
    if not parent_id:
        return None
    return parent_id


def _next_hypothesis_id(parent_id: str | None, hypotheses: dict[str, HypothesisNode]) -> str:
    """Generate next hypothesis ID like H1, H2, H1.1, H1.2.

    Scans existing IDs to avoid collisions when called multiple times
    for the same parent within a single commit_hypotheses invocation.
    """
    if parent_id is None:
        existing_top = [h for h in hypotheses.values() if h["parent_id"] is None]
        next_num = len(existing_top) + 1
        # Avoid collision: skip numbers already taken
        while f"H{next_num}" in hypotheses:
            next_num += 1
        return f"H{next_num}"
    parent = hypotheses.get(parent_id)
    if parent is None:
        return "H1"
    count = len(parent.get("sub_hypothesis_ids", []))
    candidate = f"{parent_id}.{count + 1}"
    # Avoid collision: increment suffix until unique
    suffix = count + 1
    while candidate in hypotheses:
        suffix += 1
        candidate = f"{parent_id}.{suffix}"
    return candidate


# ── Hypothesis tree operations ──

def can_commit_root_hypotheses(ledger: DiagnosisLedger) -> bool:
    """Check if a new batch of ROOT hypotheses may be submitted.

    Budget rules (see ``MAX_ROOT_COMMIT_BATCHES``, v2.1 H2 relaxed):

    - First batch: always allowed (no root hypotheses yet).
    - Subsequent batches: allowed when the current batch has NO confirmed
      hypothesis AND no ACTIVE pending hypothesis.  Refuted, inconclusive,
      and deferred hypotheses do NOT block a retry batch — deferred /
      inconclusive nodes stay backtrackable but must not force the LLM to
      "bury" them one by one before changing direction (observed
      2026-07-21 session: round-8 batch rejection + 4 wasted rounds of
      ritual verification on a deprioritized H3).
    """
    if ledger.get("_root_commit_count", 0) >= MAX_ROOT_COMMIT_BATCHES:
        return False
    root_ids = ledger.get("root_hypothesis_ids", [])
    if not root_ids:
        return True
    hypotheses = ledger.get("hypotheses", {})
    for hid in root_ids:
        node = hypotheses.get(hid)
        if not node:
            continue
        if node.get("status") == "confirmed":
            return False
        if node.get("status") == "pending" and not node.get("deferred"):
            return False
    return True


def add_hypotheses(
    ledger: DiagnosisLedger,
    hypothesis_specs: list[dict],
    parent_id: str | None,
) -> list[HypothesisNode]:
    """Add up to 3 hypotheses under a parent. Returns created nodes.

    Existing hypotheses with the same statement (trimmed + lowercased)
    are rejected to prevent duplicate creation.  Use ``record_finding``
    to update an existing hypothesis's probability or verdict.
    """
    if len(hypothesis_specs) > 3:
        raise ValueError(
            f"最多3个假设，收到 {len(hypothesis_specs)} 个。请只提交可能性最大的3个。"
        )

    # ── Strip self-assigned ID prefixes ("H1: ...") from statements ──
    # Applied before dedup so that "H1: 磁盘故障" and "磁盘故障" are
    # recognized as duplicates, and before node creation so rendering
    # never shows doubled prefixes.
    for spec in hypothesis_specs:
        stmt = spec.get("statement", "")
        cleaned = _STATEMENT_ID_PREFIX_RE.sub("", stmt).strip()
        if cleaned:
            spec["statement"] = cleaned

    created: list[HypothesisNode] = []
    hypotheses = ledger["hypotheses"]

    # ── Normalize parent_id ──
    # LLMs may pass an empty string "" for an optional parameter instead
    # of omitting it.  Treat falsy values (None, "") uniformly as
    # "root-level hypothesis" to avoid KeyError on hypotheses[""].
    if not parent_id:
        parent_id = None

    parent_depth = hypotheses[parent_id]["depth"] if parent_id else -1

    # ── Hard invariant: depth limit ──
    # Reject ANY commit (first batch, retry, or deepening) that would
    # exceed MAX_DEPTH.  Checked before dedup/budget so a rejected call
    # consumes nothing.
    if parent_depth + 1 > MAX_DEPTH:
        raise ValueError(
            f"假设树已达最大深化层级（{MAX_DEPTH} 层），禁止继续细化。"
            "请调用 record_finding 用 statement_update 修正当前假设表述，"
            "或评估是否满足退出条件进入 REPORT。"
        )

    # ── Hard invariant: cumulative layer-size limit (non-root) ──
    # Root layer is handled separately below (retry batch REPLACES the
    # current root layer).  For sub-hypotheses, the TOTAL sibling count
    # under one parent must not exceed MAX_LAYER_SIZE — the per-call
    # check above alone would allow 3+3+… commits under one parent.
    if parent_id is not None:
        current_siblings = len(hypotheses[parent_id]["sub_hypothesis_ids"])
        if current_siblings + len(hypothesis_specs) > MAX_LAYER_SIZE:
            raise ValueError(
                f"父假设 {parent_id} 下已有 {current_siblings} 个子假设，"
                f"本次提交 {len(hypothesis_specs)} 个将超过每层上限"
                f"（{MAX_LAYER_SIZE} 个）。请只提交可能性最高的子假设，"
                "或先用 record_finding 终结已有的低价值子假设。"
            )

    # Detect duplicate statements — only scan root-level hypotheses
    # (parent_id is None), since sub-hypotheses naturally contain
    # parent context.  Refuted/dead_end hypotheses also participate in
    # dedup so a retry batch cannot re-submit a failed hypothesis with
    # identical wording.
    if parent_id is None:
        existing_root = {
            h["statement"].strip().lower(): h
            for h in hypotheses.values()
            if h["parent_id"] is None
        }
        for spec in hypothesis_specs:
            norm = spec["statement"].strip().lower()
            dup = existing_root.get(norm)
            if dup is not None:
                if dup.get("status") in _HARD_FAILED_STATUSES:
                    raise ValueError(
                        f"假设 '{spec['statement'][:80]}' 与已排除假设 "
                        f"{dup['id']} 重复。新一批假设必须基于排除证据"
                        "换方向，禁止重复或仅换措辞。"
                    )
                raise ValueError(
                    f"假设 '{spec['statement'][:80]}' 已存在。"
                    f"如需更新其概率或结论，请使用 record_finding 工具，"
                    f"而非 commit_hypotheses 重复提交。"
                )

        # ── Root-batch budget ──
        # Checked AFTER dedup so a rejected duplicate does not consume
        # budget.  Deepening commits (parent_id given) never consume it.
        if not can_commit_root_hypotheses(ledger):
            if ledger.get("_root_commit_count", 0) >= MAX_ROOT_COMMIT_BATCHES:
                raise ValueError(
                    f"根假设批次预算已用尽（{MAX_ROOT_COMMIT_BATCHES} 批）。"
                    "请基于已有假设的验证结果生成报告。"
                )
            raise ValueError(
                "当前批仍有活跃 pending 或已 confirmed 的根假设，不可换批。"
                "请先用 record_finding 终结活跃假设，或用 select_path "
                "切换验证焦点（deferred/inconclusive 假设不阻塞换批）。"
            )
        ledger["_root_commit_count"] = ledger.get("_root_commit_count", 0) + 1

        # ── Hard invariant: root layer = CURRENT batch only ──
        # A retry batch REPLACES the root layer instead of appending to
        # it.  Prior-batch hypotheses stay in ``hypotheses`` (rendered
        # under "已排除/降级假设") but no longer count as the current
        # root layer — otherwise two batches accumulate to 6 roots and
        # the "layer ≤ 3" invariant breaks (observed 2026-07-20 session:
        # H1-H3 + H4-H6 coexisting as the root layer).
        # First batch (no existing roots or all stale) keeps append
        # semantics via a fresh list.
        ledger["root_hypothesis_ids"] = []

    for spec in hypothesis_specs:
        hid = _next_hypothesis_id(parent_id, hypotheses)
        node = new_hypothesis(
            hid=hid,
            statement=spec["statement"],
            probability=spec.get("probability", 50),
            parent_id=parent_id,
            depth=parent_depth + 1,
            round_num=ledger["current_round"],
            rationale=spec.get("rationale", ""),
        )
        hypotheses[hid] = node
        created.append(node)

        if parent_id is None:
            ledger["root_hypothesis_ids"].append(hid)
        else:
            parent = hypotheses[parent_id]
            parent["sub_hypothesis_ids"].append(hid)

    # Auto-select the highest probability as the verification focus.
    # Clear stale selected markers first so exactly one node carries the
    # ★ marker — nodes from a prior batch or a refuted path must not stay
    # selected.  (v2.1: no "verifying" status — focus == selected flag.)
    if created:
        for n in hypotheses.values():
            n["selected"] = False
        best = max(created, key=lambda h: h["probability"])
        best["selected"] = True
        best["deferred"] = False
        if parent_id is None:
            # New root batch: reset the active path — a stale path from
            # a failed batch must not prefix the new root hypothesis.
            ledger["active_path"] = [best["id"]]
        else:
            ledger["active_path"].append(best["id"])

    return created


def select_path(
    ledger: DiagnosisLedger,
    selected_id: str,
    rationale: str = "",
    deprioritized: list[dict] | None = None,
) -> None:
    """Select one hypothesis to deepen, mark others as deferred."""
    if deprioritized is None:
        deprioritized = []
    hypotheses = ledger["hypotheses"]
    if selected_id not in hypotheses:
        raise ValueError(f"假设 {selected_id} 不存在")

    # Mark selected.  Deferred hypotheses are re-activatable — selecting
    # one clears the shelved mark.  Clear all other selected markers
    # first so exactly one node carries ★.
    for n in hypotheses.values():
        n["selected"] = False
    selected = hypotheses[selected_id]
    selected["selected"] = True
    selected["deferred"] = False
    if rationale:
        selected["rationale"] = rationale

    # Update active_path: remove siblings, keep ancestors
    parent_id = _norm_parent_id(selected["parent_id"])
    siblings: list[str] = []
    if parent_id is None:
        siblings = ledger["root_hypothesis_ids"]
    else:
        siblings = hypotheses[parent_id]["sub_hypothesis_ids"]

    # Rebuild active_path: ancestors only
    new_path: list[str] = []
    cursor = selected_id
    while cursor:
        new_path.insert(0, cursor)
        node = hypotheses[cursor]
        cursor = _norm_parent_id(node["parent_id"])
    ledger["active_path"] = new_path

    # Mark non-selected siblings as deferred (v2.1: flag, not status)
    for sid in siblings:
        if sid == selected_id:
            continue
        sibling = hypotheses[sid]
        if sibling["status"] == "pending":
            sibling["deferred"] = True
            sibling["selected"] = False
    # Apply explicit defer reasons
    for dep in deprioritized:
        dep_id = dep.get("id", "")
        if dep_id in hypotheses:
            hypotheses[dep_id]["deferred"] = True
            hypotheses[dep_id]["selected"] = False
            hypotheses[dep_id]["deferred_reason"] = dep.get("reason", "")


def activate_hypothesis(ledger: DiagnosisLedger, hid: str) -> None:
    """Activate a hypothesis for verification (auto select_path semantics).

    Used when the Coordinator delegates task() verification targeting a
    pending / deferred / inconclusive hypothesis without calling
    ``select_path`` first.  The ledger's invariant "active hypothesis ==
    hypothesis under verification" is restored automatically.

    Unlike ``select_path``, this does NOT defer siblings: the
    Coordinator's intent is verification, not path pruning — multi-root
    scenarios verify sibling root hypotheses one by one, and deferring
    them would misrepresent that intent in the ledger.
    """
    hypotheses = ledger["hypotheses"]
    node = hypotheses.get(hid)
    if node is None:
        return
    for n in hypotheses.values():
        n["selected"] = False
    node["selected"] = True
    node["deferred"] = False

    # Rebuild active_path: ancestors + self
    new_path: list[str] = []
    cursor: str | None = hid
    while cursor:
        new_path.insert(0, cursor)
        cursor = _norm_parent_id(hypotheses[cursor]["parent_id"])
    ledger["active_path"] = new_path


def record_finding(
    ledger: DiagnosisLedger,
    hypothesis_id: str,
    verdict: Verdict,
    evidence_summary: str,
    probability_update: int,
    new_insights: str = "",
    statement_update: str = "",
) -> None:
    """Record a verification finding for a hypothesis.

    Args:
        statement_update: If provided, replaces the hypothesis statement
            with a more accurate version discovered during verification
            (e.g. "etcd compaction" → "etcd NOSPACE alarm").
    """
    hypotheses = ledger["hypotheses"]
    if hypothesis_id not in hypotheses:
        raise ValueError(f"假设 {hypothesis_id} 不存在")

    node = hypotheses[hypothesis_id]
    node["probability"] = max(0, min(100, probability_update))

    # Update statement if a more accurate version was found during verification
    if statement_update:
        node["statement"] = statement_update

    supports = verdict == "confirmed"
    if evidence_summary:
        node["evidence"].append(new_evidence(
            source="coordinator",
            summary=evidence_summary,
            supports=supports,
        ))

    if verdict == "confirmed":
        node["status"] = "confirmed"
        # verdict_reason records WHY the hypothesis was closed.  It is
        # rendered in the "已排除假设"/理由 sections instead of the
        # creation-time rationale, which would read as self-contradictory
        # (showing the original supporting argument as the reason for
        # exclusion).
        node["verdict_reason"] = evidence_summary
        node["selected"] = False
        node["deferred"] = False
        ledger["_inconclusive_streak"] = 0
    elif verdict == "refuted":
        node["status"] = "refuted"
        node["verdict_reason"] = evidence_summary
        node["selected"] = False  # terminal — no longer the focus
        node["deferred"] = False
        ledger["_inconclusive_streak"] = 0
    else:  # inconclusive
        ledger["_inconclusive_streak"] += 1
        node["_inconclusive_count"] = node.get("_inconclusive_count", 0) + 1
        node["status"] = "inconclusive"
        node["selected"] = False
        # After 3+ consecutive inconclusive (global streak), hard-close
        # as refuted + evidence_saturated (former dead_end) to break the
        # verify loop — retrying the same data is futile.
        if ledger["_inconclusive_streak"] >= 3:
            node["status"] = "refuted"
            node["terminal_reason"] = "evidence_saturated"
            node["verdict_reason"] = evidence_summary

    if new_insights:
        node["rationale"] = (node.get("rationale", "") + " | 新发现: " + new_insights).strip(" |")


def add_evidence_to_active(
    ledger: DiagnosisLedger,
    source: str,
    summary: str,
    supports: bool | None = None,
    tool_call_id: str | None = None,
    tool_name: str | None = None,
) -> None:
    """Add evidence to the current active hypothesis (auto from tool calls)."""
    if not ledger["active_path"]:
        return
    active_id = ledger["active_path"][-1]
    node = ledger["hypotheses"].get(active_id)
    if node is None:
        return
    node["evidence"].append(new_evidence(source, summary, supports, tool_call_id))
    if tool_name and tool_name not in node["verification_tools"]:
        node["verification_tools"].append(tool_name)


# ── Phase derivation ──
# 5-phase state machine (private/design/state-machine-v2.md §3).
# The phase is NEVER persisted: ``derive_phase`` is the single source of
# truth, called by rendering, tool gating, and guardrails alike.
#
# Transition table (normal flow):
#
#   | from        | event / guard                                    | to          |
#   |-------------|--------------------------------------------------|-------------|
#   | understand  | T1  coverage ready (system)                      | hypothesize |
#   | hypothesize | T10 commit_hypotheses success                    | verify      |
#   | verify      | T3  record_finding                               | evaluate    |
#   | evaluate    | T4  exit conditions E1-E4                        | report      |
#   | evaluate    | T5  select_path on pending/deprioritized/…       | verify      |
#   | evaluate    | T7  backtrack target exists                      | verify      |
#   | evaluate    | T8  layer all-hard-failed + budget left          | hypothesize |
#   | evaluate    | T9  all-hard-failed + budget exhausted (= E2)    | report      |
#   | report      | T11 write_file done → terminal summary mode      | report      |
#
# Forced transitions (guardrails, any phase): max-rounds / model-timeout /
# verify-stuck / REPORT-stall / empty-data → ``report`` (pending hypotheses
# auto-finalized by ``finalize_pending_for_report``).

def derive_phase(ledger: DiagnosisLedger) -> DiagnosisPhase:
    """Derive the current diagnosis phase from ledger state (pure function).

    Decision order (fixed, state-machine-v2.md §3):
      1. REPORT terminal lock — report already written.
      2. No hypotheses yet → understand (coverage not ready) or
         hypothesize (coverage ready).
      3. Active hypothesis still verifying → verify.
      4. EVALUATE: the direction is decided HERE, not left to the LLM —
         retry-batch (T8) and deepening (T6) precede the exit check so a
         last-standing failed root (root_causes empty) routes to
         hypothesize instead of being shadowed by the legacy exhaustion
         exit; only genuinely terminal states fall through to REPORT.
      5. Exit conditions E1-E4 → report; otherwise evaluate.
    """
    # 1. REPORT terminal lock.
    if ledger.get("report"):
        return "report"

    hypotheses = ledger.get("hypotheses", {})

    # 2. No hypotheses yet → understand / hypothesize by coverage gate.
    if not hypotheses:
        if _coverage_ready(ledger):
            return "hypothesize"
        return "understand"

    # 3. Active hypothesis under verification → verify.
    # (v2.1: the focus is active_path[-1] carrying selected=True with a
    # non-terminal status — there is no "verifying" status anymore.)
    if ledger["active_path"]:
        active = hypotheses.get(ledger["active_path"][-1])
        if (active and active.get("selected")
                and active.get("status") in ("pending", "inconclusive")):
            return "verify"

    # 4. EVALUATE direction split (BEFORE exit check):
    #    T8 retry batch — all root hypotheses refuted and budget left.
    #    A refuted layer with no resumable sibling can only move forward
    #    via a fresh batch (or exit when the budget is gone); presenting
    #    "evaluate" here would offer no executable action.
    #    Exception: E3 (evidence saturation) always wins — a ledger that
    #    hit 3 consecutive inconclusive verdicts exits to REPORT even if
    #    the active node was subsequently hard-closed (the streak marks
    #    evidence exhaustion, retrying the same data is futile).
    root_ids = ledger.get("root_hypothesis_ids", [])
    if (
            root_ids
            and ledger.get("_inconclusive_streak", 0) < 3
            and all(
                hypotheses.get(hid, {}).get("status") in _HARD_FAILED_STATUSES
                for hid in root_ids)):
        if can_commit_root_hypotheses(ledger):
            return "hypothesize"
        return "report"  # T9 == E2

    #    T6 deepening — exit conditions not met but actionable
    #    sub-hypotheses exist under a confirmed node (the confirmed
    #    root itself already yielded its verdict; work continues below
    #    it).  Only route to hypothesize when NO sub-hypothesis exists
    #    yet (otherwise the pending sub would be verifying → step 3).
    #    When exit conditions ARE met, step 5 wins (report).

    # 5. Exit conditions E1-E4 → report; otherwise evaluate.
    if ledger.get("exhausted"):
        return "report"
    should_exit, _reason, _cid = check_exit_conditions(ledger)
    if should_exit:
        return "report"
    return "evaluate"


def _coverage_ready(ledger: DiagnosisLedger) -> bool:
    """UNDERSTAND→HYPOTHESIZE gate (T1): required Argus data collected.

    Rules (see state-machine-v2.md §4):
    - At least 2 rounds have passed (delegation needs time to return).
    - If any task() delegation happened: host scenarios require
      host-argus-expert; container scenarios require BOTH host-argus-expert
      and k8s-argus-expert.
    - If no delegation happened at all, coverage is considered ready
      after the 2-round grace (the LLM may legitimately diagnose
      without Argus in data-free scenarios).
    """
    if ledger.get("current_round", 0) < 2:
        return False
    experts_delegated: set[str] = set()
    has_delegated = False
    for rd in ledger.get("rounds", []):
        experts_delegated.update(rd.get("delegated_experts", []))
        if "task" in rd.get("tools_called", []):
            has_delegated = True
    if not has_delegated:
        return True
    if ledger.get("entity_type", "") == "host":
        return "host-argus-expert" in experts_delegated
    return ("host-argus-expert" in experts_delegated
            and "k8s-argus-expert" in experts_delegated)


def finalize_pending_for_report(ledger: DiagnosisLedger) -> bool:
    """Force-finalize non-terminal hypotheses when entering REPORT phase.

    When the system transitions to REPORT (via derive_phase, timeout,
    max-rounds, or any safety valve), some hypotheses may remain
    undecided.  This function shelves them (deferred=True, selected
    cleared) so the ledger is clean when the report is generated — the
    report's "未验证假设" section lists them with their defer reasons.

    Returns True if any hypotheses were finalized.
    """
    hypotheses = ledger.get("hypotheses", {})
    if not hypotheses:
        return False

    finalized = False
    for hid, node in hypotheses.items():
        if node.get("status") in ("pending", "inconclusive"):
            node["deferred"] = True
            node["selected"] = False
            if not node.get("deferred_reason"):
                node["deferred_reason"] = (
                    "[系统] REPORT 阶段自动搁置：验证未完成"
                )
            finalized = True

    return finalized


def _get_current_layer(ledger: DiagnosisLedger) -> list[str]:
    """Get hypothesis IDs at the current exploration depth."""
    if not ledger["active_path"]:
        return ledger["root_hypothesis_ids"]
    active_id = ledger["active_path"][-1]
    node = ledger["hypotheses"].get(active_id)
    if node and node["sub_hypothesis_ids"]:
        return node["sub_hypothesis_ids"]
    # Check siblings at same depth
    parent_id = _norm_parent_id(node["parent_id"]) if node else None
    if parent_id is None:
        return ledger["root_hypothesis_ids"]
    return ledger["hypotheses"][parent_id]["sub_hypothesis_ids"]


def _can_backtrack(ledger: DiagnosisLedger) -> bool:
    """Check if there are deferred, pending, or inconclusive nodes to backtrack to."""
    for node in ledger["hypotheses"].values():
        if node["status"] in ("pending", "inconclusive"):
            return True
    return False


def _has_pending_sub_hypotheses(ledger: DiagnosisLedger) -> bool:
    """Check if any confirmed root hypothesis has actionable sub-hypotheses.

    Returns True if there exists a sub-hypothesis under a confirmed root
    that still needs verification (not terminal, not low-probability pending).
    Prevents premature REPORT entry when root causes are confirmed but
    sub-hypotheses (deepening analysis) remain unverified.
    """
    hypotheses = ledger.get("hypotheses", {})
    confirmed_ids = set(ledger.get("root_cause_hypothesis_ids", []))
    for cid in confirmed_ids:
        node = hypotheses.get(cid)
        if not node:
            continue
        for sub_id in node.get("sub_hypothesis_ids", []):
            sub = hypotheses.get(sub_id)
            if not sub:
                continue
            status = sub.get("status")
            if status in ("refuted", "inconclusive"):
                continue
            if status == "pending" and (
                    sub.get("deferred") or sub.get("probability", 0) <= 15):
                continue
            return True
    return False


def _inconclusive_exhausted(ledger: DiagnosisLedger) -> bool:
    """Check if evidence is saturated (3+ consecutive inconclusive)."""
    return ledger.get("_inconclusive_streak", 0) >= 3


def backtrack(ledger: DiagnosisLedger) -> str | None:
    """Backtrack to the nearest deferred, pending, or inconclusive hypothesis.

    Returns new active ID or None if exhausted.
    """
    ledger["_backtrack_count"] += 1

    # Walk up the active path to find a node with backtrackable siblings
    path = list(ledger["active_path"])
    while path:
        current_id = path[-1]
        current = ledger["hypotheses"].get(current_id)
        if current is None:
            path.pop()
            continue
        parent_id = _norm_parent_id(current["parent_id"])

        # Look for deferred / pending / inconclusive siblings
        if parent_id is None:
            siblings = ledger["root_hypothesis_ids"]
        else:
            siblings = ledger["hypotheses"][parent_id]["sub_hypothesis_ids"]

        for sid in siblings:
            sibling = ledger["hypotheses"][sid]
            if sibling["status"] in ("pending", "inconclusive"):
                # Found a backtrack target — re-activate (clear shelved mark)
                sibling["selected"] = True
                sibling["deferred"] = False
                sibling["deferred_reason"] = ""
                # Rebuild active path
                new_path: list[str] = []
                cursor = sid
                while cursor:
                    new_path.insert(0, cursor)
                    cursor = _norm_parent_id(ledger["hypotheses"][cursor]["parent_id"])
                ledger["active_path"] = new_path
                return sid

        path.pop()

    # No backtrack target found.  NOTE: the caller (backtrack tool)
    # decides the outcome — retry budget remaining → hypothesize;
    # budget exhausted → set ledger["exhausted"] and go to report.
    return None


def _clean_evidence_text(text: str, max_chars: int = 250) -> str:
    """Flatten markdown in evidence/reason text for tree rendering.

    Expert outputs contain ``##``/``###`` headings and ``**`` bold
    markers that break the ledger's own heading hierarchy when embedded
    verbatim.  Headings are stripped, whitespace collapsed to a single
    line, and the result truncated.
    """
    import re as _re
    cleaned = _re.sub(r"^#+\s*", "", text, flags=_re.MULTILINE)
    cleaned = cleaned.replace("**", "")
    cleaned = " ".join(cleaned.split())
    if len(cleaned) > max_chars:
        cleaned = cleaned[:max_chars] + "…"
    return cleaned


def _support_tag(supports: bool | None) -> str:
    """Render the tri-state support flag of an evidence entry."""
    if supports is True:
        return "支持"
    if supports is False:
        return "反驳"
    return "未定性"


# ── Context rendering (injected into system message each round) ──

# Scaffolding tools that are NOT diagnostic — excluded from history rendering
_SCAFFOLDING_TOOLS_BUILTIN = frozenset({
    "write_file", "read_file", "edit_file",
    "write_todos", "read_todos",
    "ls", "glob", "grep",
    "commit_hypotheses", "select_path", "record_finding", "backtrack",
    "list_diagnostic_capabilities", "get_system_overview",
})

def render_ledger_context(ledger: DiagnosisLedger | None,
                          report_path: str = "",
                          start_time: str = "",
                          end_time: str = "") -> str:
    """Render a compact, structured summary of the ledger for LLM context."""
    if not ledger:
        return ""

    lines: list[str] = ["<diagnosis_ledger>"]

    # ── Session parameters (user-provided, use as authoritative time range) ──
    if start_time or end_time:
        lines.append("## 诊断会话参数（用户已指定，禁止自行推断）")
        if start_time:
            lines.append(f"- 开始时间: {start_time}")
        if end_time:
            lines.append(f"- 结束时间: {end_time}")
        if not start_time or not end_time:
            lines.append("- 若未指定，使用最近 6 小时作为默认时间窗口")
        lines.append("")

    # Current status — phase is always derived (single source of truth);
    # legacy ledgers may still carry a stale ``current_phase`` key which
    # is deliberately ignored here.
    phase = derive_phase(ledger)
    active_path_str = " → ".join(ledger["active_path"]) if ledger["active_path"] else "(无)"
    batch_info = ""
    if ledger.get("_root_commit_count"):
        batch_info = (
            f" | 根假设批次: {ledger['_root_commit_count']}"
            f"/{MAX_ROOT_COMMIT_BATCHES}"
        )
    lines.append(f"## 当前诊断状态")
    lines.append(f"- 阶段: {phase} | 步骤: {ledger['current_round']}{batch_info} | 活动路径: {active_path_str}")
    lines.append("")

    # Hypothesis tree
    if ledger["hypotheses"]:
        lines.append("## 假设树")
        _render_hypothesis_tree(lines, ledger, ledger["root_hypothesis_ids"], indent=0)
        lines.append("")

    # Refuted / deferred hypotheses summary.  Tags are rendered
    # explicitly: deferred (shelved) hypotheses are NOT excluded — they
    # remain backtrackable, and presenting them as "已排除" biases the
    # LLM against revisiting them (observed in the 2026-07-19
    # conntrack_and_oom session where a p=30% shelved hypothesis was
    # rendered as excluded).
    _TERMINAL_REASON_TAG = {
        "evidence_saturated": "证据饱和",
        "timeout_unreached": "未及验证(超时)",
        "auto_finalized": "系统自动关闭",
    }
    refuted = [h for h in ledger["hypotheses"].values()
               if h["status"] == "refuted"]
    deferred = [h for h in ledger["hypotheses"].values()
                if h.get("deferred") and h["status"] != "refuted"]
    if refuted or deferred:
        lines.append("## 已排除/搁置假设")
        for h in refuted:
            # verdict_reason (written by record_finding at terminal
            # verdict) is the actual exclusion reason.  Falling back to
            # rationale would show the *creation* basis — the original
            # supporting argument — as the reason for exclusion, which
            # is self-contradictory.
            reason = (h.get("verdict_reason") or h.get("rationale", "")
                      or "(无记录)")
            tag = "已证伪"
            tr = h.get("terminal_reason")
            if tr:
                tag = _TERMINAL_REASON_TAG.get(tr, "已证伪")
            lines.append(
                f"- [{tag}] {h['id']} {h['statement']}: "
                f"{_clean_evidence_text(reason, 200)}"
            )
        for h in deferred:
            reason = (h.get("deferred_reason") or h.get("rationale", "")
                      or "(无记录)")
            lines.append(
                f"- [搁置待回溯 p={h['probability']}%] {h['id']} "
                f"{h['statement']}: {_clean_evidence_text(reason, 200)}"
            )
        lines.append("")

    # Root causes (multi-root support)
    root_causes: list[str] = ledger.get("root_causes", [])
    if root_causes:
        if len(root_causes) == 1:
            lines.append(f"## 根因: {root_causes[0]}")
        else:
            lines.append("## 已确认根因")
            for i, rc in enumerate(root_causes, 1):
                lines.append(f"{i}. {rc}")
        lines.append("")
    elif ledger.get("root_cause"):
        lines.append(f"## 根因: {ledger['root_cause']}")
        lines.append("")

    # ── Offloaded tool results: paths for read_file/grep access ──
    # LLM can use read_file/grep to pull specific data from previously
    # executed tools, avoiding redundant re-execution across rounds.
    tool_results = ledger.get("tool_results", {})
    if tool_results:
        lines.append("## 已有工具调用结果 (可通过 read_file/grep 查阅)  ←禁止重复调用")
        for cache_key, info in sorted(
            tool_results.items(), key=lambda x: x[1].get("round", 0)
        ):
            tool_name = cache_key.split(":")[0]
            preview = info.get("preview", "")[:80]
            path = info.get("path", "")
            fail = " ⚠失败" if info.get("is_failure") else ""
            lines.append(
                f"- [{tool_name}] 第{info['round']}轮: {preview}... "
                f"({info.get('lines', '?')}行) → `read_file(path=\"{path}\")`"
                f"{fail}"
            )
        lines.append("")

    # ── Diagnostic history: round-by-round tool call timeline ──
    # Only rendered when there is substantive content.  Coordinator
    # rounds typically contain task() calls only, which previously
    # rendered as a bare "- (含委派专家调用)" line with no actionable
    # information; instead we list the delegated experts explicitly.
    rounds_data = ledger.get("rounds", [])
    if rounds_data:
        # Sort by (round, _seq) to preserve invocation order even
        # when concurrent tool calls complete out of order.
        sorted_rounds = sorted(rounds_data, key=lambda r: (r.get("round", 0), r.get("_seq", 0)))
        round_map: dict[int, list[str]] = {}
        delegated: set[str] = set()
        for rd in sorted_rounds:
            rn = rd.get("round", 0)
            delegated.update(rd.get("delegated_experts", []))
            if rn not in round_map:
                round_map[rn] = []
            for tc in rd.get("tools_called", []):
                if tc not in _SCAFFOLDING_TOOLS_BUILTIN and tc != "task":
                    round_map[rn].append(tc)
        has_tool_rows = any(round_map[rn] for rn in round_map)
        if has_tool_rows or delegated:
            lines.append("## 轮次历史")
            for rn in sorted(round_map.keys()):
                tools = round_map[rn]
                if tools:
                    unique = list(dict.fromkeys(tools))
                    lines.append(f"- 第{rn}轮: {', '.join(unique)}")
            if delegated:
                lines.append(f"- 已委派专家: {', '.join(sorted(delegated))}")
            lines.append("")

    # Current step guidance
    guidance = _phase_guidance(phase, ledger, report_path)
    if guidance:
        lines.append("## 本步要求")
        lines.append(guidance)

    lines.append("</diagnosis_ledger>")
    return "\n".join(lines)


def _render_hypothesis_tree(
    lines: list[str],
    ledger: DiagnosisLedger,
    ids: list[str],
    indent: int,
) -> None:
    """Recursively render the hypothesis tree."""
    prefix = "  " * indent
    for hid in ids:
        node = ledger["hypotheses"].get(hid)
        if node is None:
            continue
        marker = " ★" if node["selected"] else ""
        active_marker = (" ← 当前聚焦" if (
            node["selected"] and node["status"] in ("pending", "inconclusive"))
            else "")
        deferred_tag = " [搁置]" if node.get("deferred") else ""
        lines.append(
            f"{prefix}### {node['id']} [{node['status']}{deferred_tag} "
            f"p={node['probability']}%]{marker} "
            f"{node['statement']}{active_marker}"
        )
        # Evidence.  Once a coordinator verdict exists (verdict_reason
        # set by record_finding), two classes of duplicates are skipped:
        # 1. undetermined auto-collected evidence (raw expert output,
        #    supports=None) — largely duplicates the verdict summary;
        # 2. the record_finding evidence entry itself — its summary IS
        #    the verdict_reason text, already rendered as 理由 (confirmed)
        #    or in the "## 已排除假设" section (negative terminals).
        verdict_text = (node.get("verdict_reason") or "").strip()
        evidence_entries = node["evidence"]
        if verdict_text:
            evidence_entries = [
                e for e in evidence_entries
                if e.get("supports") is not None
                and e.get("summary", "").strip()[:200] != verdict_text[:200]
            ]
        for ev in evidence_entries:
            lines.append(
                f"{prefix}  证据: "
                f"{_clean_evidence_text(ev.get('summary', ''))} "
                f"({_support_tag(ev.get('supports'))})"
            )
        # Verification tools
        if node["verification_tools"]:
            lines.append(f"{prefix}  已验证工具: {', '.join(node['verification_tools'])}")
        # Reason: only confirmed nodes render it here.  Negative terminal
        # nodes (refuted/dead_end/deprioritized) are covered by the
        # "## 已排除假设" section — rendering both would duplicate the
        # same reason text every round.
        if node["status"] == "confirmed":
            reason = node.get("verdict_reason") or node.get("rationale", "")
            if reason:
                lines.append(
                    f"{prefix}  理由: {_clean_evidence_text(reason, 150)}"
                )
        # Sub-hypotheses
        if node["sub_hypothesis_ids"]:
            _render_hypothesis_tree(lines, ledger, node["sub_hypothesis_ids"], indent + 1)


def _phase_guidance(phase: DiagnosisPhase, ledger: DiagnosisLedger,
                     report_path: str = "") -> str:
    """Generate guidance text for the current phase."""
    if phase == "understand":
        return (
            "你当前处于 UNDERSTAND 阶段。\n"
            "- 从用户输入提取故障画像（实体、症状、时间线、变更、已尝试操作）\n"
            "- 委派 Argus 专家时使用上方「诊断会话参数」中的时间范围（禁止自行推断）\n"
            "- 委派 Argus 专家采集监控指标（通过 task() 调用）：\n"
            "  主机相关（CPU/内存/磁盘/网络症状）→ task(\"host-argus-expert\", ...)\n"
            "  K8s 相关（节点/Pod/集群症状）→ task(\"k8s-argus-expert\", ...)\n"
            "  复合场景 → 同时委派两个专家（并行 task），分别负责主机级和集群级指标\n"
            "- 关注指标突变时间点和并发异常（同一分钟多条红线→可能独立根因）\n"
            "- ⚠ 查看上方「已有工具调用结果」，禁止重复调用已执行过的工具\n"
            "- ⚠ 本阶段只采集信息，不提交假设——必需专家数据齐备后"
            "系统自动进入 HYPOTHESIZE，届时再调用 commit_hypotheses"
        )
    if phase == "hypothesize":
        retry_hint = ""
        if ledger.get("_root_commit_count", 0) > 0 and ledger.get("hypotheses"):
            retry_hint = (
                f"\n- ⚠ 这是第 {ledger['_root_commit_count'] + 1}"
                f"/{MAX_ROOT_COMMIT_BATCHES} 批根假设。"
                "前一批已全部证伪（见上方「已排除/降级假设」）。\n"
                "  新假设必须基于排除证据换方向推断，"
                "禁止与已排除假设重复或仅换措辞。"
            )
        return (
            "你当前处于 HYPOTHESIZE 阶段。\n"
            "- 基于当前证据，提出最多3个可能性最大的假设（按概率降序）\n"
            "- 每个假设标注概率(0-100)和依据\n"
            "- 完成后必须调用 commit_hypotheses 提交假设"
            + retry_hint
        )
    if phase == "verify":
        active_id = ledger["active_path"][-1] if ledger["active_path"] else "?"
        active = ledger["hypotheses"].get(active_id, {})
        stmt = active.get("statement", "?")
        return (
            f"你当前处于 VERIFY 阶段，聚焦验证 {active_id}: {stmt}\n"
            "- 主机假设 → task(\"host-expert\", ...) 委派验证\n"
            "- K8s 假设 → task(\"k8s-expert\", ...) 委派验证\n"
            "- GPU 假设 → task(\"gpu-expert\", ...) 委派验证\n"
            "- 委派时明确\"验证假设{active_id}\"并传入假设上下文\n"
            "- 收到结果后必须调用 record_finding 记录结论\n"
            "- ⚠ 若假设仅部分不成立（某环节被证伪但核心机制已确认），用"
            " confirmed + statement_update 修正表述，禁止整体 refuted\n"
            "- ⚠ 查看上方「已有工具调用结果」和假设的「证据」字段，禁止重复调用已有结果的工具\n"
            "- 禁止调用与当前假设无关的工具"
        )
    if phase == "evaluate":
        # Direction is computed by the state machine and presented as
        # THE action for this step — the LLM does not choose freely.
        # Mutually exclusive by construction (state-machine-v2.md §5):
        # exit was already ruled out by derive_phase (evaluate reached),
        # so exactly one of select/deepen/backtrack/retry applies.
        pending_hint = ""
        root_cause_hyp_ids = ledger.get("root_cause_hypothesis_ids", [])
        root_ids = ledger.get("root_hypothesis_ids", [])
        pending = [h for h in root_ids if h not in root_cause_hyp_ids
                   and ledger["hypotheses"].get(h, {}).get("status") != "refuted"]
        if root_cause_hyp_ids and pending:
            pending_hint = f"\n- ⚠ 仍有待验证的假设: {', '.join(pending)}，使用 select_path 切换验证"
        return (
            "你当前处于 EVALUATE 阶段。\n"
            "- 评估本层所有假设的验证结果，选择下一步路径：\n"
            "  · 有待验证假设 → select_path 切换到最可能的继续验证\n"
            "  · confirmed 假设需更具体 → commit_hypotheses(parent_hypothesis_id=...) 提交子假设深化\n"
            "  · 本层全部 refuted 且有搁置假设 → backtrack 回溯恢复\n"
            "  · 本层全部 refuted 且不可回溯、批次预算未用尽 → commit_hypotheses 换方向提交新一批根假设\n"
            "  · 退出条件满足时系统已自动进入 REPORT（无需你判断）\n"
            "- 多根因场景：证据支持的假设即使非主根因也应标记为 confirmed（次要根因/加剧因素），"
            "refuted 仅用于证据明确证伪的假设。多个假设可同时为 confirmed。\n"
            "- ⚠ 概率判断原则：若核心因果关系已由证据确认"
            "（如「异常进程占满资源→服务响应超时」），"
            "仅次要细节未知（如触发者、启动源），"
            "应保持高概率(≥80%)。"
            "不要因为次要信息缺失而过度降低根因概率。"
            + pending_hint
        )
    if phase == "report":
        n_causes = len(ledger.get("root_causes", []))
        multi_hint = (
            f"\n- 已确认 {n_causes} 个根因，报告中需分别列出每个根因的证据链和修复建议"
            if n_causes > 1 else ""
        )
        # ── Report already written ──
        # The graph always invokes the model once more after write_file.
        # Without this branch the guidance would still demand write_file
        # and forbid text output — contradicting the final user-facing
        # summary the LLM is expected to produce in that last call.
        if ledger.get("report"):
            return (
                "你当前处于 REPORT 阶段（诊断报告已写入）。\n"
                "- 报告已保存，无需再调用 write_file / record_finding / commit_hypotheses\n"
                "- 请用 3~5 句话向用户总结：根因（多根因分别列出）、关键证据、"
                "最重要的修复建议\n"
                "- 总结面向运维工程师——禁止提及台账、报告文件路径等内部实现细节"
            )
        path_hint = (
            f"必须立即调用 write_file 将报告写入 {report_path}"
            if report_path else "必须立即调用 write_file 生成诊断报告"
        )
        # Surface never-verified (shelved) hypotheses so the report
        # labels them accurately.  A deferred root hypothesis may be a
        # real (secondary) root cause the Coordinator chose not to
        # pursue — silently presenting it as "已排除" would misrepresent
        # the diagnosis (observed: shelved H2/H3 rendered as excluded
        # in a dual-root-cause session).
        unverified_ids = [
            h["id"] for h in ledger.get("hypotheses", {}).values()
            if h.get("deferred") and h.get("status") in ("pending", "inconclusive")
        ]
        unverified_hint = ""
        if unverified_ids:
            unverified_hint = (
                f"\n- ⚠ 以下假设未实际验证（已搁置）：{', '.join(unverified_ids)}"
                " — 报告中应标注为「未验证」并列入后续排查建议，"
                "不得写入「已排除假设」"
            )
        return (
            "你当前处于 REPORT 阶段。\n"
            f"⛔ {path_hint}。\n"
            "⛔ 禁止输出文本分析、总结或复盘 — 报告内容写入 write_file，不写在对话里。\n"
            "- 系统已自动终结所有挂起假设，你无需再调用 record_finding / commit_hypotheses\n"
            "- 报告基于上方 <diagnosis_ledger> 中的证据链和根因"
            + multi_hint + unverified_hint +
            "\n- 报告模板参考系统提示中的 REPORT 阶段说明\n"
            "- ⚠ 诊断台账（ledger JSON）已由系统自动持久化，无需手动写入"
        )
    return ""


# ── Serialization ──

def ledger_to_json(ledger: DiagnosisLedger) -> str:
    """Serialize ledger to JSON string."""
    return json.dumps(ledger, ensure_ascii=False, indent=2, default=str)


def ledger_from_json(data: str) -> DiagnosisLedger:
    """Deserialize ledger from JSON string."""
    return DiagnosisLedger(json.loads(data))


# ── Round recording ──

def record_round(
    ledger: DiagnosisLedger,
    phase: DiagnosisPhase,
    tools_called: list[str],
    action_summary: str = "",
    key_findings: str = "",
    next_plan: str = "",
    _seq: int = 0,
    delegated_experts: list[str] | None = None,
) -> None:
    """Record a tool call within the current diagnosis round.

    NOTE: current_round is synced from the middleware's _model_call_count
    (via before_model hook), NOT incremented here. This ensures round
    numbers reflect actual LLM invocations, not individual tool calls.
    """
    focus_id = ledger["active_path"][-1] if ledger["active_path"] else None
    entry = RoundRecord(
        round=ledger["current_round"],
        phase=phase,
        focus_hypothesis_id=focus_id,
        action_summary=action_summary,
        tools_called=tools_called,
        key_findings=key_findings,
        next_plan=next_plan,
        _seq=_seq,
    )
    if delegated_experts:
        entry["delegated_experts"] = delegated_experts
    ledger["rounds"].append(entry)


# ── Quantified exit model (v2.1) ──

def _uncertainty(node: HypothesisNode) -> float:
    """Residual uncertainty U(h) = 4·p·(1-p), p = probability/100.

    1.0 at p=50 (maximally undecided), →0 near the extremes.  A confirmed
    hypothesis has zero residual uncertainty by definition (its verdict
    closed the question), likewise refuted; callers filter by status.
    """
    p = max(0, min(100, node.get("probability", 50))) / 100.0
    return 4.0 * p * (1.0 - p)


def residual_uncertainty(ledger: DiagnosisLedger) -> float:
    """R = Σ U(h) over undecided hypotheses.

    Undecided = pending or inconclusive AND NOT deferred.  Deferred nodes
    are explicitly shelved by the LLM — their residual uncertainty does
    not count against "把握足够" (D4); they remain backtrackable and
    re-enter R when re-activated (deferred cleared by select_path /
    activate_hypothesis / backtrack).  Low-probability pending nodes
    (p ≤ 15) are excluded on the same grounds as the legacy E1 rule.
    """
    total = 0.0
    for node in ledger.get("hypotheses", {}).values():
        if node.get("status") not in ("pending", "inconclusive"):
            continue
        if node.get("deferred"):
            continue
        if node.get("status") == "pending" and node.get("probability", 0) <= 15:
            continue
        total += _uncertainty(node)
    return total


def delegation_value(node: HypothesisNode) -> float:
    """value(a) = EIG(a)/c(a) for a targeted expert delegation on *node*.

    EIG = U(h) × D × F, D=1.0 (targeted), F = 0.5^(n_deleg + n_inc)
    (D6: inconclusive verdicts add freshness decay — the same evidence
    re-examined rarely flips the outcome).  Cost = COST_DELEGATION.
    """
    n = node.get("_delegate_count", 0) + node.get("_inconclusive_count", 0)
    eig = _uncertainty(node) * (0.5 ** n)
    return eig / COST_DELEGATION


def max_action_value(ledger: DiagnosisLedger) -> tuple[float, str]:
    """Max value over still-executable verification actions.

    Actions considered (deterministic, ledger-derived):
    - delegation on each pending (non-deferred) / inconclusive hypothesis
    - batch retry (if budget open): fresh hypotheses carry U≈1, D=1, F=1
    Returns (value, detail) — detail names the best action for logging.
    """
    hypotheses = ledger.get("hypotheses", {})
    best_v, best_d = 0.0, "(无可执行验证动作)"
    for hid, node in hypotheses.items():
        if node.get("status") not in ("pending", "inconclusive"):
            continue
        if node.get("deferred"):
            continue
        if node.get("status") == "pending" and node.get("probability", 0) <= 15:
            continue
        v = delegation_value(node)
        if v > best_v:
            best_v = v
            best_d = (
                f"委派验证 {hid} (p={node.get('probability')}%, "
                f"已委派{node.get('_delegate_count', 0)}次/"
                f"inconclusive {node.get('_inconclusive_count', 0)}次)"
            )
    if ledger.get("root_hypothesis_ids") and can_commit_root_hypotheses(ledger):
        v = 1.0 / COST_BATCH_COMMIT  # fresh batch: U=1, D=1, F=1
        if v > best_v:
            best_v = v
            best_d = "换批提交新根假设"
    return best_v, best_d


# ── Exit condition checking ──

def check_exit_conditions(ledger: DiagnosisLedger) -> tuple[bool, str, str | None]:
    """Check if diagnosis should exit to REPORT.

    Returns (should_exit, reason, confirmed_hypothesis_id).

    v2.1 quantified model (state-machine-v2.md §6):
      exit = S1 (把握足够) AND S2 (增益<成本), plus fast lanes:
      - E3 evidence saturation (3 consecutive inconclusive)
      - E2 exhaustion (all roots refuted, batch budget spent)
      - E4 quick path (confirmed leaf, nothing pending below)
    S1: ≥1 confirmed root with p≥80 AND residual uncertainty R < 0.8
        (deferred / low-p nodes excluded — they were explicitly shelved).
    S2: max action value EIG/c < κ=0.15, or no executable actions left.
    """
    hypotheses: dict = ledger["hypotheses"]
    if not hypotheses:
        return False, "", None

    # ── E3 evidence saturation (fast lane): global, takes precedence ──
    if ledger.get("_inconclusive_streak", 0) >= 3:
        return True, "证据饱和: 连续3次验证结果不明确", None

    root_ids: list[str] = ledger.get("root_hypothesis_ids", [])
    confirmed_ids = [
        hid for hid in (root_ids or list(hypotheses.keys()))
        if hypotheses.get(hid, {}).get("status") == "confirmed"
        and hypotheses[hid].get("probability", 0) >= 80
    ]

    # ── E2 exhaustion (fast lane): all roots refuted, budget spent ──
    if root_ids and not confirmed_ids:
        all_refuted = all(
            hypotheses.get(hid, {}).get("status") in _HARD_FAILED_STATUSES
            for hid in root_ids
        )
        if all_refuted and not can_commit_root_hypotheses(ledger):
            return True, (
                f"假设穷尽: {MAX_ROOT_COMMIT_BATCHES} 批根假设全部证伪，"
                "未确认根因"
            ), None

    # ── S1: confidence sufficient ──
    if not confirmed_ids:
        # No confident root cause yet — never exit on the gain<cost
        # rule alone; the LLM still owes a direction (retry / backtrack).
        return False, "", None
    r = residual_uncertainty(ledger)
    if r >= R_THRESHOLD:
        return False, (
            f"已有确认根因 {confirmed_ids[0]}，但残余不确定性 "
            f"R={r:.2f} ≥ {R_THRESHOLD}，仍有值得验证的未决假设"
        ), confirmed_ids[0]
    if _has_pending_sub_hypotheses(ledger):
        return False, "仍有待验证的子假设，需继续深化分析", confirmed_ids[0]

    # ── S2: expected information gain < cost ──
    best_v, best_d = max_action_value(ledger)
    if best_v >= KAPPA_STOP:
        return False, (
            f"已有确认根因 {confirmed_ids[0]}，但最高动作价值 "
            f"{best_v:.3f} ≥ κ={KAPPA_STOP}（{best_d}），"
            "继续验证仍有正收益"
        ), confirmed_ids[0]

    best = hypotheses[confirmed_ids[0]]
    return True, (
        f"根因确认: {best['id']} {best['statement']} "
        f"(p={best['probability']}%) | 退出判据满足: 把握足够 "
        f"(R={r:.2f}<{R_THRESHOLD}) 且继续验证增益<成本 "
        f"(最高动作价值 {best_v:.3f}<κ={KAPPA_STOP}，{best_d})"
    ), confirmed_ids[0]
