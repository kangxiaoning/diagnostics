"""Diagnosis ledger data structures, rendering, and serialization.

The ledger is a structured diagnosis memory persisted in LangGraph state.
It holds the hypothesis tree, evidence chain, and diagnosis round history.

See: private/HYPOTHESIS_LEDGER_DESIGN.md
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from typing import Annotated, Literal, NotRequired

from deepagents.graph import DeepAgentState
from langchain.agents.middleware.types import PrivateStateAttr

from diagnostics.agent.prompt import PHASE_SPEC  # P6/§9: duty/exit 单源（无循环依赖：prompt 不导入本模块）


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

# ── Hypothesis commit budget (GLOBAL, hard-coded) ──
# At most this many commit_hypotheses CALLS per diagnosis — first batch
# and retry batch (换批) BOTH consume from this single budget.  With the
# per-call limit of MAX_LAYER_SIZE, a diagnosis can never exceed
# MAX_COMMIT_CALLS × MAX_LAYER_SIZE = 6 hypotheses in total.
# (v2.7 flat model: sub-hypotheses / deepening were removed — every
# commit creates ROOT hypotheses only.)
MAX_COMMIT_CALLS = 2

# ── Root-hypothesis batch gate ──
# A retry batch of root hypotheses is only allowed after ALL root
# hypotheses from prior batches are hard-failed (refuted/dead_end) —
# and only while the global commit budget above is not exhausted.
MAX_ROOT_COMMIT_BATCHES = 2

# ── Hypothesis-list hard invariants (deterministic, code-enforced) ──
# These are BUSINESS invariants — they cannot be expressed by tool-input
# schemas (Pydantic validates a single call, not cumulative state),
# so they are enforced here at the data-structure layer.  Rationale and
# evidence: private/design/state-machine-v2.md §11; the 2026-07-20
# 32-round session (root layer grew to 6 via batch accumulation).
MAX_LAYER_SIZE = 3
"""Max hypotheses per commit call AND per current batch.  A retry batch
REPLACES the current root layer instead of appending.  v2.7: the model
is flat (no sub-hypotheses), so this is simply the per-batch cap."""

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
#   S2 (no actionable verification): max available action value < KAPPA_STOP
#       — i.e. every undecided hypothesis is economically dead
#       (delegation_value < κ).  R = Σ U(h) over undecided h (deferred
#       nodes excluded — D4) is computed for observability only; it is
#       NOT a hard gate (v2.8: a hypothesis may be unresolvable yet sit at
#       p=50 keeping R high — gating on R caused 6 blocked write_file
#       calls / 15 wasted rounds in session d06c02d2).  Actionability is
#       the correct gate: any undecided hypothesis with gain ≥ κ could be
#       a competing root cause and gets verified (multi-root protection);
#       once all are dead, continuing would be fruitless spinning → exit.
# Fast lanes (kept from v2): E2 exhaustion, E3 saturation.  (v2's E4
# quick path was removed with the quantified exit model — no implementation.)
KAPPA_STOP = 0.15
"""Stop threshold on max action value (D2).  Calibrated so a targeted
delegation is allowed ~2 times on the same hypothesis (0.25 > κ) but a
3rd is blocked (0.125 < κ)."""

R_THRESHOLD = 0.8
"""Legacy constant — no longer an exit gate (v2.8).  Kept for reference;
residual_uncertainty() is still surfaced in logs/reasons for
observability."""

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
        _commit_count=0,
        entity_type=entity_type,
        hostname=hostname,
        entity_name=entity_name,
    )


def new_hypothesis(
    hid: str,
    statement: str,
    probability: int,
    round_num: int,
    rationale: str = "",
) -> HypothesisNode:
    """Create a flat-model hypothesis node (v2.7: no parent/depth/subs)."""
    return HypothesisNode(
        id=hid,
        statement=statement,
        probability=max(0, min(100, probability)),
        status="pending",
        evidence=[],
        verification_tools=[],
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
        summary=summary[:3000],
        supports=supports,
        tool_call_id=tool_call_id,
        timestamp=datetime.now(UTC).isoformat(),
    )


# ── Hypothesis ID model (v2.7 flat, deterministic) ──
# Canonical internal IDs are NUMERIC STRINGS ("1", "2", ...) — integers
# in semantics (monotonic counter, numeric validation), stored as plain
# strings because JSON object keys are strings.  Display code prefixes
# "H" via ``fmt_hid`` ("1" -> "H1").  Hierarchical IDs ("H2.1") no
# longer exist: the sub-hypothesis machinery was removed.

def _parse_hypothesis_num(raw: object) -> int | None:
    """Parse a hypothesis ID into its integer number, or None if invalid.

    Accepts canonical numeric strings ("3"), ints, and display-prefixed
    forms ("H3"/"h3").  Rejects hierarchical IDs ("2.3"/"H2.1"), zero,
    negatives, and non-numeric input.
    """
    s = str(raw).strip()
    if s[:1] in ("H", "h"):
        s = s[1:]
    if not s.isdigit():
        return None
    n = int(s)
    return n if n >= 1 else None


def fmt_hid(hid: object) -> str:
    """Display form of a hypothesis ID: "H" + numeric part ("1" -> "H1").

    Idempotent for legacy display-form IDs ("H1" -> "H1"); legacy
    hierarchical IDs pass through unchanged ("H2.1" -> "H2.1") so old
    ledger files still render readably.
    """
    s = str(hid).strip()
    if s[:1] in ("H", "h"):
        return "H" + s[1:]
    return "H" + s


def _valid_ids_hint(hypotheses: dict[str, HypothesisNode]) -> str:
    """Human-readable list of valid hypothesis IDs for error messages."""
    if not hypotheses:
        return "(尚无假设)"
    nums = sorted(
        (n for n in (_parse_hypothesis_num(k) for k in hypotheses)
         if n is not None)
    )
    return ", ".join(fmt_hid(str(n)) for n in nums)


def resolve_hypothesis_id(ledger: DiagnosisLedger, raw: object) -> str:
    """Normalize an LLM-supplied hypothesis ID to its canonical form.

    Deterministic validation (v2.7 flat model): accepts 3 / "3" /
    "H3" / "h3" and returns "3"; rejects hierarchical IDs, zero,
    out-of-range, and non-numeric input with a corrective Chinese
    error listing the valid IDs.  All tool entry points MUST route
    LLM-supplied IDs through here before touching ``hypotheses``.
    """
    hypotheses = ledger.get("hypotheses", {})
    n = _parse_hypothesis_num(raw)
    if n is None:
        raise ValueError(
            f"假设 ID '{raw}' 无效。假设 ID 是整数编号（如 H1、H2），"
            "不支持 H2.1 这类层级 ID（假设深化已移除，如需更具体的表述"
            "请用 record_finding(statement_update=...) 修正）。"
            f"当前可用: {_valid_ids_hint(hypotheses)}"
        )
    hid = str(n)
    if hid not in hypotheses:
        raise ValueError(
            f"假设 {fmt_hid(hid)} 不存在。"
            f"当前可用: {_valid_ids_hint(hypotheses)}"
        )
    return hid


def _next_hypothesis_id(hypotheses: dict[str, HypothesisNode]) -> str:
    """Generate the next canonical hypothesis ID (max existing + 1).

    Scans existing IDs numerically, so repeated calls within a single
    commit_hypotheses invocation (nodes are inserted as they are
    created) never collide.
    """
    max_n = 0
    for key in hypotheses:
        n = _parse_hypothesis_num(key)
        if n is not None and n > max_n:
            max_n = n
    return str(max_n + 1)


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
    - v2.9 soft-terminal: an ECONOMICALLY DEAD pending node
      (``_economically_dead`` — delegation_value < κ, so G5 would reject
      any further delegation anyway) does NOT block either.  Requiring a
      burial record_finding for a node the system itself forbids
      verifying is the same ritual-verification waste the rule above
      eliminated for deferred nodes.
    - Any batch additionally requires the GLOBAL commit budget
      (``MAX_COMMIT_CALLS``) to still be open — derive_phase (T8) and the
      backtrack tool both route through this predicate, so folding the
      global check in here keeps every caller coherent.
    """
    if ledger.get("_commit_count", 0) >= MAX_COMMIT_CALLS:
        return False
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
        if (node.get("status") == "pending"
                and not node.get("deferred")
                and not _economically_dead(node)):
            return False
    return True


