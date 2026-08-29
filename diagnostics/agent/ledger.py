"""Diagnosis ledger data structures, rendering, and serialization.

The ledger is a structured diagnosis memory persisted in LangGraph state.
It holds the hypothesis tree, evidence chain, and diagnosis round history.

"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from typing import Annotated, Literal, NamedTuple, NotRequired

from deepagents.graph import DeepAgentState
from langchain.agents.middleware.types import PrivateStateAttr

from diagnostics.agent.prompt import PHASE_SPEC  # P6/§9: duty/exit 单源（无循环依赖：prompt 不导入本模块）


# ── Phase & status types ──

# ── Phase & status types ──
# 5-phase state machine (design document):
# understand → hypothesize → verify → evaluate → report.
# backtrack/skill_verify are NOT phases: backtrack is an EVALUATE action,
# skill mode is delegation guidance inside VERIFY.
DiagnosisPhase = Literal[
    "understand", "hypothesize", "verify", "evaluate", "report",
]

# 5-state model (design document §5, v2.1 simplified):
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

# ── Hypothesis propose budget (GLOBAL, hard-coded) ──
# At most this many propose_hypotheses CALLS per diagnosis — first batch
# and retry batch (换批) BOTH consume from this single budget.  With the
# per-call limit of MAX_LAYER_SIZE, a diagnosis can never exceed
# MAX_PROPOSE_CALLS × MAX_LAYER_SIZE = 6 hypotheses in total.
# (v2.7 flat model: sub-hypotheses / deepening were removed — every
# propose creates ROOT hypotheses only.)
MAX_PROPOSE_CALLS = 2

# ── Root-hypothesis batch gate ──
# A retry batch of root hypotheses is only allowed after ALL root
# hypotheses from prior batches are hard-failed (refuted/dead_end) —
# and only while the global propose budget above is not exhausted.
MAX_ROOT_PROPOSE_BATCHES = 2

# ── Hypothesis-list hard invariants (deterministic, code-enforced) ──
# These are BUSINESS invariants — they cannot be expressed by tool-input
# schemas (Pydantic validates a single call, not cumulative state),
# so they are enforced here at the data-structure layer.  Rationale and
# evidence: design document §11; the 2026-07-20
# 32-round session (root layer grew to 6 via batch accumulation).
MAX_LAYER_SIZE = 3
"""Max hypotheses per propose call AND per current batch.  A retry batch
REPLACES the current root layer instead of appending.  v2.7: the model
is flat (no sub-hypotheses), so this is simply the per-batch cap."""

# ── Total-hypothesis hard cap (outer convergence bound) ──
# The documented invariant MAX_PROPOSE_CALLS × MAX_LAYER_SIZE bounds the
# TOTAL number of hypotheses per diagnosis.  v3.10.0: the evidence-driven
# append channel (T10′) shares this cap — appends consume NO batch budget,
# so this total cap (together with F-decay and the round caps) is what
# keeps termination intact when hypotheses are added mid-diagnosis.
MAX_TOTAL_HYPOTHESES = MAX_PROPOSE_CALLS * MAX_LAYER_SIZE

# Statuses that permanently close a hypothesis with a negative verdict.
# (dead_end merged into refuted in the 5-state model.)
_HARD_FAILED_STATUSES = frozenset({"refuted"})

# ── Confirmed-root predicate (design document §6 S1) ──
# A "confirmed root cause" requires BOTH a confirmed verdict AND
# probability ≥ 80 — the record_finding contract states "低于 80 的
# confirmed 不被视为已确认根因", and S1's exit semantics are "a
# confirmed root cause with p ≥ 80".  The root_causes tracker and its
# cleanup pass MUST share this predicate with S1 so a low-confidence
# confirmed hypothesis (e.g. a synonymous hypothesis stamped confirmed
# with p=0) can never surface as a "root cause" while S1 refuses to
# count it — the two would otherwise drift (observed 2026-08-14,
# scenario 36: root_causes=2 but S1 only recognized 1).
CONFIRMED_ROOT_MIN_PROBABILITY = 80


def is_confirmed_root(node: dict) -> bool:
    """True iff ``node`` qualifies as a confirmed root cause (S1 semantics)."""
    return (
        node.get("status") == "confirmed"
        and node.get("probability", 0) >= CONFIRMED_ROOT_MIN_PROBABILITY
    )

# ── Quantified exit model (design document §6, v2.1) ──
# 退出时机 = 继续验证的期望信息增益 < 成本，且当前把握足够。
# Value of a verification action: value(a) = EIG(a) / c(a), where
#   EIG(a) = U(h) × D(a,h) × F(a,h)
#     U(h)   residual uncertainty of the target hypothesis
#            = 4·p·(1-p), p = probability/100  (1.0 at p=50, ~0 at extremes)
#     D(a,h) discriminativeness: 1.0 targeted delegation / 0.5 indirect
#     F(a,h) freshness decay = 0.5^n_prior_delegations(h) (inconclusive
#            verdicts add an extra halving each — D6)
#   c(a)   cost in round-equivalents (D3): delegation 2.0 / state-op 0.5 /
#          batch propose 1.0 / direct query 0.3
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
COST_BATCH_PROPOSE = 1.0
COST_QUERY = 0.3

_EPISTEMIC_BONUS = 2.0
"""First-delegation epistemic bonus for unverified hypotheses.

LLM self-reported probabilities suffer from systematic overconfidence
(Chhikara 2025, TMLR, arXiv:2502.11028 — 9 LLMs tested, all miscalibrated).
U(h)=4·p·(1-p) captures only aleatoric uncertainty; an unverified hypothesis
(_delegate_count=0, pending/inconclusive) also carries epistemic uncertainty
that the first delegation resolves.  The 2x bonus ensures delegation_value
>= kappa for p <= 0.96, preventing premature exit in multi-root-cause
scenarios where the second root cause is created with high prior probability
but never delegated (cf. scenario 25: H5 p=95, value 0.095 < kappa -> S2 exits)."""

# LLMs sometimes prefix hypothesis statements with the ledger ID they
# expect (e.g. "H1: 磁盘故障"), producing doubled prefixes in rendering
# ("H1 [refuted] H1: 磁盘故障") and guidance ("聚焦验证 H1: H1: ...").
# Strip such prefixes at propose time.  A trailing separator is required
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
    """Extend deepagents state with private diagnosis fields."""

    _diagnosis_ledger: Annotated[
        NotRequired[DiagnosisLedger | None],
        PrivateStateAttr,
    ]
    # Transient stall guidance: set by after_model when a stall is detected,
    # consumed by awrap_model_call on the NEXT round (injected into the
    # system message), then cleared.  Ordinary LastValue (NOT EphemeralValue)
    # — the value must survive the before_model super-step to reach
    # awrap_model_call, which EphemeralValue clears too early (verified by a
    # controlled test; see .local/scripts/tests/test_jump_to_guidance.py).
    _stall_guidance: Annotated[
        NotRequired[str | None],
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
        _root_propose_count=0,
        _propose_count=0,
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
        _statement_only_updates=0,  # pure wording refinements (verdict omitted)
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
    propose_hypotheses invocation (nodes are inserted as they are
    created) never collide.
    """
    max_n = 0
    for key in hypotheses:
        n = _parse_hypothesis_num(key)
        if n is not None and n > max_n:
            max_n = n
    return str(max_n + 1)


# ── Hypothesis tree operations ──

def can_propose_root_hypotheses(ledger: DiagnosisLedger) -> bool:
    """Check if a new batch of ROOT hypotheses may be submitted.

    Budget rules (see ``MAX_ROOT_PROPOSE_BATCHES``, v2.1 H2 relaxed):

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
    - Any batch additionally requires the GLOBAL propose budget
      (``MAX_PROPOSE_CALLS``) to still be open — derive_phase (T8) and the
      backtrack tool both route through this predicate, so folding the
      global check in here keeps every caller coherent.
    """
    if ledger.get("_propose_count", 0) >= MAX_PROPOSE_CALLS:
        return False
    if ledger.get("_root_propose_count", 0) >= MAX_ROOT_PROPOSE_BATCHES:
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
                and not _economically_dead(ledger, hid)):
            return False
    return True


def can_append_hypothesis(ledger: DiagnosisLedger) -> bool:
    """Check if ONE evidence-driven hypothesis may be appended to the
    current batch (design document §5 T10′, v3.10.0).

    Distinct from the retry-batch path (T8): an append adds a single new
    hypothesis while the current batch still has ACTIVE PENDING
    hypotheses — new evidence pointing at a direction outside the current
    differential must not wait for the whole batch to close (iterative
    hypothesis testing: candidates are evidence/conflict driven, not
    batch driven).  Guards:

    - a batch already exists (the first batch uses the normal propose
      path);
    - no confirmed hypothesis anywhere (a confirmed root cause closes
      hypothesis generation — refinement goes through
      record_finding(statement_update=...));
    - the batch propose budget is still open (an exhausted budget closes
      ALL hypothesis creation, appends included);
    - total hypotheses below MAX_TOTAL_HYPOTHESES — the outer
      convergence bound (appends consume no batch budget, so the total
      cap is what bounds hypothesis creation).

    Callers must prefer the batch path whenever
    can_propose_root_hypotheses holds (no active pending) — append is the
    channel for the active-pending case only.
    """
    hypotheses = ledger.get("hypotheses", {})
    if not hypotheses:
        return False
    if len(hypotheses) >= MAX_TOTAL_HYPOTHESES:
        return False
    if ledger.get("_propose_count", 0) >= MAX_PROPOSE_CALLS:
        return False
    if ledger.get("_root_propose_count", 0) >= MAX_ROOT_PROPOSE_BATCHES:
        return False
    for node in hypotheses.values():
        if node.get("status") == "confirmed":
            return False
    return True


def _propose_budget_exhausted_message(ledger: DiagnosisLedger) -> str:
    """Shared rejection text when the global propose budget is spent."""
    return (
        f"假设提出预算已用尽（全程最多 {MAX_PROPOSE_CALLS} 次，"
        f"已提出 {ledger.get('_propose_count', 0)} 次）。不可再提出新假设。"
        "如需细化已有假设，请用 record_finding(statement_update=...) "
        "修正其表述；如需调整验证方向，用 select_path 切换焦点 / "
        "backtrack 恢复搁置假设；全部假设证伪时系统会自动进入 "
        "REPORT（T9），无需手动提出。"
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
    if len(hypothesis_specs) > MAX_LAYER_SIZE:
        raise ValueError(
            f"最多{MAX_LAYER_SIZE}个假设，收到 {len(hypothesis_specs)} 个。"
            "请只提出可能性最大的3个。"
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
                f"而非 propose_hypotheses 重复提出。"
            )

    # ── Propose budget (global) + retry-batch gate / append channel ──
    # Checked AFTER dedup so a rejected duplicate does not consume
    # budget.  can_propose_root_hypotheses folds in the global
    # MAX_PROPOSE_CALLS check, so budget exhaustion surfaces first;
    # a confirmed root anywhere closes hypothesis creation entirely;
    # the remaining False case is an ACTIVE PENDING root — that is the
    # evidence-driven append channel (T10′, v3.10.0): ONE new hypothesis
    # joins the current batch WITHOUT consuming the batch budget (the
    # MAX_TOTAL_HYPOTHESES cap is the outer convergence bound).
    append_mode = False
    if not can_propose_root_hypotheses(ledger):
        if (ledger.get("_propose_count", 0) >= MAX_PROPOSE_CALLS
                or ledger.get("_root_propose_count", 0)
                >= MAX_ROOT_PROPOSE_BATCHES):
            raise ValueError(_propose_budget_exhausted_message(ledger))
        if any(n.get("status") == "confirmed" for n in hypotheses.values()):
            raise ValueError(
                "已有 confirmed 假设（根因已确认），不可提出新假设。"
                "如需细化其表述，请用 record_finding(statement_update=...)；"
                "若你的意图是确认某个已有假设为根因，请直接对该假设调用 "
                "record_finding(verdict='confirmed')——重复提出不会确认它。"
            )
        # Active pending exists → append channel (T10′).
        if len(hypotheses) >= MAX_TOTAL_HYPOTHESES:
            raise ValueError(
                f"假设总数已达上限 {MAX_TOTAL_HYPOTHESES} 个，不可追加。"
                "请用 record_finding 终结或修正已有假设。"
            )
        if len(hypothesis_specs) > 1:
            raise ValueError(
                "证据驱动追加通道单次仅限 1 个新假设"
                f"（收到 {len(hypothesis_specs)} 个）。"
                "若意图是换批（当前方向全部失败后的重开），"
                "deferred/inconclusive/经济死亡的假设不阻塞换批——"
                "请先用 record_finding 处理活跃 pending 假设再提出。"
            )
        append_mode = True

    # Total-cap enforcement for batch proposes — MUST precede any ledger
    # mutation so a rejected call consumes nothing (the cap can bind
    # before the per-call limit once appends have joined).
    if not append_mode:
        _remaining = MAX_TOTAL_HYPOTHESES - len(hypotheses)
        if len(hypothesis_specs) > _remaining:
            raise ValueError(
                f"假设总数上限 {MAX_TOTAL_HYPOTHESES} 个"
                f"（已有 {len(hypotheses)} 个），"
                f"本次最多可提出 {_remaining} 个。"
            )

    if append_mode:
        # T10′ append: consume NO batch budget; the appended hypothesis
        # JOINS the current batch (root_hypothesis_ids extended below).
        ledger["_append_count"] = ledger.get("_append_count", 0) + 1
    else:
        ledger["_root_propose_count"] = ledger.get("_root_propose_count", 0) + 1

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

        # ── Global propose budget consumption ──
        # Every successful BATCH propose — first batch or retry batch —
        # consumes exactly one unit (appends do not, v3.10.0 T10′).
        ledger["_propose_count"] = ledger.get("_propose_count", 0) + 1

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

    # ── T5 guard (design document §5): the target must be decidable ──
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
    verdict: Verdict | None,
    evidence_summary: str,
    probability_update: int | None,
    new_insights: str = "",
    statement_update: str = "",
) -> None:
    """Record a verification finding for a hypothesis.

    Args:
        verdict: confirmed/refuted/inconclusive.  When None, this is a
            PURE statement refinement (iterative-hypothesis-update): only
            the statement is replaced, no verdict is formalized and no
            verification state advances (probability/status/evidence are
            untouched).
        statement_update: If provided, replaces the hypothesis statement
            with a more accurate version discovered during verification
            (e.g. "etcd compaction" → "etcd NOSPACE alarm").
    """
    hypotheses = ledger["hypotheses"]
    hypothesis_id = resolve_hypothesis_id(ledger, hypothesis_id)

    node = hypotheses[hypothesis_id]

    # Update statement if a more accurate version was found during verification
    if statement_update:
        node["statement"] = statement_update

    # Pure statement refinement (verdict omitted): wording-only update that
    # does not advance verification state.  Bounded by the middleware B′
    # guard (≤2 per hypothesis); here we only bump the counter and return.
    if verdict is None:
        node["_statement_only_updates"] = node.get("_statement_only_updates", 0) + 1
        return

    node["probability"] = max(0, min(100, probability_update or 0))

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

    # State-driven guidance fact fields (design document §10, v3.10.1):
    # _last_finding_round records when the last verdict was formalized;
    # _g17_blocked_round is cleared (the G17 recovery path is resolved).
    # These feed compute_next_action's "awaiting verdict" detection.
    node["_last_finding_round"] = ledger.get("current_round", 0)
    node["_g17_blocked_round"] = None

    if new_insights:
        node["rationale"] = (node.get("rationale", "") + " | 新发现: " + new_insights).strip(" |")