def _commit_budget_exhausted_message(ledger: DiagnosisLedger) -> str:
    """Shared rejection text when the global commit budget is spent."""
    return (
        f"假设提交预算已用尽（全程最多 {MAX_COMMIT_CALLS} 次，"
        f"已提交 {ledger.get('_commit_count', 0)} 次）。不可再提交新假设。"
        "如需细化已有假设，请用 record_finding(statement_update=...) "
        "修正其表述；如需调整验证方向，用 select_path 切换焦点 / "
        "backtrack 恢复搁置假设；全部假设证伪时系统会自动进入 "
        "REPORT（T9），无需手动提交。"
    )


def add_hypotheses(
    ledger: DiagnosisLedger,
    hypothesis_specs: list[dict],
) -> list[HypothesisNode]:
    """Add up to 3 ROOT hypotheses (v2.7 flat model). Returns created nodes.

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

    # Detect duplicate statements across ALL hypotheses (the model is
    # flat — every hypothesis is a root).  Refuted/dead_end hypotheses
    # also participate in dedup so a retry batch cannot re-submit a
    # failed hypothesis with identical wording.
    existing_root = {
        h["statement"].strip().lower(): h
        for h in hypotheses.values()
    }
    for spec in hypothesis_specs:
        norm = spec["statement"].strip().lower()
        dup = existing_root.get(norm)
        if dup is not None:
            if dup.get("status") in _HARD_FAILED_STATUSES:
                raise ValueError(
                    f"假设 '{spec['statement'][:80]}' 与已排除假设 "
                    f"{fmt_hid(dup['id'])} 重复。新一批假设必须基于排除证据"
                    "换方向，禁止重复或仅换措辞。"
                )
            raise ValueError(
                f"假设 '{spec['statement'][:80]}' 已存在。"
                f"如需更新其概率或结论，请使用 record_finding 工具，"
                f"而非 commit_hypotheses 重复提交。"
            )

    # ── Commit budget (global) + retry-batch gate ──
    # Checked AFTER dedup so a rejected duplicate does not consume
    # budget.  can_commit_root_hypotheses folds in the global
    # MAX_COMMIT_CALLS check, so budget exhaustion surfaces first;
    # the remaining False case is the retry-batch gate (confirmed or
    # active-pending root still standing).
    if not can_commit_root_hypotheses(ledger):
        if (ledger.get("_commit_count", 0) >= MAX_COMMIT_CALLS
                or ledger.get("_root_commit_count", 0)
                >= MAX_ROOT_COMMIT_BATCHES):
            raise ValueError(_commit_budget_exhausted_message(ledger))
        raise ValueError(
            "当前批仍有活跃 pending 或已 confirmed 的假设，不可换批。"
            "请先用 record_finding 终结活跃假设，或用 select_path "
            "切换验证焦点（deferred/inconclusive 假设不阻塞换批）。"
            "若你的意图是确认某个已有假设为根因，请直接对该假设调用 "
            "record_finding(verdict='confirmed')——重复提交不会确认它。"
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

    # ── Global commit budget consumption ──
    # Every successful commit — first batch or retry batch — consumes
    # exactly one unit.  With the per-call limit above, total
    # hypotheses per diagnosis are bounded by
    # MAX_COMMIT_CALLS × MAX_LAYER_SIZE = 6.
    ledger["_commit_count"] = ledger.get("_commit_count", 0) + 1

    for spec in hypothesis_specs:
        hid = _next_hypothesis_id(hypotheses)
        node = new_hypothesis(
            hid=hid,
            statement=spec["statement"],
            probability=spec.get("probability", 50),
            round_num=ledger["current_round"],
            rationale=spec.get("rationale", ""),
        )
        hypotheses[hid] = node
        created.append(node)
        ledger["root_hypothesis_ids"].append(hid)

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
        # New root batch: reset the active path — a stale path from
        # a failed batch must not prefix the new root hypothesis.
        ledger["active_path"] = [best["id"]]

    return created


def select_path(
    ledger: DiagnosisLedger,
    selected_id: str,
    rationale: str = "",
    deprioritized: list[dict] | None = None,
) -> None:
    """Select one hypothesis as the verification focus, defer others."""
    if deprioritized is None:
        deprioritized = []
    hypotheses = ledger["hypotheses"]
    selected_id = resolve_hypothesis_id(ledger, selected_id)

    # ── T5 guard (state-machine-v2.md §5): the target must be decidable ──
    # Selecting a terminal node is never a verification direction: a
    # confirmed node is refined via record_finding (v2.7.1 channel), a
    # refuted node is permanently closed.  Previously the guard existed
    # only in the doc — selecting a terminal node polluted the ★ marker
    # and active_path (reproduced 2026-07-31).
    _target = hypotheses[selected_id]
    _target_status = _target.get("status")
    if _target_status not in ("pending", "inconclusive"):
        if _target_status == "confirmed":
            raise ValueError(
                f"假设 {fmt_hid(selected_id)} 已 confirmed"
                f"（p={_target.get('probability')}%），不是验证目标。"
                "如需强化置信度或修正表述，请用 "
                "record_finding(verdict='confirmed', probability_update=..., "
                "statement_update=...)。"
            )
        raise ValueError(
            f"假设 {fmt_hid(selected_id)} 已终态（{_target_status}），"
            "不可选为验证焦点。可选目标：pending / inconclusive 假设"
            "（含 deferred 搁置复活）。"
        )

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

    # Flat model (v2.7): siblings are always the current root batch and
    # the active path is the single selected hypothesis.
    ledger["active_path"] = [selected_id]

    # Mark non-selected siblings as deferred (v2.1: flag, not status)
    for sid in ledger["root_hypothesis_ids"]:
        if sid == selected_id:
            continue
        sibling = hypotheses[sid]
        if sibling["status"] == "pending":
            sibling["deferred"] = True
            sibling["selected"] = False
    # Apply explicit defer reasons
    for dep in deprioritized:
        dep_raw = dep.get("id", "")
        try:
            dep_id = resolve_hypothesis_id(ledger, dep_raw)
        except ValueError:
            continue  # unknown/invalid id in a defer hint — not fatal
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

    # Flat model (v2.7): the active path is the single active hypothesis.
    ledger["active_path"] = [hid]


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
    hypothesis_id = resolve_hypothesis_id(ledger, hypothesis_id)

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
#   | evaluate    | T4  exit conditions E1-E3                        | report      |
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
         retry-batch (T8) precedes the exit check so a last-standing
         failed root (root_causes empty) routes to hypothesize instead
         of being shadowed by the legacy exhaustion exit; only
         genuinely terminal states fall through to REPORT.
      5. Exit conditions E1-E3 → report; otherwise evaluate.
    """
    # 1. REPORT terminal lock: report already written, OR a guardrail
    #    forced the terminal entry (§8: G1 max-rounds / G7 fallback / G9 /
    #    verify-stall).  The forced marker is ledger data, so P1 (phase
    #    derived purely from the ledger) still holds; honouring it here
    #    keeps the rendered phase guidance consistent with the tool gate
    #    and the middleware's effective phase (both already treat
    #    _forced_terminal as REPORT) — otherwise a forced entry with no
    #    confirmed root cause would keep rendering the EVALUATE/
    #    HYPOTHESIZE menu while the gate rejects those tools.
    if ledger.get("report") or ledger.get("_forced_terminal"):
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
    #    T8 retry batch — all root hypotheses hard-failed OR economically
    #    dead (v2.9 soft-terminal: a node G5 would refuse to verify must
    #    not trap the layer in evaluate — nothing legal can be done with
    #    it there) and budget left.  A failed/dead layer with no
    #    resumable sibling can only move forward via a fresh batch (or
    #    exit when the budget is gone); presenting "evaluate" here would
    #    offer no executable action.
    #    Exception: E3 (evidence saturation) always wins — a ledger that
    #    hit 3 consecutive inconclusive verdicts exits to REPORT even if
    #    the active node was subsequently hard-closed (the streak marks
    #    evidence exhaustion, retrying the same data is futile).
    root_ids = ledger.get("root_hypothesis_ids", [])
    if (
            root_ids
            and ledger.get("_inconclusive_streak", 0) < 3
            and all(
                hypotheses.get(hid, {}).get("status")
                in _HARD_FAILED_STATUSES
                or _economically_dead(hypotheses.get(hid, {}))
                for hid in root_ids)):
        if can_commit_root_hypotheses(ledger):
            return "hypothesize"
        return "report"  # T9 == E2

    # 5. Exit conditions E1-E3 → report; otherwise evaluate.
    if ledger.get("exhausted"):
        return "report"
    should_exit, _reason, _cid = check_exit_conditions(ledger)
    if should_exit:
        return "report"
    return "evaluate"


# Round at which the no-delegation grace fires even when an Argus target
# IS resolved: a deadlock escape for an LLM that refuses to delegate at
# all (G2's round≥2 nudges had their window).
_NO_DELEGATION_FALLBACK_ROUND = 4


def _argus_target_resolved(ledger: DiagnosisLedger) -> bool:
    """True when Argus has a queryable target for this diagnosis.

    Host scenarios need a hostname; container/k8s scenarios need a
    cluster (entity_name) or a hostname.  When nothing resolved, the
    scenario is genuinely data-free.
    """
    if ledger.get("entity_type", "") == "host":
        return bool(ledger.get("hostname"))
    return bool(ledger.get("entity_name") or ledger.get("hostname"))


def _coverage_ready(ledger: DiagnosisLedger) -> bool:
    """UNDERSTAND→HYPOTHESIZE gate (T1): required Argus data collected.

    Rules (see state-machine-v2.md §4):
    - At least 2 rounds have passed (delegation needs time to return).
    - If any task() delegation happened: host scenarios require
      host-argus-expert; container scenarios require BOTH host-argus-expert
      and k8s-argus-expert.
    - If no delegation happened at all, coverage is considered ready
      after the grace ONLY for genuinely data-free scenarios (no
      resolved Argus target), or as a round-4 deadlock escape.
    """
    if ledger.get("current_round", 0) < 2:
        return False
    # v2.5 argus-failure degrade: when the argus monitoring platform
    # returned empty data / errors (and any one-shot param re-delegation
    # was exhausted), the coverage gate is bypassed — the LLM commits
    # hypotheses from user input and the normal VERIFY→EVALUATE→REPORT
    # flow continues with domain experts collecting evidence.
    if ledger.get("_argus_unavailable"):
        return True
    experts_delegated: set[str] = set()
    has_delegated = False
    for rd in ledger.get("rounds", []):
        experts_delegated.update(rd.get("delegated_experts", []))
        if "task" in rd.get("tools_called", []):
            has_delegated = True
    if not has_delegated:
        # Data-free grace — restricted to scenarios with NO resolved
        # Argus target.  When the entity resolved (Argus data IS
        # available), flipping to HYPOTHESIZE here traps the LLM into
        # data-less commits: the §7 gate blocks the very task()
        # delegation the prompt and the G2 valve demand (observed
        # 2026-07-30 session f62a11eb: round-1 skill reads — legal per
        # prompt — then the round-2 flip hard-blocked both argus
        # delegations, forcing 3 data-less hypotheses at round 3).
        if not _argus_target_resolved(ledger):
            return True
        return (ledger.get("current_round", 0)
                >= _NO_DELEGATION_FALLBACK_ROUND)
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