def add_evidence_to_active(
    ledger: DiagnosisLedger,
    source: str,
    summary: str,
    supports: bool | None = None,
    tool_call_id: str | None = None,
    tool_name: str | None = None,
    structured: dict | None = None,
) -> None:
    """Add evidence to the current active hypothesis (auto from tool calls).

    *structured* (optional) preserves the raw structured expert return
    (prompt-contract JSON) on the evidence entry for exact
    downstream parsing (verdict inference / argus classification).
    """
    if not ledger["active_path"]:
        return
    active_id = ledger["active_path"][-1]
    node = ledger["hypotheses"].get(active_id)
    if node is None:
        return
    _ev = new_evidence(source, summary, supports, tool_call_id)
    if structured is not None:
        _ev["structured"] = structured
    node["evidence"].append(_ev)
    if tool_name and tool_name not in node["verification_tools"]:
        node["verification_tools"].append(tool_name)


# ── Phase derivation ──
# 5-phase state machine (design document §3).
# The phase is NEVER persisted: ``derive_phase`` is the single source of
# truth, called by rendering, tool gating, and guardrails alike.
#
# Transition table (normal flow):
#
#   | from        | event / guard                                    | to          |
#   |-------------|--------------------------------------------------|-------------|
#   | understand  | T1  coverage ready (system)                      | hypothesize |
#   | hypothesize | T10 propose_hypotheses success                    | verify      |
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

    Decision order (fixed, design document §3):
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
                or _economically_dead(ledger, hid)
                for hid in root_ids)):
        if can_propose_root_hypotheses(ledger):
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


def _required_argus(ledger: DiagnosisLedger) -> set[str]:
    """Required Argus experts for the UNDERSTAND coverage gate (D1).

    Mirrors the coverage rules in ``_coverage_ready`` / ``_serverless_coverage_ready``:
    Serverless four-view, host single, dedicated container dual.  Used by the
    proactive UNDERSTAND coverage-status hint (design document §5).
    """
    if ledger.get("topology") or ledger.get("_topology_unavailable"):
        return {
            "serverless-argus-expert", "kmc-argus-expert",
            "sci-argus-expert", "host-argus-expert",
        }
    if ledger.get("entity_type", "") == "host":
        return {"host-argus-expert"}
    return {"host-argus-expert", "k8s-argus-expert"}