def _can_backtrack(ledger: DiagnosisLedger) -> bool:
    """Check if there are deferred, pending, or inconclusive nodes to backtrack to."""
    for node in ledger["hypotheses"].values():
        if node["status"] in ("pending", "inconclusive"):
            return True
    return False


def _inconclusive_exhausted(ledger: DiagnosisLedger) -> bool:
    """Check if evidence is saturated (3+ consecutive inconclusive)."""
    return ledger.get("_inconclusive_streak", 0) >= 3


def backtrack(ledger: DiagnosisLedger) -> str | None:
    """Backtrack to the first deferred, pending, or inconclusive root hypothesis.

    Flat model (v2.7): backtrack targets are always siblings in the
    current root batch.  Returns new active ID or None if exhausted.
    """
    ledger["_backtrack_count"] += 1

    hypotheses = ledger["hypotheses"]
    active_id = ledger["active_path"][-1] if ledger["active_path"] else None
    for sid in ledger["root_hypothesis_ids"]:
        if sid == active_id:
            continue
        sibling = hypotheses.get(sid)
        if sibling is None:
            continue
        if sibling["status"] in ("pending", "inconclusive"):
            # Found a backtrack target — re-activate (clear shelved mark)
            sibling["selected"] = True
            sibling["deferred"] = False
            sibling["deferred_reason"] = ""
            ledger["active_path"] = [sid]
            return sid

    # No backtrack target found.  NOTE: the caller (backtrack tool)
    # decides the outcome — retry budget remaining → hypothesize;
    # budget exhausted → set ledger["exhausted"] and go to report.
    return None


def _clean_evidence_text(text: str, max_chars: int = 3000) -> str:
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


# ── Belief self-report (SBR v2, tool-based) parsing ──
# state-machine-v2.md §12: every Coordinator round should call the
# report_beliefs tool, self-reporting the model's belief state (phase /
# per-hypothesis beliefs / root-cause candidate / menu choice) as flat
# string args.  The middleware parses the args and runs deterministic
# belief-ledger consistency checks; these helpers are the shared,
# dependency-free parsing primitives.
#
# v1 parsed a fenced ```diagnosis_status text block from the response —
# abandoned: the local model emitted it 0/14 rounds (its reliable
# structured channel is tool calls, not persistent text formats).
# strip_diagnosis_status_blocks stays as protection for legacy blocks
# that may still appear in multi-turn history / reports.

_SBR_FENCED_RE = re.compile(
    r"```\s*diagnosis_status[^\n`*]*\n(.*?)(?:```|\Z)", re.S | re.I,
)
_SBR_XML_RE = re.compile(
    r"<diagnosis_status>(.*?)(?:</diagnosis_status>|\Z)", re.S | re.I,
)

# Status aliases → canonical ledger status.  English tokens pass through
# lowercased; common Chinese renderings are accepted (lenient parsing —
# a local model's wording varies, and a missed alias must degrade to
# "no opinion" rather than a parse failure).
_SBR_STATUS_ALIASES = {
    "pending": "pending", "待验证": "pending", "未决": "pending", "待定": "pending",
    "inconclusive": "inconclusive", "不确定": "inconclusive", "无法定论": "inconclusive",
    "refuted": "refuted", "证伪": "refuted", "已证伪": "refuted",
    "排除": "refuted", "已排除": "refuted",
    "confirmed": "confirmed", "确认": "confirmed", "已确认": "confirmed",
    "已证实": "confirmed", "证实": "confirmed",
}

# H3=pending@20 / 3=证伪@5 / H1=confirmed@95（附注） — the trailing
# parenthetical note is simply not consumed.
_SBR_BELIEF_ITEM_RE = re.compile(
    r"[Hh]?(\d+)\s*[=＝:：]\s*([A-Za-z一-鿿]+?)\s*[@＠]\s*(\d{1,3})\s*%?"
)

_SBR_KNOWN_PHASES = frozenset({
    "understand", "hypothesize", "verify", "evaluate", "report",
})


def parse_beliefs_field(value: str) -> dict:
    """Parse a report_beliefs ``beliefs`` arg like
    ``"1=refuted@5, H3=pending@20（附注）"`` into
    ``{hid: (canonical_status, prob)}``.  Unparseable items are skipped
    (never an error — a malformed item means "no opinion")."""
    beliefs: dict = {}
    if not value:
        return beliefs
    for bm in _SBR_BELIEF_ITEM_RE.finditer(value):
        hid, raw_status, prob = bm.group(1), bm.group(2), int(bm.group(3))
        status = _SBR_STATUS_ALIASES.get(raw_status.lower())
        if status:
            beliefs[hid] = (status, min(prob, 100))
    return beliefs


def parse_root_cause_field(value: str) -> tuple[str, int | None] | None:
    """Parse a report_beliefs ``root_cause_candidate`` arg like
    ``"DockerHub限速@95"`` into ``(description, prob | None)``.
    Returns ``None`` for empty/none-like values."""
    if not value:
        return None
    v = value.strip()
    if not v or v.lower() in ("none", "无", "暂无", "(无)", "n/a", "-"):
        return None
    pm = re.search(r"[@＠]\s*(\d{1,3})\s*%?", v)
    desc = v[:pm.start()].rstrip(" ，,、") if pm else v
    prob = min(int(pm.group(1)), 100) if pm else None
    return (desc, prob) if desc else None


def strip_diagnosis_status_blocks(text: str) -> str:
    """Remove all diagnosis_status blocks (fenced and XML forms).

    Used when persisting LLM text as the fault report — legacy v1 blocks
    may still appear in multi-turn history and must not leak into
    reports (they are process metadata, not report content).
    """
    if not text:
        return text
    out = _SBR_FENCED_RE.sub("", text)
    out = _SBR_XML_RE.sub("", out)
    # Collapse the blank line run a stripped block typically leaves at
    # the document head.
    return out.lstrip("\n") if out != text else text


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
    active_path_str = (" → ".join(fmt_hid(h) for h in ledger["active_path"])
                       if ledger["active_path"] else "(无)")
    batch_info = ""
    if ledger.get("_commit_count"):
        batch_info = (
            f" | 假设提交: {ledger['_commit_count']}"
            f"/{MAX_COMMIT_CALLS} 次"
            f"（共 {len(ledger.get('hypotheses', {}))}"
            f"/{MAX_COMMIT_CALLS * MAX_LAYER_SIZE} 个）"
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
                f"- [{tag}] {fmt_hid(h['id'])} {h['statement']}: "
                f"{_clean_evidence_text(reason, 2000)}"
            )
        for h in deferred:
            reason = (h.get("deferred_reason") or h.get("rationale", "")
                      or "(无记录)")
            lines.append(
                f"- [搁置待回溯 p={h['probability']}%] {fmt_hid(h['id'])} "
                f"{h['statement']}: {_clean_evidence_text(reason, 2000)}"
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

    # Current step guidance.  Step 0 is always the belief self-report
    # (SBR v2, §12): folded INTO the phase-duty menu because that menu is
    # the one instruction block the model demonstrably follows every
    # round — a standalone reminder section gets classified as optional
    # metadata and dropped under task load (production: tail-reminder
    # sections ignored in rounds 2+ after a compliant round 1).
    guidance = _phase_guidance(phase, ledger, report_path)
    if guidance:
        lines.append("## 本步要求")
        lines.append(
            "0. 调用 report_beliefs 自报当前认知"
            "（phase / beliefs / root_cause_candidate / menu_choice）"
            "——每轮例行，先于下列动作；可与其他工具同轮并行；"
            "自报不改变台账，结论仍须经 commit_hypotheses / record_finding "
            "正式提交。"
        )
        lines.append(guidance)

    # ── Belief self-report mirror (SBR v2, §12) ──
    # Reflect the model's latest report_beliefs self-report next to the
    # ledger truth, at the prompt tail.  Deterministic ⚠ flags mark
    # self-report/ledger divergences and STALENESS — an outdated
    # self-report is itself a nudge to re-report.
    latest = ledger.get("_sbr_latest")
    if latest:
        staleness = ledger.get("current_round", 0) - (latest.get("round") or 0)
        lines.append("")
        if staleness >= 1:
            lines.append("## 你最近一次的自报认知（report_beliefs 镜像）")
            lines.append(
                f"- ⚠ 该自报来自第 {latest.get('round')} 轮，"
                f"已 {staleness} 轮未更新——本轮请先调用 report_beliefs 更新"
            )
        else:
            lines.append("## 你上一轮的自报认知（report_beliefs 镜像）")
        sp = latest.get("phase") or "?"
        phase_flag = "" if sp == phase else f" ⚠ 台账当前为 {phase}"
        lines.append(f"- 自报相位: {sp}{phase_flag}")
        if latest.get("beliefs"):
            lines.append(f"- 假设判断: {latest['beliefs']}")
        rc = (latest.get("root_cause_candidate") or "").strip()
        if rc and rc.lower() not in ("none", "无", "暂无", "n/a", "-"):
            has_confirmed = any(
                h.get("status") == "confirmed"
                for h in ledger.get("hypotheses", {}).values()
            )
            rc_flag = "" if has_confirmed else " ⚠ 台账尚无 confirmed 假设——需经 record_finding 正式确认"
            lines.append(f"- 根因候选: {rc}{rc_flag}")
        if latest.get("menu_choice"):
            lines.append(f"- 动作依据: {latest['menu_choice']}")

    lines.append("</diagnosis_ledger>")
    return "\n".join(lines)


def _render_hypothesis_tree(
    lines: list[str],
    ledger: DiagnosisLedger,
    ids: list[str],
    indent: int,
) -> None:
    """Render the hypothesis list (v2.7 flat model — one level only).

    ``indent`` is kept for signature compatibility but is always 0:
    sub-hypotheses no longer exist.
    """
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
            f"{prefix}### {fmt_hid(node['id'])} [{node['status']}{deferred_tag} "
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
                f"{_clean_evidence_text(ev.get('summary', ''), 3000)} "
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
                    f"{prefix}  理由: {_clean_evidence_text(reason, 2000)}"
                )