def _coverage_ready(ledger: DiagnosisLedger) -> bool:
    """UNDERSTAND→HYPOTHESIZE gate (T1): required Argus data collected.

    Rules (see design document §4):
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
    # was exhausted), the coverage gate is bypassed — the LLM proposes
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
        # data-less proposes: the §7 gate blocks the very task()
        # delegation the prompt and the G2 valve demand (observed
        # 2026-07-30 session f62a11eb: round-1 skill reads — legal per
        # prompt — then the round-2 flip hard-blocked both argus
        # delegations, forcing 3 data-less hypotheses at round 3).
        if not _argus_target_resolved(ledger):
            return True
        return (ledger.get("current_round", 0)
                >= _NO_DELEGATION_FALLBACK_ROUND)
    # Serverless scene (k8s-on-k8s): topology-aware coverage gate (D1/D2,
    # see design document §7.2).  Topology injection marks the scene; on
    # topology failure (_topology_unavailable) degrade to argus-only.
    if ledger.get("topology") or ledger.get("_topology_unavailable"):
        return _serverless_coverage_ready(ledger)
    if ledger.get("entity_type", "") == "host":
        return "host-argus-expert" in experts_delegated
    return ("host-argus-expert" in experts_delegated
            and "k8s-argus-expert" in experts_delegated)


def _serverless_coverage_ready(ledger: DiagnosisLedger) -> bool:
    """Serverless coverage gate (design document §7.2 D1/D2).

    D1 (topology available): the four serverless view-layer argus experts
    (logical serverless / KMC control-plane / SCI data-plane / physical host)
    must ALL be delegated — four-view coverage before hypothesizing across
    layers (Serverless workloads run on SCI physical nodes; host-argus covers
    the physical-layer metrics that kmc/sci node overviews do not split).
    D2 (topology failed): same four-expert judgement without the topology
    requirement (mirror of ``_argus_unavailable``).
    """
    if not ledger.get("_topology_unavailable") and not ledger.get("topology"):
        return False  # topology still pending — keep UNDERSTAND collecting
    experts_delegated: set[str] = set()
    for rd in ledger.get("rounds", []):
        experts_delegated.update(rd.get("delegated_experts", []))
    required = {
        "serverless-argus-expert", "kmc-argus-expert",
        "sci-argus-expert", "host-argus-expert",
    }
    return required.issubset(experts_delegated)


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
            # Idempotent: a node already shelved (deferred, selection
            # cleared, reason recorded) needs no further mutation — the
            # caller logs/persists only on actual change.
            if node.get("deferred") and not node.get("selected") \
                    and node.get("deferred_reason"):
                continue
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
    "propose_hypotheses", "select_path", "record_finding", "backtrack",
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
    if ledger.get("_propose_count"):
        batch_info = (
            f" | 假设提出: {ledger['_propose_count']}"
            f"/{MAX_PROPOSE_CALLS} 次"
            f"（共 {len(ledger.get('hypotheses', {}))}"
            f"/{MAX_PROPOSE_CALLS * MAX_LAYER_SIZE} 个）"
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


# Argus targeted-recheck channel note (design document §9, v3.5.1):
# argus experts are the ONLY channel to monitoring time series (deep
# experts carry no query_argus_* tools).  The note makes the channel
# visible in VERIFY without listing argus experts alongside the deep
# expert map (PG4 brevity): a hypothesis-driven targeted re-query —
# specific time window/metric/object — yields new information, not a
# repeat of UNDERSTAND's broad coverage sweep (2026-08-08 scenario-35
# empirical: decisive evidence came from kmc-argus in VERIFY).  Abuse
# is bounded by the view-aware G5 gate + dedup; no hard gate needed.
_ARGUS_CHANNEL_NOTE_SUFFIX = (
    "——与 UNDERSTAND 的广覆盖采集不同，须带假设聚焦的具体问题\n"
)
_ARGUS_CHANNEL_NOTE_SLS = (
    "- 需定向复核监控时序（特定时间窗/指标/对象）→ 对应视角 argus 专家"
    "（serverless/kmc/sci/host-argus-expert）"
    + _ARGUS_CHANNEL_NOTE_SUFFIX
)

# ── Serverless 委派映射：层归属 + 观测域/证据通道（design document §9,
# v3.20.0; 2026-08-28 scenario 39 post-mortem) ─────────────────────────
# Structural-ownership routing alone is insufficient: a component's
# TOPOLOGICAL owner is not always the owner of its EVIDENCE CHANNEL.
# Empirically: virtualNode is a KMC control-plane component, so the
# topo-derived map routes "VK sync-link anomaly" to kmc-expert — yet
# kmc-expert's toolset (get_kmc_deployments/pods/pod_logs/etcd_status/
# apigateway/group1/ipam/vpc_cni) has NO logical-layer event channel,
# while the decisive evidence (SyncFailed events, vk_sync_failure_count)
# lives in get_serverless_events — serverless-expert only.  The expert
# then reported "no vk_sync_failure_count spike observed", i.e. an
# INABILITY TO SEE rendered as a positive negative finding
# (argument from absence in its least detectable form); the correctly
# directed H1 was refuted, cascading into 5 refutations and a 3600s
# timeout without convergence.
# The remedy is a POSITIVE routing recipe (§9 P5): name each expert's
# observation domain so the coordinator checks evidence-channel coverage
# BEFORE delegating.  Single source of truth: the expert toolsets in the
# mock tool registry — test_sls_expert_domain_map.py asserts this text
# stays consistent with the actual tool bindings (doc-drift guard).
_SLS_EXPERT_DOMAIN_MAP = (
    "- Serverless 场景委派映射（层归属 + 观测域）：委派前先明确期望从该专家"
    "获得哪条证据，并确认该证据在其观测域内——若不在，改派观测域覆盖该证据的专家\n"
    "  · 逻辑集群：Deployment/Pod 状态与日志、**逻辑层事件**（VK 同步状态、"
    "Pod 生命周期事件、同步失败计数）→ serverless-expert\n"
    "  · KMC 控制面组件与共享组件链路：控制面 Deployment/Pod 与日志、共享 etcd、"
    "API Gateway / Group1 / IPAM / VPC-CNI → kmc-expert"
    "（无逻辑层事件通道：查证 VK 同步状态/同步失败计数须走 serverless-expert）\n"
    "  · SCI 数据面：burst Pod 与日志、节点/kubelet、VPC-CNI、IP 分配 → sci-expert\n"
    "  · 物理节点/主机：CPU/内存/磁盘/网络 → host-expert\n"
)


# ── Argus 参数澄清契约（design document §8 G2 / §9, v3.11.1）─────────
# 契约化标记（非关键词枚举）：argus 专家返回契约要求参数澄清回执以该
# 标记开头，给协调者侧分类器一个确定性信号（规避关键词穷举隐患——
# 同 §11 S1 否决理由）；旧措辞保留为契约采纳前的回退兼容。
ARGUS_CLARIFICATION_MARKER = "【需要参数】"
ARGUS_PARAM_MISSING_PATTERNS = (
    ARGUS_CLARIFICATION_MARKER,
    "需要补充",
    "需要更具体的信息",
    "需要 hostname",
)


def argus_param_clarification_pending(
        ledger: DiagnosisLedger) -> list[str]:
    """已委派但返回参数澄清（未返回监控数据）的 argus 专家名单
    （design document §9, v3.11.1）。同一专家后续返回实质数据则解除
    pending 状态。供 HYPOTHESIZE 数据缺口补救指引的选择性注入。"""
    pending: list[str] = []
    for rnd in ledger.get("rounds", []):
        kf = str(rnd.get("key_findings", ""))
        for expert in rnd.get("delegated_experts", []):
            if not str(expert).endswith("argus-expert"):
                continue
            if any(p in kf for p in ARGUS_PARAM_MISSING_PATTERNS):
                if expert not in pending:
                    pending.append(expert)
            elif kf.strip() and expert in pending:
                pending.remove(expert)
    return pending


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
        # Scene-aware delegation rule (design document §9, 2026-08-05):
        # only the CURRENT scene's required Argus set is shown.  The LLM
        # must not see other scenes' delegation rules (avoids cognitive
        # load and scene misclassification); the set is sourced from
        # _required_argus — the same single source of truth as the D1
        # coverage gate and the reactive coverage valve (§8).
        if ledger.get("topology") or ledger.get("_topology_unavailable"):
            delegation_rules = (
                "- 单轮并行委派四个 Argus 专家（Serverless 四视角：逻辑/控制面/"
                "数据面/物理主机）：\n"
                "  task(subagent_type=\"serverless-argus-expert\") + "
                "task(subagent_type=\"kmc-argus-expert\") + task(subagent_type=\"sci-argus-expert\") "
                "+ task(subagent_type=\"host-argus-expert\")\n"
                "  四视角 argus 时序齐备才判数据齐、可进入 HYPOTHESIZE\n"
            )
        elif ledger.get("entity_type", "") == "host":
            delegation_rules = (
                "- 仅委派 task(subagent_type=\"host-argus-expert\", "
                "description=...) 采集主机指标\n"
            )
        else:
            delegation_rules = (
                "- 单轮并行委派两个 Argus 专家：task(subagent_type=\"host-argus-expert\") "
                "+ task(subagent_type=\"k8s-argus-expert\")\n"
                "  即使症状仅点名集群/Pod/DNS/节点也必须双委派——主机节点资源"
                "（CPU/内存/磁盘 IO）是集群故障的底层维度，缺一则覆盖门控判数据不齐\n"
            )
        # Proactive coverage-status hint (design document §5): name the argus
        # experts still missing so the LLM delegates them before the coverage
        # gate (D1) blocks.  Selective (only when incomplete) and actionable.
        _req = _required_argus(ledger)
        _del: set[str] = set()
        for rd in ledger.get("rounds", []):
            _del.update(rd.get("delegated_experts", []))
        _miss = _req - _del
        coverage_hint = ""
        if _miss:
            coverage_hint = (
                f"- 覆盖状态：已委派 {', '.join(sorted(_del & _req)) or '（无）'}；"
                f"仍缺 **{', '.join(sorted(_miss))}**（覆盖门控要求全部委派）。\n"
            )
        return (
            "你当前处于 UNDERSTAND 阶段。\n"
            f"- {duty}\n"
            + coverage_hint
            + delegation_rules
            + "- 委派 Argus 专家时使用上方「诊断会话参数」中的时间范围（禁止自行推断）\n"
            + "- 关注指标突变时间点和并发异常（同一分钟多条红线→可能独立根因）\n"
            "- 上方「已有工具调用结果」已含全部采集数据，直接复用即可\n"
            f"- ⚠ {exit_txt}"
        )
    if phase == "hypothesize":
        retry_hint = ""
        if ledger.get("_root_propose_count", 0) > 0 and ledger.get("hypotheses"):
            # v3.11.0: structured failure digest at the decision point
            # (design document §9) — replaces the indirect "see above"
            # pointer when prior-batch failures exist.
            digest = render_failure_digest(ledger)
            retry_hint = (
                f"\n- ⚠ 这是第 {ledger.get('_propose_count', 0) + 1}"
                f"/{MAX_PROPOSE_CALLS} 次假设提出（最后一次）。"
                "前一批已全部证伪/验证价值耗尽。\n"
                "  新假设必须基于排除证据换方向推断，"
                "禁止与已排除假设重复或仅换措辞。"
            ) + digest
        # v3.11.1: data-gap remedy hint (design document §9) — when a
        # required argus expert was delegated but returned a parameter
        # clarification (no metrics), name the legal channel BEFORE the
        # LLM attempts an in-phase re-delegation: write the gap direction
        # as a hypothesis; VERIFY re-queries with complete parameters.
        # Selective injection (P7 / Proactive Memory Agent §12).
        gap_hint = ""
        if not ledger.get("hypotheses"):
            _clarified = argus_param_clarification_pending(ledger)
            if _clarified:
                gap_hint = (
                    f"\n- ⚠ {'、'.join(_clarified)} 已委派但未返回监控数据"
                    "（请求补充参数）——该维度证据缺口请直接写成假设"
                    "（probability 体现不确定性）；VERIFY 阶段将以假设聚焦"
                    "方式定向补查（委派描述须带完整实体与参数）。"
                    "本阶段职责不变：提出假设。"
                )
        return (
            "你当前处于 HYPOTHESIZE 阶段。\n"
            f"- {duty}\n"
            f"- 提出预算：全程最多 {MAX_PROPOSE_CALLS} 次"
            f"（本次为第 {ledger.get('_propose_count', 0) + 1} 次）；"
            "预算用尽后只能用 record_finding(statement_update=...) 修正已有假设\n"
            "- ⚠ 字段契约：每条假设必须含 statement/probability/rationale 三字段，"
            "statement 必填且为一句完整根因表述——缺失会导致调用失败"
            "（失败不消耗提出预算，修正后重新提交即可）\n"
            "- ⚠ 同源合并：若多个候选描述的是同一故障的机制/因果链上下游"
            "（如\"内存超限 OOMKilled\"与\"memory limit 配置过低\"互为表里），"
            "应合并为一条假设；仅当证据显示独立故障源并存时才提出多条\n"
            "- ⚠ 多根因场景：当证据显示多个独立故障源同时存在时，"
            "不要默认奥卡姆剃刀给多根因/叠加假设赋低概率——应给合理概率"
            "（≥30%），否则会在主根因确认后被退出判据结构性搁置，"
            "导致漏判独立故障源。\n"
            f"- 完成后必须调用 propose_hypotheses 提出假设（{exit_txt}）"
            + retry_hint
            + gap_hint
        )
    if phase == "verify":
        active_id = ledger["active_path"][-1] if ledger["active_path"] else "?"
        active = ledger["hypotheses"].get(active_id, {})
        stmt = active.get("statement", "?")
        disp_id = fmt_hid(active_id) if active_id != "?" else "?"
        sls_verify = ""
        # Scene-aware expert map (design document §9, 2026-08-05): only the
        # CURRENT scene's verification experts are shown (same principle as
        # UNDERSTAND: no other-scene delegation rules in view).
        if ledger.get("topology") or ledger.get("_topology_unavailable"):
            expert_map = _SLS_EXPERT_DOMAIN_MAP + _ARGUS_CHANNEL_NOTE_SLS
        elif ledger.get("entity_type", "") == "host":
            expert_map = (
                "- 主机假设（CPU/内存/磁盘/网络/GPU）→ "
                "task(subagent_type=\"host-expert\", description=...) 委派验证\n"
                "- 需定向复核监控时序（特定时间窗/指标）→ "
                "task(subagent_type=\"host-argus-expert\")"
                + _ARGUS_CHANNEL_NOTE_SUFFIX
            )
        else:
            expert_map = (
                "- 主机假设 → task(subagent_type=\"host-expert\", description=...) 委派验证\n"
                "- K8s 假设 → task(subagent_type=\"k8s-expert\", description=...) 委派验证\n"
                "- 需定向复核监控时序（特定时间窗/指标/对象）→ "
                "host-argus-expert / k8s-argus-expert"
                + _ARGUS_CHANNEL_NOTE_SUFFIX
            )
        # Proactive next-action directive (design document
        # proactive-guidance §5.3): single render_verify_directive
        # decision-tree output, placed first (primacy) so the LLM sees
        # the right action before the duty/expert-map bullets — same
        # primacy rationale as the EVALUATE exit banner.
        _vd = render_verify_directive(ledger)
        _vd_block = (_vd + "\n") if _vd else ""
        # Sub-state-focused signal rendering (design document §9 proactive
        # guidance, 2026-08-10): the confirmed/ledgering details are only
        # exposed once the current sub-state makes them the natural next
        # action — never before (noise reduction: the LLM otherwise tends to
        # run ahead and call record_finding(confirmed) before delegating).
        #   state 1 (no delegation yet): focus delegation only; keep the
        #     refuted/inconclusive escape channel (G17 asymmetric evidence
        #     standard — screening-level counter-evidence may conclude).
        #   state 2 (delegated, no expert evidence back): phase exit contract.
        #   state 3 (expert evidence back): harvest-nudge already covers it.
        _delegated = active.get("_delegate_count", 0) or 0
        _has_expert_ev = any(
            str(e.get("source", "")).startswith("expert:")
            for e in active.get("evidence", [])
        )
        if _delegated == 0:
            _record_hint = (
                "- ⚠ 若现有监控证据已明确证伪该假设，可直接 record_finding 判"
                " refuted/inconclusive（confirmed 须以专家验证结论为依据，"
                "未委派直接 confirmed 会被系统拦截）\n"
                # P2 (design document §9 v3.20.1): the refuted coverage
                # criterion formulated as a SUCCESS CRITERION, i.e. stated
                # BEFORE the action — left-shifting what G22 backstops
                # reactively.  Positive form on purpose: prohibition-style
                # copy backfires under motivated violation (§11 v2.9.2),
                # whereas a success criterion is the model's own target.
                "- 判 refuted 的三要素齐备才可落账：①所依据的证据通道"
                "（指标 / 日志 / 事件）②查询时段覆盖故障时段 ③该通道属"
                "假设所涉实体的观测域；任一项不齐备则判 inconclusive，"
                "并在 rationale 中写清缺哪一项、为何不可取得\n"
            )
        elif _has_expert_ev:
            _record_hint = ""
        else:
            _record_hint = (
                f"- 收到结果后必须调用 record_finding 记录结论（{exit_txt}）\n"
            )
        return (
            f"你当前处于 VERIFY 阶段，聚焦验证 {disp_id}: {stmt}\n"
            + _vd_block
            + f"- {duty}\n"
            + expert_map
            + "- 委派时明确\"验证假设"
            f"{disp_id}\"并传入假设上下文\n"
            + _record_hint
            + "- ⚠ 委派专家前先检查上方「已有工具调用结果」与假设「证据」字段："
            "此前验证其他假设时若已产出相关检查数据（check_* / 指标 / 专家结论），"
            "委派时明确要求专家「复用已有证据，仅补查缺失项」\n"
        )
    if phase == "evaluate":
        # Direction is computed by the state machine and presented as
        # THE action for this step — the LLM does not choose freely.
        # Mutually exclusive by construction (design document §5):
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
                if _economically_dead(ledger, h):
                    _tag += "（增益<κ，委派已被门控——请 record_finding 终结或搁置）"
                _tags.append(_tag)
            pending_hint = (
                f"\n- ⚠ 仍有待验证的假设: "
                f"{', '.join(_tags)}，使用 select_path 切换验证"
            )
        # Proactive dead-node hint (design document §9 E2).  pending_hint
        # only renders once a root cause is confirmed; with NO confirmed root
        # cause an economically dead node is otherwise invisible — the LLM
        # cannot compute delegation_value vs κ itself and may select_path a
        # dead node straight into the G5 delegation gate.  Name the dead nodes
        # here so the reaction never has to fire.  Renders only when dead
        # nodes exist AND no root cause is confirmed yet (with one,
        # pending_hint already annotates them — no duplication, and selective
        # injection forbids always-on noise).
        dead_hint = ""
        if not root_cause_hyp_ids:
            _dead = [
                fmt_hid(hid)
                for hid, n in ledger.get("hypotheses", {}).items()
                if _economically_dead(ledger, hid)
            ]
            if _dead:
                dead_hint = (
                    f"\n- ⚠ 验证增益已低于成本（委派已被门控，勿再 "
                    f"select_path 委派验证）的假设: {', '.join(_dead)}"
                    "——请用 record_finding 依据已有证据终结（或让其保持搁置），"
                    "将验证精力集中于上方点名的阻塞假设"
                )
        _retry_option = (
            "  · 本层全部 refuted 或验证增益已低于成本（经济死亡）、"
            "提出预算未用尽 → propose_hypotheses 换方向提出新一批假设\n"
            if MAX_PROPOSE_CALLS - ledger.get("_propose_count", 0) > 0 else ""
        )
        # Evidence-driven append option (T10′, v3.10.0): shown only when
        # the append channel is actually open AND the batch path is not
        # (an open batch path already offers propose via _retry_option —
        # rendering both would present two same-tool options with subtle
        # semantic differences, an instruction-collision risk).  Positive
        # form (design document §9): name the action and its condition.
        _append_option = (
            "  · 新证据指向当前假设集之外的全新方向 → propose_hypotheses "
            "追加 1 个新假设（单次 1 个，并入当前批，不占用换批预算；"
            f"当前假设总数 {len(ledger.get('hypotheses', {}))}"
            f"/{MAX_TOTAL_HYPOTHESES}）\n"
            if can_append_hypothesis(ledger)
            and not can_propose_root_hypotheses(ledger) else ""
        )
        # Live exit-condition verdict for the LLM.  In EVALUATE the state
        # machine has already ruled out exit (derive_phase), so this is
        # always "not satisfied" — surface the REASON so the LLM understands
        # why it is NOT in REPORT yet (positive framing; the write_file
        # exit gate owns the rejection).  Without this, the ledger
        # context's "## 根因" label (populated the moment a
        # hypothesis is confirmed) misleads the LLM into thinking the
        # diagnosis is done and it should write the report.
        _should_exit, _exit_reason, _ = check_exit_conditions(ledger)
        # Parallel suggestion is rendered once and shared with VERIFY
        # (SSOT, design document parallel-delegation §4.2).
        _psug = render_parallel_suggestion(ledger)
        parallel_hint = f"\n- {_psug}" if _psug else ""
        # NB: the "what to do next" advice (select_path 验证/证伪、无法定论
        # 可 defer) lives in PHASE_SPEC evaluate duty — single source; the
        # banner only carries the live verdict + the positive next-step
        # pointer (negative injunctions belong to the gates).
        # Proactive exit-condition directive (SSOT: render_exit_directive),
        # placed FIRST so the LLM sees the blocker + phase-appropriate actions
        # before anything else (primacy — avoids the buried-banner dilution
        # observed in scenario 32 where the LLM looped on unproductive actions).
        _directive = render_exit_directive(ledger, "evaluate")
        if _directive:
            exit_banner = "\n## 退出条件诊断（系统计算，无需你判断）\n" + _directive + "\n"
        else:
            exit_banner = (
                f"\n- 出口判据当前未满足——{_exit_reason}，仍属 EVALUATE"
                "（即使台账已列出「根因」）。\n"
                "  判据满足后系统自动进入 REPORT 并提示写入报告；"
                "当前请按下方路径菜单处理未决假设。"
            )
        # State-driven action recommendation (v3.10.2, P7): compute the
        # single most valuable next action from ledger state and inject
        # it BEFORE the menu (primacy — Anthropic Context Engineering:
        # "smallest set of high-signal tokens").  The menu still lists
        # all options below as fallback.
        _eval_hint = compute_evaluate_action(ledger)
        _eval_rec = ""
        if _eval_hint:
            _eval_rec = f"- ⭐ 系统推荐：{_eval_hint.reason}\n"
        # Pre-write report constraint (v3.10.2, P7 proactive): inject the
        # consistency constraint BEFORE the LLM calls write_file, not after.
        # Anthropic Context Engineering: "pre-generation constraint injection,
        # not post-generation validation."  Only when no confirmed root cause
        # exists (honest-negative exit path).
        _report_cons = compute_report_guidance(ledger)
        _report_banner = f"\n- {_report_cons}\n" if _report_cons else ""
        return (
            "你当前处于 EVALUATE 阶段。\n"
            + exit_banner +
            _report_banner +
            f"- {duty}\n"
            + _eval_rec +
            "- 下一步路径菜单：\n"
            "  · 现有证据已可判定某未决假设 → 直接 record_finding 记录结论"
            "（跨假设证据复用，无需重复委派——比 select_path 更省轮次；"
            "仅限该假设已有专家验证结论的情形，confirmed 须以 expert 证据为依据，"
            "未委派假设直接 confirmed 会被系统拦截）\n"
            "  · 证据不足、需继续验证 → select_path 切换到最可能的假设\n"
            "- ℹ 若你在本阶段直接委派 task（描述指向某个未决假设，如"
            "\"验证假设Hx\"），系统会自动聚焦该假设进入 VERIFY——等价于 "
            "select_path 切换 + 委派一步完成，无需先 select_path\n"
            "  · confirmed 假设需更具体 → record_finding(statement_update=...) "
            "修正其表述（假设为扁平结构，无层级深化）\n"
            "  · 本层全部 refuted 且有搁置假设 → backtrack 回溯恢复\n"
            + _retry_option
            + _append_option +
            "- 多根因场景：证据支持的假设即使非主根因也应标记为 confirmed（次要根因/加剧因素），"
            "refuted 仅用于证据明确证伪的假设。多个假设可同为 confirmed。\n"
            "- ⚠ 独立故障源识别：当证据显示多个独立故障源同时存在"
            "（如多个进程/配置异常各自被观测到），即使某故障源非主症状的"
            "直接因果原因，也应作为独立根因 confirmed 或在报告中明确列为"
            "次要根因——不得仅因『非主症状直接原因』而 refuted 一个客观"
            "存在的独立故障（refuted 仅用于因果链被证据明确证伪）。\n"
            "- ⚠ 概率判断原则：若核心因果关系已由证据确认"
            "（如「异常进程占满资源→服务响应超时」），"
            "仅次要细节未知（如触发者、启动源），"
            "应保持高概率(≥80%)。"
            "不要因为次要信息缺失而过度降低根因概率。\n"
            f"- 推进：{exit_txt}"
            + pending_hint
            + dead_hint
            # Parallel-delegation suggestion (design document
            # parallel-delegation §4.2 T8): the complementary-expert
            # delegations empirically happen in EVALUATE (auto-heal
            # path), so the decision point needs the same guidance —
            # same renderer as the VERIFY directive (SSOT).
            + parallel_hint
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
                    "⛔ 报告正文一律经 write_file 写入文件。\n"
                    "- 若确认简版已足够，可跳过 write_file\n"
                    f"- 最后{summary_req}\n"
                    "- 总结面向运维工程师——禁止提及台账、报告文件路径等内部实现细节"
                )
            return (
                "你当前处于 REPORT 阶段（诊断报告已写入）。\n"
                "- 报告已保存，无需再调用 write_file / record_finding / propose_hypotheses\n"
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
            "⛔ 报告正文全部经 write_file 写入文件；写盘后再向用户输出总结。\n"
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
    delegated_for: str | None = None,
) -> None:
    """Record a tool call within the current diagnosis round.

    NOTE: current_round is synced from the middleware's _model_call_count
    (via before_model hook), NOT incremented here. This ensures round
    numbers reflect actual LLM invocations, not individual tool calls.

    *delegated_for* records WHICH hypothesis a task() delegation targeted
    (design document parallel-delegation §5 D2) — a structured fact that
    makes per-hypothesis delegated-expert sets, parallel-group keys, and
    audit trails derivable from the rounds history.
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
    if delegated_for:
        entry["delegated_for"] = delegated_for
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

    EIG = U(h) x D x F x B, D=1.0 (targeted), F = 0.5^(n_deleg + n_inc)
    (D6: inconclusive verdicts add freshness decay -- the same evidence
    re-examined rarely flips the outcome).  B = _EPISTEMIC_BONUS (2.0) for
    first-ever delegation to an unverified hypothesis (_delegate_count=0,
    pending/inconclusive): U(h) captures only aleatoric uncertainty, but an
    unverified prior also carries epistemic uncertainty that the first
    delegation resolves.  Cost = COST_DELEGATION.
    """
    n = node.get("_delegate_count", 0) + node.get("_inconclusive_count", 0)
    eig = _uncertainty(node) * (0.5 ** n)
    if node.get("_delegate_count", 0) == 0 and node.get("status") in ("pending", "inconclusive"):
        eig *= _EPISTEMIC_BONUS
    return eig / COST_DELEGATION


# ── View-aware delegation value (design document parallel-delegation §8) ──
# D6's freshness decay assumes "the same evidence re-examined rarely flips
# the outcome" — but a NEW observability view is not the same evidence.
# The 2026-08-08 scenario-35 replay proved multi-view cross-validation is
# sometimes the ONLY path to a verdict (host/SCI/KMC views each contributed
# new evidence), and with view-blind decay any hypothesis at
# _delegate_count=1 + _inconclusive_count=1 has value ≤ 0.125 < κ — the
# gate would kill legitimate multi-view verification.  F therefore decays
# on SAME-VIEW prior delegations only; n_inc stays global.

def _view_delegation_count(
    ledger: DiagnosisLedger, hid: str, expert: str,
) -> int:
    """Prior delegations for *hid* covering the SAME view as *expert*."""
    from diagnostics.agent.scene_profile import EXPERT_VIEW

    view = EXPERT_VIEW.get(expert, "")
    n = 0
    has_d2 = False
    for rd in ledger.get("rounds", []):
        if rd.get("delegated_for") == hid:
            has_d2 = True
            for e in rd.get("delegated_experts", []):
                if EXPERT_VIEW.get(e, "") == view:
                    n += 1
    if has_d2:
        return n
    # Legacy fallback (pre-D2 ledgers): derive from evidence sources.
    node = ledger.get("hypotheses", {}).get(hid) or {}
    for e in node.get("evidence", []):
        s = str(e.get("source", ""))
        if s.startswith("expert:"):
            if EXPERT_VIEW.get(s.split("expert:", 1)[-1], "") == view:
                n += 1
    return n


def _value_with_view(node: HypothesisNode, same_view_deleg: int) -> float:
    """delegation_value with F decayed by same-view delegations only."""
    n = same_view_deleg + node.get("_inconclusive_count", 0)
    eig = _uncertainty(node) * (0.5 ** n)
    if (node.get("_delegate_count", 0) == 0
            and node.get("status") in ("pending", "inconclusive")):
        eig *= _EPISTEMIC_BONUS
    return eig / COST_DELEGATION


def delegation_value_for(
    ledger: DiagnosisLedger, hid: str, expert: str | None = None,
) -> float:
    """View-aware delegation value for hypothesis *hid*.

    *expert* given → value of delegating THAT expert (G5 gate semantics,
    per-call).  *expert*=None → best case over the remaining assembled
    experts (exit-model / dead-detection semantics: "is ANY verification
    action still worth its cost").  Falls back to the legacy view-blind
    value when the ledger carries no scene_experts snapshot (D1).
    """
    node = ledger.get("hypotheses", {}).get(hid)
    if node is None:
        return 0.0
    if expert is not None:
        return _value_with_view(node, _view_delegation_count(ledger, hid, expert))
    scene_experts = ledger.get("scene_experts") or []
    if not scene_experts:
        return delegation_value(node)
    delegated = _delegated_experts_for(ledger, hid)
    remaining = [e for e in scene_experts if e not in delegated]
    # All experts already tried → best case is the least-degraded view.
    pool = remaining or list(scene_experts)
    return max(
        (_value_with_view(node, _view_delegation_count(ledger, hid, e))
         for e in pool),
        default=0.0,
    )


def _economically_dead(ledger: DiagnosisLedger, hid: str) -> bool:
    """软终态（v2.9）：未决但验证增益已低于成本（best-case delegation value < κ）。

    A hypothesis that is still pending/inconclusive but can no longer be
    delegated (G5 would hard-block it) must not block direction changes:
    it neither blocks the retry-batch gate (can_propose) nor keeps the
    layer out of T8.  Its STATUS stays pending/inconclusive — the
    record_finding honesty semantics are unchanged; only the direction
    logic treats it as dead.  deferred nodes are NOT dead: an explicit
    shelve is a revivable direction (G9 revival channel), matching the
    E2′ exclusion.

    View-aware (design document parallel-delegation §8): dead means NO
    remaining expert view offers gain ≥ cost — a hypothesis with an
    untried complementary view is still alive.
    """
    node = ledger.get("hypotheses", {}).get(hid)
    if node is None:
        return False
    if node.get("status") not in ("pending", "inconclusive"):
        return False
    if node.get("deferred"):
        return False
    # ── F1 conflict override (v3.12.0, design document §8 G17-E2/G20
    # context, 2026-08-12 scenario 37) ──
    # When the metric-layer evidence for *hid* is CONFLICTED (argus
    # cluster/node anomaly + target-entity normal), the E2/G20 guards
    # deterministically require a deep-expert verification BEFORE any
    # verdict.  That "necessary verification" must not be silently
    # killed by the soft economic-death model — cost optimisation cannot
    # override an evidence-standard requirement (AgentDoG §12: safety
    # dimension over cost dimension; OpenAI SDK §12: guardrails must
    # change state, not deadlock).  While the conflict is unresolved the
    # hypothesis stays alive for deep delegation; once a deep expert
    # returns, evidence is no longer all-argus, the conflict clears and
    # the economic model resumes normally (self-healing).
    if _argus_conflict_signal(ledger, hid) is not None:
        return False
    return delegation_value_for(ledger, hid) < KAPPA_STOP


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
        v = delegation_value_for(ledger, hid)
        if v > best_v:
            best_v = v
            best_d = (
                f"委派验证 {fmt_hid(hid)} (p={node.get('probability')}%, "
                f"已委派{node.get('_delegate_count', 0)}次/"
                f"inconclusive {node.get('_inconclusive_count', 0)}次)"
            )
    if (ledger.get("root_hypothesis_ids") and can_propose_root_hypotheses(ledger)
            and not has_confirmed):
        v = 1.0 / COST_BATCH_PROPOSE  # fresh batch: U=1, D=1, F=1
        if v > best_v:
            best_v = v
            best_d = "换批提出新假设"
    return best_v, best_d


# ── Exit condition checking ──

def check_exit_conditions(ledger: DiagnosisLedger) -> tuple[bool, str, str | None]:
    """Check if diagnosis should exit to REPORT.

    Returns (should_exit, reason, confirmed_hypothesis_id).

    v2.8 flat model (design document §6):
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
        if is_confirmed_root(node)
    ]

    # ── E2 exhaustion (fast lane): all roots refuted, budget spent ──
    if root_ids and not confirmed_ids:
        all_refuted = all(
            hypotheses.get(hid, {}).get("status") in _HARD_FAILED_STATUSES
            for hid in root_ids
        )
        if all_refuted and not can_propose_root_hypotheses(ledger):
            return True, (
                f"假设穷尽: 提出预算已用尽（{MAX_PROPOSE_CALLS} 次），"
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
        # The budget test uses the raw GLOBAL propose count, NOT the
        # can_propose proxy (an active pending node would poison that
        # proxy).
        if ledger.get("_propose_count", 0) >= MAX_PROPOSE_CALLS:
            survivors = [
                (hid, node) for hid in root_ids
                if (node := hypotheses.get(hid)) is not None
                and node.get("status") not in _HARD_FAILED_STATUSES
            ]
            if survivors and all(
                    node.get("status") in ("pending", "inconclusive")
                    and not node.get("deferred")
                    and delegation_value_for(ledger, hid) < KAPPA_STOP
                    for hid, node in survivors):
                return True, (
                    f"假设穷尽: {MAX_ROOT_PROPOSE_BATCHES} 批假设预算用尽，"
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


def _exit_blocker(ledger: DiagnosisLedger) -> dict | None:
    """Return the hypothesis blocking the exit (max delegation_value >= κ), or None.

    Exit condition S2 requires every pending/inconclusive hypothesis to be
    economically dead (delegation_value < κ).  A hypothesis above κ is a
    potential competing root cause that must be resolved before REPORT
    (multi-root protection).  This is the deterministic, code-maintained
    predicate behind the exit directive (design document §5/§6.3).
    """
    best: dict | None = None
    best_v = -1.0
    for h, node in ledger.get("hypotheses", {}).items():
        if (node.get("status") in ("pending", "inconclusive")
                and not node.get("deferred")):
            v = delegation_value_for(ledger, h)
            if v >= KAPPA_STOP and v > best_v:
                best_v = v
                best = {
                    "id": h,
                    "statement": node.get("statement", ""),
                    "probability": node.get("probability", 0),
                    "delegation_value": round(v, 3),
                    "delegated": node.get("_delegate_count", 0),
                    "inconclusive": node.get("_inconclusive_count", 0),
                }
    return best


def render_unverified_disclosure(ledger: DiagnosisLedger) -> str:
    """Render the system-injected disclosure section listing unverified
    competing hypotheses (design document §6.3 E2' release-with-disclosure,
    v3.6.0).

    Appended to the report content by the write_file gate (release) and
    by the report-capture path (rewrite backstop) whenever unresolved
    hypotheses remain — multi-root-cause protection via deterministic
    DISCLOSURE (code-injected, does not depend on LLM compliance)
    instead of blocking.  Lists ALL unresolved hypotheses regardless of
    deferred state (a shelved hypothesis is still an unverified fact the
    reader must see); Returns "" when no unresolved hypotheses remain.
    """
    lines = []
    for hid, n in ledger.get("hypotheses", {}).items():
        if n.get("status") in ("pending", "inconclusive"):
            if n.get("status") == "inconclusive":
                _st = "证据不足（inconclusive）"
            else:
                _st = "未验证"
            if n.get("deferred"):
                _st += "，已搁置"
            lines.append(
                f"- **{fmt_hid(hid)}**（概率 {n.get('probability')}%，{_st}）："
                f"{n.get('statement', '')}"
            )
    if not lines:
        return ""
    return (
        "\n\n---\n\n## 未验证竞争假设（系统披露）\n\n"
        "> 本报告在确认根因后生成；以下竞争假设尚未完成验证，系统已将其搁置。"
        "若故障症状持续或复发，建议优先针对以下方向继续排查：\n\n"
        + "\n".join(lines)
    )


def unrecorded_evidence_hypotheses(
        ledger: DiagnosisLedger) -> list[tuple[str, "HypothesisNode"]]:
    """Undecided hypotheses carrying expert evidence newer than the last
    recorded finding (design document §6.3 evidence closure, v3.11.0).

    Same predicate family as ``compute_next_action``'s "awaiting verdict"
    detection (expert evidence present ∧ ``_last_evidence_round`` >
    ``_last_finding_round``) — pure derivation from ledger fact fields,
    never persisted (P1).  Terminal nodes are excluded: their verdict
    already closed the evidence loop.
    """
    out: list[tuple[str, HypothesisNode]] = []
    for hid, node in ledger.get("hypotheses", {}).items():
        if node.get("status") not in ("pending", "inconclusive"):
            continue
        has_expert = any(
            str(e.get("source", "")).startswith("expert:")
            for e in node.get("evidence", [])
        )
        if (has_expert
                and node.get("_last_evidence_round", 0)
                > node.get("_last_finding_round", 0)):
            out.append((hid, node))
    return out


def render_unrecorded_evidence_appendix(ledger: DiagnosisLedger) -> str:
    """Render the system-injected appendix disclosing gathered-but-
    unrecorded expert evidence (design document §6.3, v3.11.0).

    Evidence closure at report time: expert conclusions that were
    collected but never formalized via record_finding must still reach
    the reader.  Deterministic DISCLOSURE (code-injected, does not depend
    on LLM compliance) — the same fail-open philosophy as the E2'
    unverified-hypothesis disclosure; blocking exit until ledgering would
    risk zero-output timeouts.  Literature: MAST (arXiv:2503.13657)
    FM-3.1 premature termination / FM-2.4 information withholding.
    Returns "" when no unrecorded expert evidence exists.
    """
    pairs = unrecorded_evidence_hypotheses(ledger)
    if not pairs:
        return ""
    lines = []
    for hid, node in pairs:
        expert_evs = [
            e for e in node.get("evidence", [])
            if str(e.get("source", "")).startswith("expert:")
        ]
        latest = expert_evs[-1] if expert_evs else {}
        src = (str(latest.get("source", "")).split("expert:", 1)[-1]
               or "expert")
        summary = _clean_evidence_text(str(latest.get("summary", "")), 600)
        lines.append(
            f"- **{fmt_hid(hid)}**（p={node.get('probability')}%，"
            f"{node.get('status')}）：{node.get('statement', '')}\n"
            f"  - {src} 结论（未落账）：{summary}"
        )
    return (
        "\n\n---\n\n## 已采集未落账证据（系统附录）\n\n"
        "> 以下假设的专家验证结论在诊断过程中已采集，但未形成正式判定"
        "（record_finding）即收口。系统如实披露原始专家证据，供后续排查参考：\n\n"
        + "\n".join(lines)
    )


def _evidence_layer_label(node: dict) -> str:
    """Deterministic evidence-layer label for a confirmed root cause
    (design document §9, v3.14.0): a pure function of the ledger's
    evidence sources — any deep-expert conclusion → 深度确证; argus-only
    (metric layer) → 指标直接观测.  Whether the STATEMENT overclaims
    beyond its layer stays a semantic obligation of the report contract
    (【推断】 marking); code only reports the facts.
    """
    sources = [
        str(e.get("source", ""))
        for e in node.get("evidence", [])
        if str(e.get("source", "")).startswith("expert:")
    ]
    if not sources:
        return "协调员记录"
    if all("-argus-expert" in s for s in sources):
        return "指标直接观测"
    return "深度确证"


def render_derived_report_appendix(ledger: DiagnosisLedger) -> str:
    """Render ledger-derived report sections deterministically (design
    document §9, v3.14.0): per-root-cause evidence-layer labels +
    confidence, Serverless topology mapping, delegated-expert list with
    key conclusions, refuted hypotheses with reasons.

    Deterministic logic must be code, not prompt obligations: every
    section here is a pure function of the ledger, and transcription of
    structured data through the generative path is a known
    fidelity-failure surface (hallucination literature, design document
    §12).  Injected on every gate-passing report write — the same
    fail-open channel as the v3.11.0 evidence-closure appendix.
    Returns "" when there is nothing to derive.
    """
    hypotheses = ledger.get("hypotheses", {})
    sections: list[str] = []

    # 1. Root-cause evidence layers + confidence.
    confirmed = [
        (hid, n) for hid, n in hypotheses.items()
        if n.get("status") == "confirmed"
    ]
    if confirmed:
        lines = []
        for hid, node in confirmed:
            lines.append(
                f"- **{fmt_hid(hid)}**（置信度 {node.get('probability')}%）"
                f"【{_evidence_layer_label(node)}】："
                f"{node.get('statement', '')}"
            )
        sections.append(
            "### 根因证据层级\n\n"
            "> 【深度确证】=领域专家经日志/事件/状态验证；"
            "【指标直接观测】=监控指标直接呈现故障事实"
            "（机制性解释未经深度验证，正文引用时视为推断）\n\n"
            + "\n".join(lines)
        )

    # 2. Serverless topology mapping (scene-conditional).
    topology = ledger.get("topology")
    if topology:
        from diagnostics.agent.topology_render import report_mapping_table
        table = report_mapping_table(topology)
        if table:
            sections.append("### 拓扑映射\n\n" + table)

    # 3. Delegated experts + latest key conclusion (from rounds[]).
    delegated: list[str] = []
    for r in ledger.get("rounds", []):
        for e in r.get("delegated_experts") or []:
            if e and e not in delegated:
                delegated.append(e)
    if delegated:
        lines = []
        for expert in delegated:
            latest: dict | None = None
            for node in hypotheses.values():
                for e in node.get("evidence", []):
                    if str(e.get("source", "")) == f"expert:{expert}":
                        latest = e
            finding = ""
            if latest:
                structured = latest.get("structured")
                if (isinstance(structured, dict)
                        and structured.get("preliminary_judgment")):
                    finding = _clean_evidence_text(
                        str(structured["preliminary_judgment"]), 200)
                else:
                    finding = _clean_evidence_text(
                        str(latest.get("summary", "")), 200)
            lines.append(f"- {expert}" + (f"：{finding}" if finding else ""))
        sections.append("### 委派专家与关键结论\n\n" + "\n".join(lines))

    # 4. Refuted hypotheses with reasons.
    refuted = [
        (hid, n) for hid, n in hypotheses.items()
        if n.get("status") == "refuted"
    ]
    if refuted:
        lines = []
        for hid, node in refuted:
            reason = _clean_evidence_text(
                str(node.get("verdict_reason") or ""), 200)
            lines.append(
                f"- **{fmt_hid(hid)}**：{node.get('statement', '')}"
                + (f"——{reason}" if reason else "")
            )
        sections.append("### 排除的假设\n\n" + "\n".join(lines))

    if not sections:
        return ""
    return (
        "\n\n---\n\n## 系统附录：台账派生数据（系统确定性生成）\n\n"
        "> 以下内容由系统从诊断台账确定性生成（非 LLM 转录），"
        "与正文具有同等效力：\n\n"
        + "\n\n".join(sections)
    )


def render_failure_digest(ledger: DiagnosisLedger) -> str:
    """Render a structured digest of the failed batch for retry-batch
    HYPOTHESIZE guidance (design document §9, v3.11.0).

    Prior-batch failures are the generation context for the new batch:
    HypoGeniC (arXiv:2404.04326) conditions hypothesis generation on
    falsified hypotheses WITH their outcomes; MAI-DxO (§12) explicitly
    maintains eliminated entries in the differential list.  The digest
    renders at the decision point (本步要求) rather than relying on the
    mid-context 已排除 section (Lost in the Middle, §12).  Covers
    prior-batch refuted nodes AND economically-dead survivors (soft
    terminals block the retry gate exactly like refuted ones, §10);
    deferred nodes stay excluded (shelved = revivable direction, not a
    failure).  Returns "" outside retry-batch context (first batch) or
    when nothing failed.
    """
    if ledger.get("_root_propose_count", 0) < 1:
        return ""
    # v3.11.1 fix: digest 渲染时机是换批提出之前——此时 root_hypothesis_ids
    # 仍指向前一批（新批尚未提出），故绝不能按 "不在当前批" 过滤（否则恰好
    # 排除全部失败节点，摘要恒为空）。正确语义：渲染所有 refuted / 经济死亡
    # 且非 deferred 的节点（换批时全部假设都属于失败批）。2026-08-11 三场景
    # 复跑实证（场景 18/2 换批摘要缺失）。
    lines: list[str] = []
    for hid, node in ledger.get("hypotheses", {}).items():
        status = node.get("status")
        if status == "refuted":
            tag = {
                "evidence_saturated": "证据饱和",
                "timeout_unreached": "未及验证(超时)",
                "auto_finalized": "系统关闭",
            }.get(node.get("terminal_reason") or "", "已证伪")
        elif (status in ("pending", "inconclusive")
                and not node.get("deferred")
                and _economically_dead(ledger, hid)):
            tag = "验证价值耗尽"
        else:
            continue
        reason = _clean_evidence_text(
            (node.get("verdict_reason") or node.get("rationale", "")
             or "(无记录)"), 200)
        counter = ""
        contra = [
            e for e in node.get("evidence", [])
            if e.get("supports") is False
        ]
        if contra:
            counter = ("；关键反证：" + _clean_evidence_text(
                str(contra[-1].get("summary", "")), 150))
        lines.append(
            f"- [{tag}] {fmt_hid(hid)} {node.get('statement', '')}："
            f"{reason}{counter}"
        )
    if not lines:
        return ""
    return (
        "\n- ⚠ 前批失败摘要（新批必须吸收这些排除证据，"
        "禁止重复或仅换措辞）：\n"
        + "\n".join("  " + line for line in lines)
    )


def render_exit_directive(ledger: DiagnosisLedger, phase: DiagnosisPhase) -> str:
    """Render an actionable, phase-aware exit-condition directive (SSOT).

    Consumed by the proactive exit_banner (in `_phase_guidance`, injected
    every EVALUATE round) and the VERIFY directive's exit-risk overlay.
    Names the blocking hypothesis and lists ONLY the actions available in
    the current phase.  Options are verify-first ordered and carry an
    anti-fabrication caveat to mitigate constraint-evasive fabrication
    (arXiv 2606.14831 — agents may fabricate a verdict to evade the block).
    Returns "" when there is no blocker (exit condition satisfied).

    v3.6.0 (release-with-disclosure): the directive no longer forbids
    write_file — verification stays the RECOMMENDED option, but closing
    out is legitimate: the write_file gate releases the report and
    deterministically injects the unverified-hypothesis disclosure
    (design document §6.3 — disclosure, not blocking, protects against
    dropped second root causes).

    v3.8.0 (S1-aware branch): WITHOUT a confirmed root cause the message
    must NOT claim "已确认根因" nor promise "write_file 不会拦截" — the
    generic gate DOES block when S1 fails, and a contradicting promise
    deterministically manufactures the retry loop (2026-08-08 scenario
    gpu_oom: 4 consecutive write_file blocks, rounds 22-25).  The no-
    confirmed branch names the residual hypothesis and lists the honest
    exits: verify (new view / argus re-check), refute, shelve — after
    which the gate releases an honest "root cause not confirmed" report
    with disclosure.
    """
    blocker = _exit_blocker(ledger)
    if blocker is None:
        return ""
    hid = fmt_hid(blocker["id"])
    stmt = blocker["statement"]
    p = blocker["probability"]
    dv = blocker["delegation_value"]
    _has_confirmed = any(
        n.get("status") == "confirmed"
        for n in ledger.get("hypotheses", {}).values()
    )
    if not _has_confirmed:
        # S1 not satisfied — honest-negative-exit guidance.
        header = (
            f"⚠ 尚无确认根因（无 p≥80 的 confirmed 假设）。"
            f"残余未决假设 {hid}（{stmt}，p={p}%，验证价值 {dv}≥κ={KAPPA_STOP}）仍可行动。"
        )
        if phase == "evaluate":
            actions = (
                "请选择（EVALUATE 可用动作）：\n"
                f"  ① select_path 切换到 {hid} → 委派新视角专家验证（推荐；监控时序定向复核可委派对应 argus 专家）\n"
                f"  ② 现有证据已明确排除 → record_finding 判 {hid} refuted\n"
                f"  ③ 反复委派仍无法定论 → select_path(deprioritized=True) 搁置 {hid}\n"
                "全部假设终态或搁置后，系统将放行「未确认根因」的诚实报告"
                "（披露已排除方向与未决方向）。\n"
                "⛔ 不要直接 write_file——无确认根因时会被拦截。"
            )
        elif phase == "verify":
            actions = (
                "请选择（VERIFY 可用动作）：\n"
                f"  ① task 委派新视角专家验证 {hid}（推荐）\n"
                f"  ② 现有证据已明确排除 → record_finding 判 {hid} refuted；无法定论判 inconclusive\n"
                "⛔ 不要直接 write_file——无确认根因时会被拦截。"
            )
        else:
            actions = f"请先验证、终结或搁置 {hid}，再考虑生成报告。"
        return header + "\n" + actions
    header = (
        f"⚠ 已确认根因，但竞争假设 {hid}"
        f"（{stmt}，p={p}%，验证价值 {dv}≥κ={KAPPA_STOP}）尚未验证。"
    )
    if phase == "evaluate":
        actions = (
            "请选择（EVALUATE 可用动作）：\n"
            f"  ① select_path 切换到 {hid} → 进入 VERIFY 委派专家验证（推荐：多视角证据更完备）\n"
            f"  ② select_path(deprioritized=True) 搁置 {hid}（若反复委派仍无法定论）\n"
            f"  ③ record_finding 终结 {hid}（仅当现有证据已明确判定；不得为满足退出条件而随意终结）\n"
            "  ④ 若评估后决定收口：直接 write_file 生成报告——系统将自动搁置未验证假设"
            "并在报告末尾披露（多根因保护），不会拦截。"
        )
    elif phase == "verify":
        actions = (
            "请选择（VERIFY 可用动作）：\n"
            f"  ① task 委派专家验证 {hid}（推荐：多视角证据更完备）\n"
            f"  ② record_finding 终结 {hid}（仅当现有证据已明确判定；不得为满足退出条件而随意终结）\n"
            "  ③ 若评估后决定收口：直接 write_file 生成报告——系统将自动搁置未验证假设"
            "并在报告末尾披露（多根因保护），不会拦截。"
        )
    else:
        actions = f"请先验证或搁置 {hid}，再考虑生成报告。"
    return header + "\n" + actions


def _delegated_experts_for(ledger: DiagnosisLedger, hid: str) -> set[str]:
    """Experts already delegated for hypothesis *hid*.

    Derived from the rounds history ``delegated_for`` fact (design
    document parallel-delegation §5 D2); unioned with legacy evidence
    source parsing so ledgers written before D2 still work (B9).
    """
    out: set[str] = set()
    for rd in ledger.get("rounds", []):
        if rd.get("delegated_for") == hid:
            out.update(rd.get("delegated_experts", []))
    node = ledger.get("hypotheses", {}).get(hid) or {}
    for e in node.get("evidence", []):
        s = str(e.get("source", ""))
        if s.startswith("expert:"):
            out.add(s.split("expert:", 1)[-1])
    return out


# ── Argus conflict-signal detection (design document §8 G13 context,
# 2026-08-11 scenario 38) ────────────────────────────────────────────
# K8s observability reality (Azure/Microsoft + ACK docs, 2026-08 search):
# a Pod's OOMKilled/CrashLoopBackOff is an *event-state* fact that does
# NOT surface in the metric time series (memory-limit pressure is
# invisible in metrics; kubectl describe pod's Last State + Events are
# authoritative).  Therefore an argus-class expert returning "target
# entity normal" while *also* reporting cluster-level anomalies (restart
# count up / OOM count > 0) leaves a *conflict*: the metric layer cannot
# refute an event-state hypothesis.  The verify directive must steer the
# coordinator to a deep expert (log/event inspection) instead of letting
# the single-dimension negative evidence silently kill the hypothesis
# (proactive guidance, design document proactive-guidance §5.3 — the
# directive leads, the record_finding gate (C2) only backstops).
# ── Line-level classification (avoids whole-text cross-matching: the
# string "重启计数 0" would otherwise hit BOTH the anomaly "重启" and the
# normal "重启.*0" regexes).  Anomaly lines are those starting with a
# severity marker (🔴/⚠) OR a mutation point with a trend (上升/突增/0→N);
# normal lines start with ✅ or are negative evidence about the target.
_ARGUS_ANOMALY_LINE = re.compile(
    r"^🔴|^⚠|OOM|oom|CrashLoopBackOff|驱逐|evict|"
    r"拒绝|reject|失败|超标|突增|上升|峰值|0 ?→ ?[1-9]|1 ?→ ?[2-9]",
    re.IGNORECASE,
)
_ARGUS_CLUSTER_SCOPE = re.compile(
    r"集群|cluster|节点|node|工作负载|deployment|namespace|命名空间|全局|整体",
    re.IGNORECASE,
)
_ARGUS_TARGET_NORMAL = re.compile(
    r"✅.*(正常|0)|目标.*(正常|0)|.*(重启计数|restart).*0|.*OOMKilled.*0|无 OOM|无OOM|未触发",
    re.IGNORECASE,
)


def _argus_conflict_signal(ledger: DiagnosisLedger, hid: str) -> str | None:
    """Detect a metric-layer conflict for hypothesis *hid*.

    A conflict exists when ALL expert evidence for *hid* comes from
    argus-class experts (``*-argus-expert``) AND an argus return reports
    an anomaly (🔴/⚠/OOM/restart-up, scoped to cluster/node/workload
    layer) while ALSO reporting the target entity as normal.  Metric-
    layer "target normal" cannot refute an event-state hypothesis (Pod
    OOMKilled is invisible in metrics; the authoritative sources are
    logs/events — Azure/ACK docs), so the coordinator must deep-dive with
    a deep expert instead of concluding (proactive guidance §5.3: the
    directive leads; the record_finding gate only backstops).

    Returns a short rationale string when a conflict is detected, else
    None.  Structured returns (``structured`` field) are checked first;
    the formatted summary text is the degraded path.

    Line-level classification: an anomaly line must start with a
    severity marker or a mutation point (0→N trend); a "✅ … 0" or a
    target-restart-0 negative line is normal, never an anomaly — so a
    lone "重启计数 0" row cannot self-conflict.
    """
    node = ledger.get("hypotheses", {}).get(hid) or {}
    expert_sources = [
        str(e.get("source", ""))
        for e in node.get("evidence", [])
        if str(e.get("source", "")).startswith("expert:")
    ]
    if not expert_sources:
        return None
    # Conflict only when every expert view so far is metric-layer.
    if not all("-argus-expert" in s for s in expert_sources):
        return None
    for e in node.get("evidence", []):
        if not str(e.get("source", "")).startswith("expert:"):
            continue
        structured = e.get("structured")
        lines: list[str] = []
        if isinstance(structured, dict):
            for key in ("anomaly_ranking", "mutation_points",
                        "negative_evidence"):
                lines.extend(str(i) for i in (structured.get(key) or []))
            # preliminary_judgment often carries the cross-layer reading.
            _pj = structured.get("preliminary_judgment")
            if _pj:
                lines.append(str(_pj))
        else:
            # Text fallback: split on newlines, keep informative lines.
            for ln in str(e.get("summary") or "").splitlines():
                s = ln.strip()
                if s:
                    lines.append(s)
        if not lines:
            continue
        has_anomaly = False
        has_target_normal = False
        for ln in lines:
            if _ARGUS_ANOMALY_LINE.search(ln):
                # Severity-marker anomalies count only when scoped to a
                # non-target layer (cluster/node/workload/global) — a
                # 🔴 about the target itself is not a conflict.
                if _ARGUS_CLUSTER_SCOPE.search(ln):
                    has_anomaly = True
            if _ARGUS_TARGET_NORMAL.search(ln):
                has_target_normal = True
        if has_anomaly and has_target_normal:
            return (
                "argus 指标层同时报告（集群/节点/工作负载级）异常与目标实体"
                "正常——指标层无法覆盖 Pod 事件状态（OOMKilled 在指标中不可见），"
                "需委派深度专家查日志/事件确认目标实体。"
            )
    return None


def expert_verdict_conflict(ledger: DiagnosisLedger, hid: str,
                            new_verdict: str) -> dict | None:
    """Detect a terminal expert-verdict conflict on hypothesis *hid*
    (design document §8 G21, v3.16.0).

    Fires when the hypothesis already holds a structured deep-expert
    return whose verdict is a TERMINAL value (confirmed / refuted) that
    OPPOSES *new_verdict* — two deep experts measured the same claim in
    opposite directions.  Letting the later verdict silently overwrite
    the earlier is a recency bias; belief revision requires an explicit
    retraction reason (AGM).  Non-terminal returns (inconclusive) never
    fire; argus-layer internal conflicts are already covered by
    G17-E2/G20 and stay out of scope here.

    Returns a dict describing the opposing evidence (expert name /
    verdict / summary excerpt) for the arbitration message, or None.
    """
    if new_verdict not in ("confirmed", "refuted"):
        return None
    node = ledger.get("hypotheses", {}).get(hid) or {}
    for ev in reversed(node.get("evidence", [])):
        source = str(ev.get("source", ""))
        if not source.startswith("expert:"):
            continue
        structured = ev.get("structured") or {}
        prev = structured.get("verdict")
        if prev in ("confirmed", "refuted") and prev != new_verdict:
            return {
                "expert": source[len("expert:"):],
                "prev_verdict": prev,
                "summary": (ev.get("summary") or "")[:200],
            }
    return None


def single_channel_refute_signal(ledger: DiagnosisLedger, hid: str) -> dict | None:
    """Detect a SINGLE-CHANNEL refutation on hypothesis *hid*
    (design document §8 G22, v3.20.0, judgement narrowed v3.20.1).

    A refutation is single-channel when the hypothesis' expert-layer
    evidence comes from EXACTLY ONE distinct deep expert, in a scene that
    actually assembled two or more deep experts.  That is the structural
    precondition of *argument from absence* in its least detectable form:
    an expert that CANNOT observe a given evidence channel tends to
    phrase its blindness as a positive negative finding ("no spike
    observed", "logs all normal") — and a topologically-correct routing
    can send the hypothesis to exactly that expert.

    Empirical driver (2026-08-28 scenario 39, session a8fa9526): H1
    (VK sync-link anomaly) was routed to kmc-expert by the topology-
    derived map (virtualNode IS a KMC control-plane component), but the
    decisive evidence — SyncFailed events and vk_sync_failure_count
    1→6 in 15:00~15:05 — lives in get_serverless_events, i.e.
    serverless-expert only.  kmc-expert reported "no vk_sync_failure_count
    spike observed" and refuted a CORRECTLY directed hypothesis,
    cascading into five refutations and a 3600s timeout without
    convergence.  The same session's morning run (directed at
    serverless-expert) confirmed H1 in 8 rounds.

    Structural ownership ≠ evidence-channel ownership; this signal does
    not attempt to judge which channel is correct (that needs semantics),
    it only flags the precondition and hands the LLM the ledger's fault
    window so it can self-check coverage.  Multi-deep-expert scenes only
    (a scene with a single deep expert has no alternative channel to
    route to — blocking there would be pure ceremony).

    WHY EXACTLY ONE, NOT "AT MOST ONE" (v3.20.1, 2026-08-28 scenario 39
    session 7ce7bb66 post-mortem): the ZERO-deep-expert case is a path
    the PROACTIVE layer explicitly authorises — ledger.py §9 verify 档1
    grants "若现有监控证据已明确证伪…可直接 record_finding 判 refuted"
    and 档2/3 actively ENCOURAGES reusing evidence gathered while
    verifying OTHER hypotheses.  Blocking it pitted the gate against the
    guidance (the same layer-coherence anti-pattern recorded in §11
    v3.8.0, which previously produced a four-block loop).  Measured
    cost in that session: both G22 triggers fired on hypotheses holding
    ZERO expert evidence — H2 (serverless-expert had already shown the
    sync failure was an API-Server watch timeout, not a tunnel fault)
    and H3 (four argus returns showed etcd fully healthy — argus IS the
    authoritative channel for an infrastructure-health hypothesis) — i.e.
    a 100% false-positive rate against the <2% target that production
    guardrail practice sets, and two legitimate refutations were
    demoted to inconclusive (over-abstention).  The original defect's
    H1 carried exactly ONE deep channel (expert:kmc-expert + a
    coordinator entry), so this narrowing keeps the defect covered.

    Returns a dict (expert name / deep-expert candidates) or None.
    """
    nodes = ledger.get("hypotheses", {})
    if hid not in nodes:
        # Unknown hypothesis: nothing to judge.  The gate caller already
        # guards on membership; keeping the pure function self-explanatory
        # avoids a silent "fires" reading for a non-existent node.
        return None
    node = nodes[hid] or {}
    experts = ledger.get("scene_experts") or []
    deep = [e for e in experts if not str(e).endswith("-argus-expert")]
    if len(deep) < 2:
        return None
    seen: list[str] = []
    for ev in node.get("evidence", []):
        source = str(ev.get("source", ""))
        if not source.startswith("expert:"):
            continue
        name = source[len("expert:"):]
        # an argus expert is a monitoring channel, not a deep-evidence
        # channel: it cannot observe event/log state either.
        if name.endswith("-argus-expert"):
            continue
        if name not in seen:
            seen.append(name)
    # EXACTLY one deep channel (v3.20.1): zero deep channels is the
    # proactive layer's own authorised path (monitoring-evidence refute +
    # cross-hypothesis evidence reuse) — never block it.  Two or more
    # means coverage exists.
    if len(seen) != 1:
        return None
    return {
        "expert": seen[0] if seen else None,
        "deep_experts": deep,
        "fault_window": (
            f"{ledger.get('start_time', '?')} ~ {ledger.get('end_time', '?')}"
        ),
    }


def _parallel_candidates(
    ledger: DiagnosisLedger, hid: str, node: HypothesisNode,
) -> list[str]:
    """Complementary-expert candidates for a parallel delegation group.

    candidates = scene_experts (assembled directory snapshot, D1)
                 − experts already delegated for *hid* (D2)
    ordered by: uncovered views first (EXPERT_VIEW, D3), deep before
    argus within a view (inspection class is an ordering prior, never
    an exclusion — the decisive evidence in the 2026-08-08 scenario-35
    replay came from an argus-class expert).
    """
    from diagnostics.agent.scene_profile import EXPERT_VIEW

    scene_experts = ledger.get("scene_experts") or []
    if not scene_experts:
        return []
    delegated = _delegated_experts_for(ledger, hid)
    candidates = [e for e in scene_experts if e not in delegated]
    covered_views = {EXPERT_VIEW.get(e, "") for e in delegated}
    candidates.sort(
        key=lambda e: (EXPERT_VIEW.get(e, "") in covered_views,
                       "-argus" in e))
    return candidates


def _format_expert_names(candidates: list[str]) -> str:
    from diagnostics.agent.scene_profile import EXPERT_VIEW

    return "、".join(
        f"{e}（{EXPERT_VIEW.get(e, '互补视角')}）" for e in candidates)


def render_parallel_suggestion(ledger: DiagnosisLedger) -> str:
    """Formal parallel-delegation suggestion (design document
    parallel-delegation §4).

    Fires ONLY when the focus hypothesis is already judged inconclusive
    (the deterministic "single view insufficient" signal) — the pending
    state gets the conditional seeding via render_verify_directive
    instead, so the two never fire together (mutually exclusive
    predicates, WIRE).  Single renderer consumed by BOTH the VERIFY
    directive and the EVALUATE direction menu (SSOT, T8).
    """
    hypotheses = ledger.get("hypotheses", {})
    active_path = ledger.get("active_path") or []
    active_id = active_path[-1] if active_path else None
    node = hypotheses.get(active_id) if active_id is not None else None
    if node is None or node.get("status") != "inconclusive":
        return ""
    if not _delegated_experts_for(ledger, active_id):
        return ""
    # §4.1 cond 3 (view-aware): at least one candidate must clear the
    # G5 gate — the guidance never suggests what the gate would block.
    candidates = [
        e for e in _parallel_candidates(ledger, active_id, node)
        if delegation_value_for(ledger, active_id, e) >= KAPPA_STOP
    ]
    if not candidates:
        return ""
    hid = fmt_hid(active_id)
    if len(candidates) >= 2:
        names = _format_expert_names(candidates[:3])
        return (
            f"⚠ {hid} 单视角证据不足（inconclusive）。建议同轮并行委派互补视角专家："
            f"{names}——多视角交叉一次性取证。约束：并行委派须全部指向同一假设 "
            f"{hid}（单焦点），description 须含「验证假设 {hid}」字样。"
            "也可基于已有证据 record_finding 判定。"
        )
    if len(candidates) == 1:
        names = _format_expert_names(candidates)
        return (
            f"⚠ {hid} 单视角证据不足（inconclusive）。建议委派互补视角专家 "
            f"{names} 继续验证（description 须含「验证假设 {hid}」字样）；"
            "也可基于已有证据 record_finding 判定。"
        )
    return ""


# ── State-driven next-action computation (v3.10.1) ──
# Pure function (P1): derives the single most valuable next action for a
# hypothesis from ledger state.  Consumed by the proactive guidance layer
# (render_verify_directive, P7) BEFORE the LLM decides, and by the reactive
# backup layer (G17 receipt / write_file gate / deterministic exit) as the
#依从率 residual safety net.  Literature: StateAct (ACL REALM 2025) — state
# tracking enhances agent decisions 10-30%; GDE (de Kleer 1987) — next
# measurement computed from state; Harm Recovery (arXiv:2604.18847) —
# targeted recovery > comprehensive; EDF (arXiv:2602.01415) — adaptive
# scaffolding with escalation; Belief-R (arXiv:2406.19764) — LLMs need
# external scaffolding for belief revision.


class ActionHint(NamedTuple):
    """Computed next action for a hypothesis (pure derivation, not persisted)."""
    action: str          # "record_finding" | "task" | None
    urgency: str         # "critical" | "high" | "normal"
    reason: str          # human-readable justification (Chinese)


def compute_next_action(ledger: DiagnosisLedger, hid: str) -> ActionHint | None:
    """Derive the single most valuable next action for hypothesis ``hid``
    from ledger state (pure function, design document §6/P1, v3.10.1).

    Three fact fields (set by the middleware, §10) feed the computation:
    ``_g17_blocked_round``, ``_last_evidence_round``, ``_last_finding_round``.
    The function does NOT persist anything — it is the same family as
    ``derive_phase`` / ``_economically_dead`` (派生态归一化, §11).

    Returns ``None`` when no action is owed (terminal / economically dead).
    """
    node = ledger.get("hypotheses", {}).get(hid)
    if not node or node.get("status") in ("confirmed", "refuted"):
        return None

    has_expert = any(
        str(e.get("source", "")).startswith("expert:")
        for e in node.get("evidence", [])
    )
    ev_round = node.get("_last_evidence_round", 0)
    find_round = node.get("_last_finding_round", 0)
    g17_round = node.get("_g17_blocked_round")
    cur_round = ledger.get("current_round", 0)

    # Priority 1: expert evidence exists but hasn't been formalized via
    # record_finding (ev_round > find_round).  This is the "awaiting
    # verdict" state — the LLM owes a record_finding call.
    if has_expert and ev_round > find_round:
        stall = cur_round - ev_round
        if g17_round and g17_round < ev_round:
            # G17 previously blocked confirmed, then the expert was
            # delegated and returned (ev_round > g17_round).  The
            # confirmed channel is now OPEN — proactively reassure to
            # counteract Belief-R overcorrection (LLMs lose confidence
            # after rejection and avoid the rejected action).
            return ActionHint(
                "record_finding", "critical",
                f"G17 此前拦截了 {fmt_hid(hid)} 的 confirmed 判定，"
                f"专家证据现已到位——confirmed 通道已开放，不会再被拦截",
            )
        if stall >= 2:
            return ActionHint(
                "record_finding", "high",
                f"专家证据已等待 {stall} 轮未落账",
            )
        return ActionHint(
            "record_finding", "normal",
            f"专家已验证 {fmt_hid(hid)}，请落账判定",
        )

    # Priority 2: no expert evidence, delegation may be valuable.
    if not has_expert and not _economically_dead(ledger, hid):
        if node.get("_delegate_count", 0) == 0:
            return ActionHint(
                "task", "normal",
                f"尚未委派专家验证 {fmt_hid(hid)}",
            )
        return ActionHint(
            "record_finding", "normal",
            f"已有 coordinator 证据，可落账判定 {fmt_hid(hid)}",
        )

    return None


def _action_prefix(hid: str, ledger: DiagnosisLedger) -> str:
    """Convert compute_next_action output into the VERIFY directive prefix
    (v3.10.1).  Falls back to the original neutral wording when the
    function returns None (e.g. no fact fields populated yet)."""
    hint = compute_next_action(ledger, hid)
    if not hint or hint.action != "record_finding":
        return "可判定时直接 record_finding 落账"
    if hint.urgency == "critical":
        return f"{hint.reason}，请立即 record_finding 落账"
    if hint.urgency == "high":
        return f"{hint.reason}，请 record_finding 落账（否则系统将判定无进一步验证价值）"
    return "请 record_finding 落账判定"


def compute_evaluate_action(ledger: DiagnosisLedger) -> ActionHint | None:
    """Derive the single most valuable next action in EVALUATE phase
    (pure function, design document §9/P7, v3.10.2).

    Literature: Anthropic Context Engineering (2025) — "smallest set of
    high-signal tokens" at each decision point; GDE (de Kleer 1987) —
    next measurement by expected information gain; StateAct (ACL REALM
    2025) — state tracking enhances decisions.

    Priority order:
    1. Any pending hypothesis with unrecorded expert evidence →
       record_finding (reuse compute_next_action, cross-phase).
    2. Pending hypotheses ranked by delegation_value → recommend
       highest for select_path.
    3. Deferred hypotheses with revived value → backtrack.
    4. can_append_hypothesis → propose_hypotheses (append).

    Returns None when no action is owed (exit conditions should handle
    the rest via render_exit_directive).
    """
    # Priority 1: unrecorded expert evidence on any pending hypothesis
    for hid, node in ledger.get("hypotheses", {}).items():
        if node.get("status") in ("pending", "inconclusive"):
            hint = compute_next_action(ledger, hid)
            if hint and hint.action == "record_finding":
                return hint  # "先落账已有专家证据的假设"

    # Priority 2: rank pending (non-deferred, non-dead) by value
    _pending = []
    for hid, node in ledger.get("hypotheses", {}).items():
        if (node.get("status") in ("pending", "inconclusive")
                and not node.get("deferred")
                and not _economically_dead(ledger, hid)):
            val = delegation_value_for(ledger, hid)
            _pending.append((hid, val))
    if _pending:
        _pending.sort(key=lambda x: x[1], reverse=True)
        best_hid, best_val = _pending[0]
        return ActionHint(
            "select_path", "normal",
            f"{fmt_hid(best_hid)} 委派价值最高（{best_val:.2f}），推荐优先验证",
        )

    # Priority 3: deferred hypotheses worth reviving
    _deferred = []
    for hid, node in ledger.get("hypotheses", {}).items():
        if node.get("deferred") and not _economically_dead(ledger, hid):
            val = delegation_value_for(ledger, hid)
            _deferred.append((hid, val))
    if _deferred:
        _deferred.sort(key=lambda x: x[1], reverse=True)
        best_hid, best_val = _deferred[0]
        return ActionHint(
            "backtrack", "normal",
            f"搁置假设 {fmt_hid(best_hid)} 价值已恢复（{best_val:.2f}），可 backtrack 恢复",
        )

    # Priority 4: retry batch (all roots refuted/dead + budget open)
    if can_propose_root_hypotheses(ledger):
        return ActionHint(
            "propose_hypotheses", "normal",
            "当前批已全部终结，请换批提出新一批假设",
        )

    # Priority 5: append channel (batch path closed, append open)
    if can_append_hypothesis(ledger) and not can_propose_root_hypotheses(ledger):
        return ActionHint(
            "propose_hypotheses", "normal",
            "新证据指向全新方向 → 可追加 1 个新假设",
        )

    return None


def compute_report_guidance(ledger: DiagnosisLedger) -> str | None:
    """Pre-write guidance based on exit path (pure function, design
    document §9/P7, v3.10.2).

    Literature: Anthropic Context Engineering (2025) — "almost entirely
    focused on pre-generation constraint injection, not post-generation
    validation."  Injects a consistency constraint BEFORE the LLM writes
    the report so the report text does not contradict the ledger state.

    Returns None when no constraint is needed (confirmed root cause exit
    with no unrecorded evidence).

    v3.11.0: also flags gathered-but-unrecorded expert evidence
    (evidence closure, §6.3) — the LLM-authored report body must cite
    it first-hand; the system additionally injects the appendix
    deterministically at write time.
    """
    parts: list[str] = []
    unrecorded = unrecorded_evidence_hypotheses(ledger)
    if unrecorded:
        names = "、".join(fmt_hid(h) for h, _ in unrecorded)
        parts.append(
            f"⚠ {names} 已有专家验证证据但未 record_finding 落账——"
            "报告证据链须如实引用这些证据（不得遗漏）；"
            "写盘时系统将自动注入「已采集未落账证据」附录。"
        )
    has_confirmed = bool(ledger.get("root_cause_hypothesis_ids"))
    if not has_confirmed:
        parts.append(
            "⚠ 本次退出为无确认根因——报告正文不得声称"
            "'根因已确认'，应表述为"
            "'最可能根因（未完成验证）'并披露未验证假设及其概率。"
        )
    return "\n".join(parts) if parts else None


def render_verify_directive(ledger: DiagnosisLedger) -> str:
    """Render a phase-aware VERIFY action directive (proactive guidance).

    State -> instruction decision tree (design document
    proactive-guidance §5.3): mirrors the reactive gates' predicates
    forward so the LLM is told the right next action BEFORE it acts,
    instead of being corrected after a wasted round.  The three branch
    predicates are mutually exclusive by focus-hypothesis state; the
    exit-risk clause (priority 4) is ADDITIVE — merged into the same
    sentence as priority 1/2 (never a separate block, avoids the
    instruction-collision that sinks compliance, WIRE arXiv:2605.27784).

    Branch predicates (ledger-derived, deterministic):
      1 value-floor   delegation_value < κ  (delegation would be G5-blocked)
      2 harvest-nudge already delegated + expert evidence + still pending
                      (the G4 predicate brought forward from 3 rounds to 0)
      3 no-evidence   _delegate_count == 0  -> silent (static duty covers it)
      4 exit-risk     a confirmed root exists AND a competing hypothesis is
                      still actionable (max value >= κ) — the E2' predicate
                      moved forward from the write_file gate.

    Granularity for priority 1 is "name + numbers + one-line rationale"
    (PG4: the full EIG breakdown would bury the one action that matters,
    Lost in the Middle; one line of rationale closes the CEF evasion
    path "the block is incidental, let me rephrase and re-delegate").
    """
    hypotheses = ledger.get("hypotheses", {})
    active_path = ledger.get("active_path") or []
    active_id = active_path[-1] if active_path else None
    node = hypotheses.get(active_id) if active_id is not None else None

    has_confirmed = any(
        n.get("status") == "confirmed" for n in hypotheses.values())
    blocker = _exit_blocker(ledger)
    exit_risk = has_confirmed and blocker is not None

    anti_fab = "（不得为满足退出条件而随意终结）"
    directive = ""
    if node is not None and node.get("status") == "pending":
        # Best-case view-aware value: the floor message is accurate only
        # when NO remaining expert view could clear the G5 gate.
        dv = delegation_value_for(ledger, active_id)
        if dv < KAPPA_STOP:
            # Priority 1 — delegation on the focus hypothesis is about to
            # be hard-blocked by G5; steer to a record_finding verdict.
            directive = (
                f"⚠ {fmt_hid(active_id)} 委派价值 {dv:.3f}<κ={KAPPA_STOP}"
                f"（价值随委派次数指数衰减，再委派无新信息增益）——"
                f"再委派将被门控拦截，请 record_finding 依据已有证据终结 "
                f"{fmt_hid(active_id)}{anti_fab}。"
            )
        elif node.get("_delegate_count", 0) >= 1:
            expert_sources = [
                str(e.get("source", "")).split("expert:", 1)[-1]
                for e in node.get("evidence", [])
                if str(e.get("source", "")).startswith("expert:")
            ]
            if expert_sources:
                # Priority 2 — expert conclusion is back but not yet
                # recorded; nudge record_finding instead of drifting into
                # write_file (the 2026-08-07 scenario-32 soft loop).
                # T9 pre-verdict seeding (design document
                # parallel-delegation §4.2): when >=2 complementary
                # experts remain, plant the parallel-group option BEFORE
                # the verdict so an inconclusive judgement already knows
                # its next action — one round earlier than the formal
                # suggestion (predicates mutually exclusive: pending
                # seeds, inconclusive suggests).
                seeding = ""
                candidates = [
                    e for e in _parallel_candidates(ledger, active_id, node)
                    if delegation_value_for(ledger, active_id, e) >= KAPPA_STOP
                ]
                if len(candidates) >= 2:
                    seeding = (
                        "若判定证据不足（inconclusive），建议下一步同轮并行"
                        "委派互补专家："
                        f"{_format_expert_names(candidates[:3])}；"
                    )
                if seeding:
                    directive = (
                        f"⚠ {fmt_hid(active_id)} 已由 {expert_sources[-1]} 验证"
                        f"（结论见上方证据），{_action_prefix(active_id, ledger)}；"
                        f"{seeding}{anti_fab}。"
                    )
                else:
                    directive = (
                        f"⚠ {fmt_hid(active_id)} 已由 {expert_sources[-1]} 验证"
                        f"（结论见上方证据），{_action_prefix(active_id, ledger)}；"
                        f"证据不足可再委派（价值仍≥κ）{anti_fab}。"
                    )
                # ── G21 proactive override (design document §8/§9,
                # v3.16.0; 2026-08-21 scenario 40 follow-up) ── deep
                # experts returned OPPOSING terminal verdicts on the
                # SAME hypothesis: the harvest-nudge above would drive a
                # verdict that silently picks one side.  Override with
                # the single arbitration action (WIRE: exactly one
                # recommended action; AGM: retracting an entrenched
                # belief requires an explicit reason).  Mutually
                # exclusive with the C1 argus-conflict override below
                # (C1 requires ALL expert evidence argus-class; this one
                # requires >=2 deep terminal returns).
                _terminal_vs = {
                    (e.get("structured") or {}).get("verdict")
                    for e in node.get("evidence", [])
                    if str(e.get("source", "")).startswith("expert:")
                }
                if {"confirmed", "refuted"} <= _terminal_vs:
                    directive = (
                        f"⚠ {fmt_hid(active_id)} 的专家结论方向冲突：多个深度"
                        f"专家对同一假设给出相反终局结论（confirmed 与 "
                        f"refuted 并存，证据见上方）——落账任一终局判定等于"
                        f"单方面采信一侧。当前唯一推荐动作：先仲裁——复检矛盾"
                        f"数据源（委派第三方视角专家复核两侧对同一物理量的"
                        f"观测差异），或改判 inconclusive 并在 new_insights "
                        f"披露两侧矛盾；确有把握推翻某侧结论时，携带 "
                        f"statement_update（推翻理由）落账，通道立即开放、"
                        f"不会被拦截。"
                    )
                # Evidence-scope calibration (design document §9,
                # v3.13.0): when every expert conclusion so far is
                # metric-layer (argus-class) and no conflict fired, the
                # verdict channel stays open — but the recorded
                # conclusion must stay within what metrics directly
                # observe ("what happened").  A mechanism claim ("why")
                # is either deep-verified or explicitly labeled an
                # inference in the report (claim-grounding literature:
                # Nature 2025 s41467-025-58551-6; MiniCheck
                # arXiv:2404.10774).  Fail-open disclosure, same
                # philosophy as E2' — the conflict override below
                # replaces this whole directive when it fires.
                if all("-argus-expert" in s for s in expert_sources):
                    directive += (
                        " ⚠ 证据均为监控指标层：落账结论须限定于指标"
                        "直接可观测的范围（发生了什么）；机制性断言"
                        "（为何发生）请委派深度专家确证，或在报告中"
                        "明确标注为推断。"
                    )
        # C1 conflict override (proactive guidance §5.3, 2026-08-11
        # scenario 38): when the metric-layer evidence is CONFLICTED
        # (cluster/node anomaly + target-entity normal), the directive
        # steers to a deep-dive BEFORE any record_finding verdict — the
        # metric layer cannot refute an event-state hypothesis.  This
        # OVERRIDES the harvest-nudge (priority 2) instead of appending
        # to it: two competing action instructions in one directive
        # collapse joint compliance to ~35% (WIRE arXiv:2605.27784,
        # design document §12/§11) — the coordinator must get exactly ONE
        # recommended action.  The conflict clears automatically once a
        # deep expert returns (evidence is no longer all-argus), so the
        # record_finding channel reopens next round — stated explicitly
        # in the directive to counter Belief-R overcorrection (design
        # document §11 v3.10.1: post-gate fear made the LLM skip the
        # verdict entirely).
        conflict_hint = _argus_conflict_signal(ledger, active_id) if node is not None else None
        if conflict_hint:
            _deep_candidates = [
                e for e in _parallel_candidates(ledger, active_id, node)
                if not e.endswith("-argus-expert")
                and delegation_value_for(ledger, active_id, e) >= KAPPA_STOP
            ]
            _deep_names = _format_expert_names(_deep_candidates[:3])
            if _deep_names:
                # E3: machine-readable single-action statement — the
                # recommended action is a task delegation with an
                # explicit description contract (WASP 2025 structured
                # output: explicit single instruction beats open prose).
                _conflict_txt = (
                    f"{conflict_hint}"
                    f"当前唯一推荐动作：委派深度专家（{_deep_names}）"
                    f"查目标实体日志/事件确认后再判定——description 须含"
                    f"「验证假设 {fmt_hid(active_id)}」；"
                    f"深度专家返回后若已确认，record_finding 通道自动开放，"
                    f"不会被拦截。"
                )
            else:
                # No deep candidates (all-metric scene) — fall back to
                # the unblocked verdict so the coordinator is not left
                # with an impossible instruction (deadlock prevention).
                _conflict_txt = (
                    f"{conflict_hint}"
                    f"当前场景无可用深度专家；可基于现有证据 record_finding 判定，"
                    f"或委派任一未验证视角专家补证。"
                )
            directive = _conflict_txt
        # Priority 3 (no delegation yet) stays silent — the static duty
        # already directs task delegation; injecting here would be noise
        # (selective injection, PG3).
    elif node is not None and node.get("status") == "inconclusive":
        # Parallel-delegation branch (design document parallel-delegation
        # §4): single view proved insufficient — offer the complementary
        # group up front instead of letting the LLM drift into serial
        # re-querying (the 2026-08-08 scenario-35 six-round drain).
        directive = render_parallel_suggestion(ledger)

    if exit_risk:
        # Priority 4 — the E2' competing-hypothesis predicate, moved
        # forward.  v3.6.0: no longer "forbid write_file" — the gate
        # releases with disclosure; the proactive hint keeps verification
        # the recommended option.
        if directive:
            directive += " ⚠ 竞争假设尚未验证：建议先验证；若收口，系统将自动披露未验证假设。"
        else:
            # SSOT: same renderer the EVALUATE exit_banner consumes, so
            # the proactive hints never diverge.
            directive = render_exit_directive(ledger, "verify")
    return directive