def _phase_guidance(phase: DiagnosisPhase, ledger: DiagnosisLedger,
                     report_path: str = "") -> str:
    """Generate guidance text for the current phase.

    职责（duty）与推进（exit/summary）语句单源于 ``prompt.PHASE_SPEC``
    （v2.8.2，P6/§9：此前两个文件各手写一份、历次设计迭代均需双改）；
    本函数只负责相位引入句、操作性 bullets 与动态 hint。
    """
    spec = PHASE_SPEC.get(phase, {})
    duty = spec.get("duty", "")
    exit_txt = spec.get("exit", "")
    if phase == "understand":
        return (
            "你当前处于 UNDERSTAND 阶段。\n"
            f"- {duty}\n"
            "- 委派 Argus 专家时使用上方「诊断会话参数」中的时间范围（禁止自行推断）\n"
            "- 委派几个 Argus 专家由实体类型决定（与覆盖门控一致）：\n"
            "  纯主机场景（entity_type=host）→ 仅 task(subagent_type=\"host-argus-expert\", description=...)\n"
            "  K8s/容器集群场景 → 单轮并行委派两个：task(subagent_type=\"host-argus-expert\") + task(subagent_type=\"k8s-argus-expert\")\n"
            "  即使症状仅点名集群/Pod/DNS/节点也必须双委派——主机节点资源（CPU/内存/磁盘 IO）是集群故障的底层维度，缺一则覆盖门控判数据不齐\n"
            "- 关注指标突变时间点和并发异常（同一分钟多条红线→可能独立根因）\n"
            "- ⚠ 查看上方「已有工具调用结果」，禁止重复调用已执行过的工具\n"
            f"- ⚠ {exit_txt}"
        )
    if phase == "hypothesize":
        retry_hint = ""
        if ledger.get("_root_commit_count", 0) > 0 and ledger.get("hypotheses"):
            retry_hint = (
                f"\n- ⚠ 这是第 {ledger.get('_commit_count', 0) + 1}"
                f"/{MAX_COMMIT_CALLS} 次假设提交（最后一次）。"
                "前一批已全部证伪（见上方「已排除/降级假设」）。\n"
                "  新假设必须基于排除证据换方向推断，"
                "禁止与已排除假设重复或仅换措辞。"
            )
        return (
            "你当前处于 HYPOTHESIZE 阶段。\n"
            f"- {duty}\n"
            f"- 提交预算：全程最多 {MAX_COMMIT_CALLS} 次"
            f"（本次为第 {ledger.get('_commit_count', 0) + 1} 次）；"
            "预算用尽后只能用 record_finding(statement_update=...) 修正已有假设\n"
            f"- 完成后必须调用 commit_hypotheses 提交假设（{exit_txt}）"
            + retry_hint
        )
    if phase == "verify":
        active_id = ledger["active_path"][-1] if ledger["active_path"] else "?"
        active = ledger["hypotheses"].get(active_id, {})
        stmt = active.get("statement", "?")
        disp_id = fmt_hid(active_id) if active_id != "?" else "?"
        return (
            f"你当前处于 VERIFY 阶段，聚焦验证 {disp_id}: {stmt}\n"
            f"- {duty}\n"
            "- 主机假设 → task(subagent_type=\"host-expert\", description=...) 委派验证\n"
            "- K8s 假设 → task(subagent_type=\"k8s-expert\", description=...) 委派验证\n"
            "- GPU 假设 → task(subagent_type=\"gpu-expert\", description=...) 委派验证\n"
            f"- 委派时明确\"验证假设{disp_id}\"并传入假设上下文\n"
            f"- 收到结果后必须调用 record_finding 记录结论（{exit_txt}）\n"
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
        # so exactly one of select/refine/backtrack/retry applies.
        pending_hint = ""
        root_cause_hyp_ids = ledger.get("root_cause_hypothesis_ids", [])
        # Scan ALL hypotheses (not just the current batch): a revived
        # prior-batch hypothesis is undecided too and must appear here
        # (v2.8.4 — the exit model scans tree-wide, the hint must agree).
        # Economically dead nodes are annotated: delegation on them is
        # hard-blocked by G5, so select_path alone is NOT the remedy.
        pending = [
            hid for hid, n in ledger.get("hypotheses", {}).items()
            if n.get("status") in ("pending", "inconclusive")
            and hid not in root_cause_hyp_ids
        ]
        if root_cause_hyp_ids and pending:
            _tags = []
            for h in pending:
                _tag = fmt_hid(h)
                if _economically_dead(ledger["hypotheses"][h]):
                    _tag += "（增益<κ，委派已被门控——请 record_finding 终结或搁置）"
                _tags.append(_tag)
            pending_hint = (
                f"\n- ⚠ 仍有待验证的假设: "
                f"{', '.join(_tags)}，使用 select_path 切换验证"
            )
        _retry_option = (
            "  · 本层全部 refuted 或验证增益已低于成本（经济死亡）、"
            "提交预算未用尽 → commit_hypotheses 换方向提交新一批假设\n"
            if MAX_COMMIT_CALLS - ledger.get("_commit_count", 0) > 0 else ""
        )
        # Live exit-condition verdict for the LLM.  In EVALUATE the state
        # machine has already ruled out exit (derive_phase), so this is
        # always "not satisfied" — surface the REASON so the LLM understands
        # why it is NOT in REPORT yet and must NOT call write_file.  Without
        # this, the ledger context's "## 根因" label (populated the moment a
        # hypothesis is confirmed) misleads the LLM into thinking the
        # diagnosis is done and it should write the report.
        _should_exit, _exit_reason, _ = check_exit_conditions(ledger)
        # NB: the "what to do next" advice (select_path 验证/证伪、无法定论
        # 可 defer) lives in PHASE_SPEC evaluate duty — single source; the
        # banner only carries the live verdict + the prohibition.
        exit_banner = (
            f"\n- ⛔ 禁止调用 write_file：出口判据当前未满足——{_exit_reason}。"
            "待判据满足后系统会自动进入 REPORT 并提示你写入报告。\n"
            "  即使台账已列出「根因」，只要上方判据未满足就仍属 EVALUATE，"
            "主动 write_file 会被门控拦截。"
        )
        return (
            "你当前处于 EVALUATE 阶段。\n"
            f"- {duty}\n"
            "- 下一步路径菜单：\n"
            "  · 现有证据已可判定某未决假设 → 直接 record_finding 记录结论"
            "（跨假设证据复用，无需重复委派——比 select_path 更省轮次）\n"
            "  · 证据不足、需继续验证 → select_path 切换到最可能的假设\n"
            "  · confirmed 假设需更具体 → record_finding(statement_update=...) "
            "修正其表述（假设为扁平结构，无层级深化）\n"
            "  · 本层全部 refuted 且有搁置假设 → backtrack 回溯恢复\n"
            + _retry_option +
            "- 多根因场景：证据支持的假设即使非主根因也应标记为 confirmed（次要根因/加剧因素），"
            "refuted 仅用于证据明确证伪的假设。多个假设可同为 confirmed。\n"
            "- ⚠ 概率判断原则：若核心因果关系已由证据确认"
            "（如「异常进程占满资源→服务响应超时」），"
            "仅次要细节未知（如触发者、启动源），"
            "应保持高概率(≥80%)。"
            "不要因为次要信息缺失而过度降低根因概率。\n"
            f"- 推进：{exit_txt}"
            + pending_hint
            + exit_banner
        )
    if phase == "report":
        n_causes = len(ledger.get("root_causes", []))
        summary_req = spec.get("summary", "")
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
            # ── Auto-generated stub: invite rewrite via write_file (G10) ──
            # A stub (system-generated after timeout/safety-valve fallback)
            # is a floor, not the deliverable.  Guide the LLM to author the
            # full report via write_file; if it declines (stub sufficient),
            # a short summary is acceptable, and the REPORT-stall handler
            # salvages any substantial report text it emits instead.
            if ledger.get("_report_auto_generated"):
                _overwrite_hint = (
                    f"覆盖写入 {report_path}" if report_path else "写入报告文件"
                )
                return (
                    "你当前处于 REPORT 阶段。\n"
                    "⚠ 当前报告是系统自动生成的简版占位（信息有限）。\n"
                    f"⛔ 优先动作：调用 write_file，基于 <diagnosis_ledger> 中的"
                    f"证据链撰写完整诊断报告并{_overwrite_hint}。\n"
                    "⛔ 报告内容只能通过 write_file 写入文件——"
                    "禁止在对话里输出完整报告正文。\n"
                    "- 若确认简版已足够，可跳过 write_file\n"
                    f"- 最后{summary_req}\n"
                    "- 总结面向运维工程师——禁止提及台账、报告文件路径等内部实现细节"
                )
            return (
                "你当前处于 REPORT 阶段（诊断报告已写入）。\n"
                "- 报告已保存，无需再调用 write_file / record_finding / commit_hypotheses\n"
                f"- 请{summary_req}\n"
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
            fmt_hid(h["id"]) for h in ledger.get("hypotheses", {}).values()
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
            f"- {duty}"
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
    (v2.7 flat model: no ancestor shield — there are no sub-hypotheses.)
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


def _economically_dead(node: HypothesisNode) -> bool:
    """软终态（v2.9）：未决但验证增益已低于成本（delegation_value < κ）。

    A hypothesis that is still pending/inconclusive but can no longer be
    delegated (G5 would hard-block it) must not block direction changes:
    it neither blocks the retry-batch gate (can_commit) nor keeps the
    layer out of T8.  Its STATUS stays pending/inconclusive — the
    record_finding honesty semantics are unchanged; only the direction
    logic treats it as dead.  deferred nodes are NOT dead: an explicit
    shelve is a revivable direction (G9 revival channel), matching the
    E2′ exclusion.
    """
    if node.get("status") not in ("pending", "inconclusive"):
        return False
    if node.get("deferred"):
        return False
    return delegation_value(node) < KAPPA_STOP


def max_action_value(ledger: DiagnosisLedger) -> tuple[float, str]:
    """Max value over still-executable verification actions.

    Actions considered (deterministic, ledger-derived):
    - delegation on each pending (non-deferred) / inconclusive hypothesis
    - batch retry (if budget open): fresh hypotheses carry U≈1, D=1, F=1;
      NOT offered once any hypothesis is confirmed — batch retry exists
      for a failed direction, keeping it as an "executable action" would
      block S2 forever while the budget is open even though a root cause
      is already located
    Returns (value, detail) — detail names the best action for logging.
    (v2.7 flat model: no ancestor shield — there are no sub-hypotheses.)
    """
    hypotheses = ledger.get("hypotheses", {})
    has_confirmed = any(
        n.get("status") == "confirmed" for n in hypotheses.values()
    )
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
                f"委派验证 {fmt_hid(hid)} (p={node.get('probability')}%, "
                f"已委派{node.get('_delegate_count', 0)}次/"
                f"inconclusive {node.get('_inconclusive_count', 0)}次)"
            )
    if (ledger.get("root_hypothesis_ids") and can_commit_root_hypotheses(ledger)
            and not has_confirmed):
        v = 1.0 / COST_BATCH_COMMIT  # fresh batch: U=1, D=1, F=1
        if v > best_v:
            best_v = v
            best_d = "换批提交新假设"
    return best_v, best_d


# ── Exit condition checking ──

def check_exit_conditions(ledger: DiagnosisLedger) -> tuple[bool, str, str | None]:
    """Check if diagnosis should exit to REPORT.

    Returns (should_exit, reason, confirmed_hypothesis_id).

    v2.8 flat model (state-machine-v2.md §6):
      exit = S1 (把握足够) AND S2 (无可行动验证), plus fast lanes:
      - E3 evidence saturation (3 consecutive inconclusive)
      - E2 exhaustion (all roots refuted, batch budget spent)
    S1: ≥1 confirmed root with p≥80
        (v2.7: no deep-confirmation fallback — sub-hypotheses removed,
        every hypothesis is a root.)
    S2: max action value EIG/c < κ=0.15, or no executable actions left.
        Residual uncertainty R is observational only (v2.8) — it cannot
        distinguish resolvable ambiguity from economically dead ambiguity,
        so actionability (gain ≥ κ) is the gate.  Any undecided hypothesis
        with gain ≥ κ could be a competing root cause → still verified;
        once all are dead, exit and mark leftovers "未验证" in the report.
    """
    hypotheses: dict = ledger["hypotheses"]
    if not hypotheses:
        return False, "", None

    # ── E3 evidence saturation (fast lane): global, takes precedence ──
    if ledger.get("_inconclusive_streak", 0) >= 3:
        return True, "证据饱和: 连续3次验证结果不明确", None

    root_ids: list[str] = ledger.get("root_hypothesis_ids", [])
    # v2.8.4: scan ALL hypotheses, not just the current batch.  The flat
    # model makes every hypothesis a root (§6.3), and a prior-batch
    # hypothesis can be revived (select_path accepts any valid ID, the
    # G9 valve scans tree-wide, task() auto-heal targets any node) and
    # confirmed AFTER a retry batch replaced root_hypothesis_ids.  A
    # batch-scoped scan is blind to that confirmation: exit is refused
    # with "尚无确认根因" while root_causes (registered tree-wide by the
    # record_finding tool handler) already lists it — and E2 can even
    # fire "假设穷尽/未确认根因" over a confirmed node (reproduced
    # 2026-07-31: confirm revived H2@85 post-retry → S1 blind).
    # E2/E2′ stay batch-scoped internally (exhaustion of the CURRENT
    # direction); their `not confirmed_ids` precondition now correctly
    # suppresses them whenever any confirmation exists tree-wide.
    confirmed_ids = [
        hid for hid, node in hypotheses.items()
        if node.get("status") == "confirmed"
        and node.get("probability", 0) >= 80
    ]

    # ── E2 exhaustion (fast lane): all roots refuted, budget spent ──
    if root_ids and not confirmed_ids:
        all_refuted = all(
            hypotheses.get(hid, {}).get("status") in _HARD_FAILED_STATUSES
            for hid in root_ids
        )
        if all_refuted and not can_commit_root_hypotheses(ledger):
            return True, (
                f"假设穷尽: 提交预算已用尽（{MAX_COMMIT_CALLS} 次），"
                "所有假设均已证伪，未确认根因"
            ), None
        # ── E2′ economic exhaustion (fast lane) ──
        # Batch budget strictly spent AND every surviving root is
        # economically dead (delegation_value < κ — G5 would reject any
        # further delegation anyway): E2's "无可执行动作" semantics
        # completed for the terminal mix the refuted-only check misses
        # (e.g. H1–H5 refuted + H6 inconclusive whose value decayed below
        # κ after one delegation + one inconclusive verdict; observed
        # 2026-07-30 production: write_file gate-rejected, LLM detoured
        # through backtrack for three rounds).  Deferred survivors still
        # block — explicitly shelved = revivable direction (G9 handles).
        # The budget test uses the raw GLOBAL commit count, NOT the
        # can_commit proxy (an active pending node would poison that
        # proxy).
        if ledger.get("_commit_count", 0) >= MAX_COMMIT_CALLS:
            survivors = [
                node for hid in root_ids
                if (node := hypotheses.get(hid)) is not None
                and node.get("status") not in _HARD_FAILED_STATUSES
            ]
            if survivors and all(
                    node.get("status") in ("pending", "inconclusive")
                    and not node.get("deferred")
                    and delegation_value(node) < KAPPA_STOP
                    for node in survivors):
                return True, (
                    f"假设穷尽: {MAX_ROOT_COMMIT_BATCHES} 批假设预算用尽，"
                    "残余未决假设的验证增益均低于成本（κ），未确认根因"
                ), None

    # ── S1: confidence sufficient ──
    if not confirmed_ids:
        # No confident root cause yet — never exit on the gain<cost
        # rule alone; the LLM still owes a direction (retry / backtrack).
        # Non-empty reason: consumed by the write_file gate rejection
        # message (ledger_middleware.awrap_tool_call).
        return False, (
            "尚无确认根因（无 p≥80 的 confirmed 假设），"
            "需先完成假设验证并 record_finding"
        ), None
    # ── S2: actionable verification left? (R is no longer a hard gate) ──
    # Residual uncertainty R alone cannot distinguish "ambiguity worth
    # resolving" from "ambiguity that is economically dead" — a hypothesis
    # may sit at p=50 (U=1.0) yet be unverifiable with the available data
    # (observed 2026-07-30 session d06c02d2: H2 "which process triggered
    # the IO" stayed inconclusive, delegation_value 0.0625 < κ, and the
    # R=1.0 hard gate blocked write_file 6 times over 15 wasted rounds).
    # Gate exit on ACTIONABILITY instead: only block when some undecided
    # hypothesis still offers gain ≥ cost — such a hypothesis could be a
    # competing root cause and must be verified (multi-root protection).
    # When every leftover is economically dead, continuing would be
    # fruitless spinning → exit; leftovers are marked "未验证" in the
    # report by finalize_pending_for_report.  Termination is guaranteed:
    # delegation_value = U·0.5^(n_deleg+n_inc)/c decays monotonically
    # with each attempt, so max_action_value eventually drops below κ.
    best_v, best_d = max_action_value(ledger)
    r = residual_uncertainty(ledger)
    if best_v >= KAPPA_STOP:
        return False, (
            f"已有确认根因 {fmt_hid(confirmed_ids[0])}，但最高动作价值 "
            f"{best_v:.3f} ≥ κ={KAPPA_STOP}（{best_d}），仍有可行动验证"
            f"（残余不确定性 R={r:.2f}），继续验证以避免漏掉竞争性根因"
        ), confirmed_ids[0]

    best = hypotheses[confirmed_ids[0]]
    return True, (
        f"根因确认: {fmt_hid(best['id'])} {best['statement']} "
        f"(p={best['probability']}%) | 退出判据满足: 已确认根因且"
        f"无可行动验证（最高动作价值 {best_v:.3f}<κ={KAPPA_STOP}，{best_d}；"
        f"残余不确定性 R={r:.2f} 源于经济上已死的未决假设，"
        f"报告中标注为未验证）"
    ), confirmed_ids[0]
