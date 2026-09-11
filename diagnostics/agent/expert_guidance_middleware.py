"""Expert proactive guidance middleware (design document §9, v3.24.0).

The "consumer A" of the expert session ledger: before every model call
inside a delegation, appends a compact deterministic progress block
(已覆盖维度 / 连续无数据方向 / 去重统计 / 下一动作指针) to the system
message — the expert-scale counterpart of the Coordinator's pre-phase
guidance injection.

Why proactive beats reactive-only (design document §11 v3.24.0):
reactive guards (dedup/G19/G26) tell the expert it erred AFTER the
fact, at the cost of a full LLM turn per intervention; the injected
progress state dissolves the repetition MOTIVE before the decision —
the LLM plans its next call against "what is already covered / which
direction is dead / how much budget remains" instead of re-deriving
those facts from a diluted, partially-evicted history (PABU
arXiv:2602.09138; Anthropic context engineering — smallest high-signal
token set).

Discipline (all inherited from Coordinator-side lessons):
- positive recipe only (P7): the block names next actions ("转向 X /
  收尾"), never prohibitions — bans live in guard receipts;
- selective injection: silent until the first executed call;
- PG4 brevity: hard-capped by the ledger renderer;
- fail-safe: any injection error falls through to the unmodified
  request (same philosophy as the G24 L1 reminder);
- placement at the END of the system message (KV-prefix reuse).
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any


def _env_int(name: str, default: int) -> int:
    """Env var with safe fallback (test-only knob, not a product API)."""
    try:
        return max(1, int(os.environ.get(name, "") or default))
    except (TypeError, ValueError):
        return default

from langchain.agents.middleware import AgentMiddleware, hook_config
from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage

from deepagents.middleware._utils import append_to_system_message

from diagnostics.agent.delegation_key import delegation_key
from diagnostics.agent.expert_novelty_gate import (
    _hard_threshold as _g26_hard,
)
from diagnostics.agent.expert_session_ledger import (
    ExpertSessionLedger,
    expected_dimensions,
    parse_focus_dims,
)
from diagnostics.agent.expert_stall_watchdog import (
    _hard_threshold as _g19_hard,
    _is_zero_yield,
    _total_hard,
    _total_soft,
)
from diagnostics.agent.model_budget import (
    MAX_OUTPUT_TOKENS,
    reasoning_pressure_chars,
    truncation_risk_chars,
)
from diagnostics.agent.model_usage import (
    extract_usage,
    first_truncated,
    messages_of,
    reasoning_chars,
)

logger = logging.getLogger(__name__)


# Wrap-up reasoning cut (v3.37.3, P0).  Scenario 34 evidence: one truncated
# turn spent all 16384 output tokens on a 71419-character reasoning stream
# (0 content, 0 tool_calls) — the reasoning loop had taken over the budget.
# The recovery turns must therefore run WITHOUT reasoning: probe against
# ollama (qwen3.6:35b-mlx, 2026-09-11) shows reasoning_effort="none" yields
# 0 reasoning tokens, while thinking={"type":"disabled"} is accepted but
# IGNORED.  Applied only to OllamaChatOpenAI instances (the value travels
# as extra_body; other backends could reject it) and only for a delegation
# that has already truncated once.
_WRAPUP_THINKING_OFF = os.getenv(
    "DIAGNOSTICS_WRAPUP_THINKING_OFF", "1").strip() != "0"


def _thinking_off_variant(model: Any) -> Any | None:
    """Thinking-disabled variant of *model* (None when not applicable)."""
    if not _WRAPUP_THINKING_OFF:
        return None
    try:
        from diagnostics.agent.ollama_chat import OllamaChatOpenAI

        if not isinstance(model, OllamaChatOpenAI):
            return None
        if getattr(model, "reasoning_effort_override", None):
            return None
        return model.model_copy(update={"reasoning_effort_override": "none"})
    except Exception:
        return None


_GOAL_MARK_RE = re.compile(r"\[Coordinator 委派指令\]\s*(.+)", re.S)


def _delegation_goal(text: str) -> str:
    """The delegated task statement (progress frame, v3.37.6).

    Taken verbatim from the Coordinator's instruction block — the block
    frames "how far from the goal am I", so the goal must be the task that
    was actually delegated, never a system invention.
    """
    m = _GOAL_MARK_RE.search(text or "")
    if not m:
        return ""
    return " ".join(m.group(1).split())[:120]


def _call_ref(tool: str, args: Any) -> str:
    """Compact ``tool(arg/arg)`` label used to count repeatable calls.

    Keeps at most three discriminating values, each capped at 18 chars: the
    progress block is a fixed-cost channel and argument sets are dominated
    by low-signal fields (time windows, limits).  The label lets the frame
    NAME the call an expert is about to repeat; it is not a parameter mirror.
    """
    if not isinstance(args, dict) or not args:
        return tool
    vals: list[str] = []
    for _k, v in args.items():
        if v in (None, "", [], {}):
            continue
        s = str(v).replace("\n", " ").strip()
        if len(s) > 18:
            s = s[:17] + "…"
        vals.append(s)
        if len(vals) >= 3:
            break
    return f"{tool}({'/'.join(vals)})" if vals else tool


def _delegation_text(request: Any) -> str:
    """Text of the delegation messages (description + system-injected context).

    P-1 consumes this to extract the Coordinator-named focus dimensions;
    only HumanMessage content is read (the injected context and the
    Coordinator's ``[Coordinator 委派指令]`` block both live there).
    """
    parts: list[str] = []
    for m in (getattr(request, "messages", None) or []):
        if isinstance(m, HumanMessage):
            c = getattr(m, "content", "")
            if isinstance(c, str):
                parts.append(c)
    return "\n".join(parts)


def _last_ai_text_chars(state: Any) -> int:
    """Length of the last assistant text left in the delegation.

    Zero means the turn was cut off inside the reasoning stream (no
    visible output at all); a non-zero value means some text exists but
    was never submitted as a structured conclusion.
    """
    messages = state.get("messages", []) if isinstance(state, dict) else []
    for msg in reversed(list(messages)):
        if isinstance(msg, HumanMessage):
            continue
        content = getattr(msg, "content", "") or ""
        if isinstance(content, str) and content.strip():
            return len(content.strip())
    return 0


def _channels(tool_names: list[str]) -> list[str]:
    """Observability channel labels derived from the bound toolset.

    The subagent's own name is not visible inside the middleware, but
    every Argus expert binds ``get_argus_<domain>_*`` tools (legacy
    ``query_argus_<domain>_*`` kept for backward compat), so the domain
    identifies the channel for logs and for the Coordinator-facing
    degradation note.
    """
    domains: list[str] = []
    for name in tool_names or []:
        parts = name.split("_")
        is_argus = len(parts) >= 3 and parts[1] == "argus" and parts[0] in ("get", "query")
        if is_argus:
            if parts[2] not in domains:
                domains.append(parts[2])
    return domains or [n for n in (tool_names or [])[:3]]


# Tools that never carry diagnostic information into the ledger:
# planner scaffolding, file tools (G11's domain), ledger/delegation
# tools (Coordinator-side), and the structured-return conclusion tools
# (defense in depth — they are intercepted at the model node's
# structured-output path and normally never reach this middleware).
# Redundancy wrap-up (v3.37.3, P1).  A delegation that keeps re-issuing the
# SAME call with the SAME parameters is not converging — the per-call block
# receipts (dedup's ⛔ message already says "produce your conclusion") have
# not stopped it.  Measured 2026-09-11 (scenario 34, kmc-expert): 6 identical
# calls, 5 blocked, the resulting confusion drove the reasoning stream into a
# 69×-repeated loop that consumed the whole 16384 output budget.  Circuit
# breaker practice (AgentOps Circuit Breaker; deer-flow's LOOP DETECTED) is
# to convert the PATTERN — not the single call — into a forced wrap-up.
# The signal reuses the G27/G28 latch and guidance channel, so the cost stays
# bounded (guidance + one strengthened reminder, no extra turns by itself).
# v3.39.2: lowered 3 → 1.  Measured 2026-09-12 (scenario 38, delegation
# del:26788e7ca9ac): 20 identical-parameter calls were blocked by dedup and
# the expert STILL kept going — the loop ended in a length-truncated turn with
# no conclusion.  The FIRST repeat is already diagnostic; waiting for the
# third wastes two round-trips and, on a saturated output budget, costs the
# conclusion itself.  The latch keeps the guidance one-shot, so an earlier
# trigger cannot repeat or spam.  Proactive by design: the block receipts are
# the reactive fallback, not the primary teacher.
_REDUNDANCY_WRAPUP_HITS = _env_int("DIAGNOSTICS_REDUNDANCY_WRAPUP_HITS", 1)
_REDUNDANCY_WRAPUP_GUIDANCE = (
    "[系统提示·重复取证收敛] 本委派已有多次完全相同参数的调用被系统去重"
    "（结果已缓存并返回到上下文中，重复调用不会获得新数据）——请立即基于"
    "已采集数据调用结论工具收尾：尚未取证的维度在 coverage_gaps 按"
    "『维度名：类型｜原因』声明（类型：数据不可用/不适用/强制收尾）。"
    "不要再用相同参数重复调用任何工具。"
)

    # Q4 (v3.38.5): near-truncation reasoning pressure.  A single turn whose
    # reasoning stream runs an order of magnitude longer than its siblings is
    # the leading indicator of the degeneracy that ends in a length
    # truncation (2026-09-11 session fe3a8604: 40417 chars vs ≤4095
    # elsewhere).  Guidance on the NEXT turn is proactive; the thinking-off
    # recovery after a real truncation stays as the reactive backstop.
    # Industry context: CoT length vs accuracy follows an inverted-U
    # (ICLR 2026 "When More Is Less"), i.e. the over-long tail is exactly
    # where quality degrades.
# v3.39.0: the threshold is DERIVED from the deployment's output cap
# (model_budget.reasoning_pressure_chars) rather than fixed here.  Both
# supported backends spend `max_tokens` on CoT + content together, and the
# deployment uses ONE cap for both (32K) — so 32768 tokens → 36864 chars,
# i.e. half the budget, leaving the other half for the conclusion itself.
# The old fixed 8000 was calibrated on 16K-token mock runs and would fire
# constantly at 32K/64K (measured 2026-09-11).
_REASONING_PRESSURE_GUIDANCE = (
    "[系统提示·思考收敛] 上一轮你的内部推理异常冗长（远超正常水平，已接近"
    "输出上限）——继续发散会耗尽输出预算、导致结论无法产出。请立即停止展开"
    "推理，基于已采集数据直接调用结论工具收尾：尚未取证的维度在 coverage_gaps"
    " 按『维度名：类型｜原因』声明。"
)
# v3.39.2 (C): output-budget guard.  The cap is shared by CoT and content on
# both backends.  When the PREVIOUS turn's CONTENT alone is already near the
# ceiling, the next turn must conclude rather than emit more bulk — measured
# 2026-09-12 (scenario 38): a turn saturated 16384 tokens with
# reasoning_chars=0, i.e. content-only saturation, and the conclusion was lost.
_OUTPUT_PRESSURE_GUIDANCE = (
    "[系统提示·输出预算保护] 上一轮你的输出内容已接近输出上限——继续输出大段内容"
    "（写文件、长文本）会触发系统截断并丢失结论。请立即收敛：直接调用结论工具"
    "提交结论；确需落盘的内容精简为关键摘要，未覆盖项在 coverage_gaps 声明。"
)

_SKIP_TOOLS = frozenset({
    "write_todos", "read_todos",
    "read_file", "edit_file", "write_file", "ls", "glob", "grep",
    "task", "list_diagnostic_capabilities",
    "DeepExpertFindings", "ArgusExpertFindings",
})


def _declared_gaps(sr: Any) -> list[str]:
    """Gap declarations carried by the conclusion itself.

    G27's receipt has always said "declare the gap and resubmit", but the
    checkpoint could only compare the tool-derived expectation against
    the executed calls — a declared gap was invisible, so the expert had
    to spend one more collection turn even when the dimension was
    genuinely unavailable (2026-09-09 session 59e7da10: kmc-argus cost a
    139.5s collection turn + a 58.4s conclusion turn for a dimension it
    had already called out).  The conclusion schema now carries
    `coverage_gaps`, which makes the promise machine-checkable.
    """
    raw = getattr(sr, "coverage_gaps", None) if sr is not None else None
    if raw is None and isinstance(sr, dict):
        raw = sr.get("coverage_gaps")
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, (list, tuple)):
        return []
    return [str(x).strip() for x in raw if str(x).strip()]


def _gap_declared(dim: str, declared: list[str]) -> bool:
    """Whether *dim* is covered by one of the declarations.

    Two-directional substring match: the declaration usually embeds the
    dimension name with a reason suffix ("KMC Pod指标：无 Pod"), but the
    model may also abbreviate it ("Pod指标").  The short-token guard
    stops a vague declaration such as "指标" from silently clearing every
    dimension and disabling the checkpoint.

    v3.37.2 (structured declarations, guidance-first): a declaration that
    follows the TAUGHT shape (「维度名：类型｜原因」) is matched on its
    parsed dimension field first.  The historical substring paths remain
    in force for every entry — including structured ones whose field
    match failed — so this can only ADD matches, never remove one.
    """
    for g in declared:
        parsed = parse_gap_declaration(g)
        if parsed is not None:
            gdim = parsed[0]
            if dim == gdim or dim in gdim or (len(gdim) >= 4 and gdim in dim):
                return True
        if dim in g:
            return True
        if len(g) >= 4 and g in dim:
            return True
    return False


# ── Structured gap declarations (v3.37.2) ─────────────────────────────
# The declaration CHANNEL has existed since v3.26.0, but its payload was
# free text: the checkpoint could only substring-match a dimension name,
# and nobody downstream could tell WHY a dimension was skipped (data
# truly unavailable vs. irrelevant to the task vs. cut off by a budget
# guard — a distinction the Coordinator is explicitly asked to preserve).
# Per the project principle「前置引导优先于反应式拦截」the shape is taught
# AT THE DECISION POINT (the conclusion field description the expert fills
# in + the closing contract), and the parser merely consumes it.  The
# reactive checkpoint gains precision WITHOUT becoming stricter: free-text
# declarations keep passing exactly as before.
_GAP_KINDS = ("数据不可用", "不适用", "强制收尾")

_GAP_STRUCT_RE = re.compile(
    r"^\s*(?P<dim>[^：:｜|]{1,24})\s*[：:]\s*(?P<kind>"
    + "|".join(_GAP_KINDS)
    + r")\s*(?:[｜|:：]\s*(?P<reason>.*))?$",
    re.DOTALL,
)


def parse_gap_declaration(entry: str) -> tuple[str, str, str] | None:
    """(dimension, kind, reason) when *entry* follows the taught shape.

    Returns None for any other free-text declaration (the caller then
    applies the historical substring match unchanged).
    """
    m = _GAP_STRUCT_RE.match(str(entry or "").strip())
    if not m:
        return None
    return (m.group("dim").strip(), m.group("kind"),
            (m.group("reason") or "").strip())


class ExpertGuidanceMiddleware(AgentMiddleware):
    """Subagent-only progress recorder + guidance injector."""

    def __init__(self, ledger: ExpertSessionLedger) -> None:
        self._ledger = ledger
        # Optional back-reference to the shared dedup middleware so the
        # injected block can report this delegation's dedup-hit count
        # (display-only — dedup remains the single bookkeeper).
        self._dedup: Any | None = None
        # Back-reference to the Coordinator's ledger middleware.  A
        # delegation runs as a subgraph whose state schema shares no keys
        # with the parent (LangGraph semantics: parent keys are not
        # accessible inside the subgraph), so the shared diagnosis ledger
        # — the only place a lost channel can be reported to the
        # Coordinator — is reachable only through this reference.
        self._coordinator: Any | None = None

    def bind_dedup(self, dedup: Any) -> None:
        self._dedup = dedup

    def bind_coordinator(self, coordinator: Any) -> None:
        self._coordinator = coordinator

    def _shared_ledger(self) -> dict | None:
        """The Coordinator's live ledger, or None when unavailable."""
        ledger = getattr(self._coordinator, "_current_ledger", None)
        return ledger if isinstance(ledger, dict) else None

    @staticmethod
    def _key_from_state(state: Any) -> str:
        """Same derivation as G11/G19/G24/G26 (shared helper): each
        delegation is a fresh agent invocation whose first HumanMessage
        carries the task description — keying on it separates
        concurrent/stale delegations deterministically (the middleware
        instance is a factory singleton shared across all delegations)."""
        return delegation_key(state)

    @classmethod
    def _delegation_key(cls, request: Any) -> str:
        return cls._key_from_state(getattr(request, "state", None) or {})

    # ── consumer A-1: record executed calls into the ledger ──

    async def awrap_tool_call(self, request, handler):
        tool_call = getattr(request, "tool_call", None) or {}
        tool_name = tool_call.get("name", "")
        if tool_name in _SKIP_TOOLS:
            return await handler(request)

        result = await handler(request)
        if isinstance(result, ToolMessage):
            content = result.content if isinstance(result.content, str) else str(result.content)
            # Outer-layer rejections (⛔) never reach this middleware in
            # the wired stack order; the check is defense in depth so a
            # rejected call never pollutes dimension coverage.
            if not content.strip().startswith("⛔"):
                verdict = "zero_yield" if _is_zero_yield(result) else "yield"
                self._ledger.record_call(
                    self._delegation_key(request), tool_name, verdict,
                    ref=_call_ref(tool_name, tool_call.get("args") or {}))
        return result

    # ── consumer A-2: inject the progress block before each model call ──

    async def awrap_model_call(self, request, handler):
        try:
            key = self._delegation_key(request)
            dedup_hits = 0
            if self._dedup is not None:
                hits = self._dedup._dedup_hit_counts.get(key)
                dedup_hits = sum(hits.values()) if hits else 0
            # P1 (v3.37.3): redundancy wrap-up — identical-parameter repeats
            # inside ONE delegation mean the model is not converging.  The
            # per-call block receipts did not stop it, so convert the
            # PATTERN into a forced wrap-up (guidance channel + latch).
            if (dedup_hits >= _REDUNDANCY_WRAPUP_HITS
                    and not self._ledger.conclusion_hinted(key)):
                self._ledger.mark_conclusion_hinted(
                    key, _REDUNDANCY_WRAPUP_GUIDANCE)
                logger.warning(
                    "Redundancy wrap-up: delegation %s has %d deduplicated "
                    "identical call(s) — forced wrap-up guidance injected",
                    key, dedup_hits,
                )
            # Q4 (v3.38.5): reasoning pressure from the PREVIOUS turn — tell
            # the model to converge BEFORE the output budget is exhausted.
            # Proactive counterpart of the truncation recovery below; reuses
            # the same one-time latch channel (selective, never repeats).
            _pressure = reasoning_pressure_chars()
            if (self._ledger.reasoning_chars(key) >= _pressure
                    and not self._ledger.conclusion_hinted(key)):
                self._ledger.mark_conclusion_hinted(
                    key, _REASONING_PRESSURE_GUIDANCE)
                logger.warning(
                    "Reasoning pressure: delegation %s previous-turn "
                    "reasoning=%d chars (>= %d chars = 50%% of the %d-token "
                    "output cap) — convergence guidance injected",
                    key, self._ledger.reasoning_chars(key),
                    _pressure, MAX_OUTPUT_TOKENS,
                )
            # v3.39.2 (C): content-side saturation — the other way a turn can
            # eat the shared output budget (see _OUTPUT_PRESSURE_GUIDANCE).
            _out_pressure = truncation_risk_chars()
            if (self._ledger.output_chars(key) >= _out_pressure
                    and not self._ledger.conclusion_hinted(key)):
                self._ledger.mark_conclusion_hinted(
                    key, _OUTPUT_PRESSURE_GUIDANCE)
                logger.warning(
                    "Output pressure: delegation %s previous-turn content="
                    "%d chars (>= %d = 90%% of the %d-token output cap) — "
                    "conclusion-first guidance injected",
                    key, self._ledger.output_chars(key),
                    _out_pressure, MAX_OUTPUT_TOKENS,
                )
            tool_names = []
            for t in (getattr(request, "tools", None) or []):
                name = getattr(t, "name", None) or (
                    t.get("name", "") if isinstance(t, dict) else "")
                if name:
                    tool_names.append(name)
            # Track the bound toolset so the conclusion checkpoint
            # (after_model, which has no ModelRequest) can compute the
            # expected-dimension set.
            self._ledger.set_tools(key, tool_names)
            # P-1 (v3.35.0): parse the Coordinator-named focus dimensions
            # from the delegation description on the FIRST model call.
            # Idempotent thereafter (set_focus only writes while unset) so
            # the task scope cannot drift mid-delegation.
            if self._ledger.session(key)["focus_dims"] is None:
                _text = _delegation_text(request)
                self._ledger.set_focus(key, parse_focus_dims(_text))
                # v3.37.6: frame the progress block around the delegated
                # goal (set once, from the Coordinator's instruction).
                self._ledger.set_goal(key, _delegation_goal(_text))
            parts = []
            pending = self._ledger.pop_pending_guidance(key)
            if pending:
                parts.append(pending)
            block = self._ledger.render(
                key, tool_names, dedup_hits, _total_soft(), _total_hard())
            if block:
                parts.append(block)
            if parts:
                request = request.override(
                    system_message=append_to_system_message(
                        request.system_message, "\n".join(parts)))
            # Decision-point restatement of the conclusion contract: the
            # progress block lives in the system message (far from the
            # generated token, behind every tool result), so the two
            # clauses the concluding turn must honour are repeated as the
            # last message in the context (design document §9, v3.26.0).
            reminder = self._ledger.closing_reminder(key, tool_names)
            if reminder:
                messages = list(getattr(request, "messages", []) or [])
                if messages:
                    request = request.override(
                        messages=[*messages, SystemMessage(content=reminder)])
            # P0 (v3.37.3): once this delegation has truncated, the recovery
            # turns run without reasoning — the truncated turn spent its
            # whole output budget on a reasoning loop (71419 chars, 0
            # content/tool_calls), so the retry must not repeat that.
            if self._ledger.truncation_count(key):
                variant = _thinking_off_variant(request.model)
                if variant is not None:
                    request = request.override(model=variant)
        except Exception:
            pass  # guidance must never become a failure source
        import time as _time
        _started = _time.monotonic()
        response = await handler(request)
        # ── G28: record length-truncated turns ──
        # The recovery decision runs in after_model, which has no
        # ModelRequest and therefore no access to the response: the
        # truncation must be detected here and bookkept per delegation
        # key (the instance is shared across experts).
        try:
            _k = self._delegation_key(request)
            self._record_truncation(
                _k, response,
                round(_time.monotonic() - _started, 1),
            )
            # Q4 (v3.38.5): remember this turn's reasoning volume so the NEXT
            # turn can be told to converge (see model_budget.reasoning_pressure_chars).
            # v3.38.6 fix: use the shared model_usage helpers.  The first cut
            # read a raw `response.message` attribute, which a LangChain
            # AIMessage does not have — so the recorded value was always 0 and
            # the guidance never fired in the field (2026-09-11 session
            # b050c5c8: an expert turn carried 15691 reasoning chars with no
            # guidance injected).  Best-effort: never break the model call.
            try:
                _msgs = messages_of(response)
                _chars = max(
                    (reasoning_chars(m) for m in _msgs),
                    default=0,
                )
                self._ledger.set_reasoning_chars(_k, _chars)
                # v3.39.2 (C): remember the CONTENT volume too — it is the
                # other half of the shared output budget, and a content-only
                # saturation is invisible to the reasoning counter.
                _content = 0
                for _m in _msgs:
                    _c = getattr(_m, "content", "") or ""
                    if isinstance(_c, list):
                        for _b in _c:
                            _content += (len(str(_b.get("text", "")))
                                         if isinstance(_b, dict)
                                         else len(str(_b)))
                    else:
                        _content += len(str(_c))
                self._ledger.set_output_chars(_k, _content)
            except Exception:
                pass
        except Exception:
            pass  # recovery bookkeeping must never break the model call
        return response

    # ── consumer C: coverage-aware conclusion checkpoint (G27) ──
    # The structured-return conclusion tools never pass through
    # wrap_tool_call — the model node's structured-output handling parses
    # the call directly, synthesizes its own ToolMessage, sets
    # structured_response and ends the loop (langchain agents factory;
    # verified 2026-09-08).  A receipt-side hint is therefore physically
    # invisible.  This hook instead detects the FIRST conclusion
    # submission of a delegation (structured_response set — a
    # backend-agnostic condition covering both ToolStrategy and
    # ProviderStrategy), and when the ledger shows UNCOVERED dimensions
    # it clears structured_response and issues jump_to="model" so the
    # expert re-decides with the coverage gap injected — a Verifier /
    # Generate-and-Test checkpoint with the discipline the pattern
    # demands: deterministic check (ledger facts, zero LLM) and a
    # BOUNDED single retry (one-time latch; the second submission
    # always passes, so the loop provably terminates — max +1 model
    # turn, 0 extra tool calls).
    @hook_config(can_jump_to=["model"])
    def after_model(self, state, runtime) -> dict | None:
        try:
            sr = state.get("structured_response") if isinstance(state, dict) else None
            if sr is None:
                # ── G28: length-truncated turn ──
                # No structured conclusion was parsed — most likely the
                # response hit the output cap.  Bounce once for a minimal
                # resubmission instead of letting the delegation end with
                # a fallback text the Coordinator cannot classify.
                return self._truncation_recovery(state)
            key = self._key_from_state(state)
            s = self._ledger.session(key)
            # Pass-through conditions (any one skips the checkpoint):
            # 1. one-time latch — the second submission always passes;
            # 2. zero executed calls (e.g. an Argus expert that could not
            #    query anything — bouncing would be a pointless turn);
            # 3. any hard block active (G19 zero-yield / total budget /
            #    G26) — the guards already demanded wrap-up; bouncing
            #    would contradict them;
            # 4. clarification marker set — the expert deterministically
            #    reported missing parameters, coverage is impossible.
            if s["conclusion_hinted"] or not s["calls"]:
                return None
            if (self._ledger.trailing_zero_yield_streak(key) >= _g19_hard()
                    or len(s["calls"]) >= _total_hard()
                    or s["novelty_streak"] >= _g26_hard()):
                return None
            clarification = getattr(sr, "clarification", "") or (
                sr.get("clarification", "") if isinstance(sr, dict) else "")
            if clarification:
                return None
            uncovered = expected_dimensions(s["tools"], s["focus_dims"]) - s["covered"]
            declared = _declared_gaps(sr)
            if declared and uncovered:
                uncovered = {
                    d for d in uncovered if not _gap_declared(d, declared)}
            if not uncovered:
                return None
            dims = "、".join(sorted(uncovered))
            guidance = (
                "[系统提示·结论完整性校验] 结论已收到。系统台账显示以下维度"
                f"尚无取证数据：{dims}——请补取其中与本次任务相关的维度后重新提交结论；"
                "确属数据不可用、与本任务无关、或被系统强制收尾的，在结论 coverage_gaps 中"
                "按『维度名：类型｜原因』逐条写明（类型取 数据不可用/不适用/强制收尾）"
                "后再提交（已声明的维度视为已交代，无需补采）。"
            )
            self._ledger.mark_conclusion_hinted(key, guidance)
            logger.info(
                "G27 conclusion checkpoint: delegation %s bounced for "
                "uncovered dimensions: %s", key, dims,
            )
            return {"jump_to": "model", "structured_response": None}
        except Exception:
            return None  # the checkpoint must never break the wrap-up path

    # ── G28: output-length truncation recovery ──────────────────────────
    # A length-truncated turn produces no structured_response, so the
    # model node's structured-output path silently falls back to the last
    # non-empty message text — a fragment that the Coordinator reads as
    # the expert's conclusion (observed 2026-09-08: a truncated
    # k8s-argus-expert came back as "【需要参数】缺少主机名", i.e. the
    # opposite of what it had actually found).  Two deterministic stages:
    #   1. one bounded resubmit — same evidence, minimal field set;
    #   2. still truncated → mark the delegation as "no conclusion" in
    #      the shared ledger so the Coordinator sees a lost channel, not
    #      a clean "nothing found".
    # Both stages reuse G27's one-time latch (conclusion_hinted), so a
    # delegation costs at most one extra model turn in total and the
    # loop provably terminates (0 extra tool calls).

    def _record_truncation(self, key: str, response: Any,
                           duration_s: float) -> None:
        """Bookkeep one length-truncated turn (observability + recovery)."""
        message = first_truncated(response)
        if message is None:
            return
        _in_tok, out_tok, reason_tok = extract_usage(message)
        reason_chars = reasoning_chars(message)
        channels = _channels(self._ledger.session(key).get("tools") or [])
        count = self._ledger.record_truncation(key, {
            "out": out_tok,
            "reasoning": reason_tok,
            "reasoning_chars": reason_chars,
            "duration_s": duration_s,
            "channels": channels,
        })
        # The recovery decision runs later in after_model (bounce once
        # when evidence exists, degrade otherwise) — the wording here
        # must not pre-empt that decision.
        logger.warning(
            "G28 expert output truncated (finish_reason=length, "
            "delegation %s, occurrence %d, out=%s, reasoning=%s, "
            "reasoning_chars=%d, duration=%.1fs, channels=%s) — no "
            "structured conclusion; recovery follows in after_model "
            "(bounce once with evidence, degrade otherwise)",
            key, count, out_tok or "?", reason_tok or "?",
            reason_chars, duration_s, ",".join(channels) or "?",
        )

    def _truncation_recovery(self, state: Any) -> dict | None:
        """Bounce a truncated delegation once; degrade otherwise.

        Degradation covers both terminal cases: the second truncated
        miss (the one bounce was already spent) and a first-turn
        truncation with zero forensics (nothing to conclude from, so
        the bounce is skipped — but the lost channel must still be
        surfaced to the Coordinator).
        """
        try:
            key = self._key_from_state(state)
            if not self._ledger.truncation_count(key):
                return None
            s = self._ledger.session(key)
            # Nothing was ever collected: the truncation hit the FIRST
            # model turn (reasoning exhausted the output budget before
            # any tool call — observed 2026-09-09).  A conclusion-
            # oriented resubmit would be pointless without evidence,
            # but ending silently would hand the Coordinator an empty
            # result it cannot tell apart from "checked, nothing
            # found" — the pseudo-signal G28 exists to eliminate
            # (design document §8 G28).  Degrade instead of bouncing.
            # Nothing was ever collected: the truncation hit the FIRST
            # model turn.  v3.37.9 (R1): the observed cause is the reasoning
            # stream exhausting the SHARED output budget (captured body of
            # such a turn: 71419 reasoning characters, 0 content, 0
            # tool_calls), and probing the same model shows tool calls are
            # emitted normally once reasoning is disabled.  So bounce ONCE
            # with reasoning off — the retry automatically runs on the
            # none-effort model variant selected in awrap_model_call — and
            # degrade only if that retry also fails (truncation_count >= 2,
            # which keeps the loop provably bounded).
            if not s["calls"]:
                if self._ledger.truncation_count(key) >= 2:
                    self._report_truncated_delegation(state, key)
                    return None
                guidance = (
                    "[系统提示·输出被截断] 上一次输出达到长度上限，且尚未产生"
                    "任何取证调用（本次已关闭长篇推理）。请立即调用 1-2 个最关键"
                    "的取证工具获取证据，然后提交结论——不要展开长篇推理。"
                )
                self._ledger.mark_conclusion_hinted(key, guidance)
                logger.warning(
                    "G28 zero-call truncation: delegation %s bounced once "
                    "with reasoning disabled", key,
                )
                return {"jump_to": "model"}
            if self._ledger.conclusion_hinted(key):
                # The one bounce was already spent (by G27 or by G28);
                # the second truncation is terminal — degrade instead of
                # looping.
                self._report_truncated_delegation(state, key)
                return None
            # The resubmission must name the conclusion TOOL: the
            # structured return only materialises when the model emits a
            # conclusion-tool call (the model node parses that call
            # directly), so "write a brief conclusion" is not enough — a
            # plain-text answer leaves structured_response unset and the
            # delegation ends with a fallback fragment.  The tool name is
            # taken from this delegation's bound toolset, so renamed or
            # additional conclusion tools need no code change.
            conclusion_tools = [t for t in (s.get("tools") or [])
                                if t.endswith("Findings")]
            tool_hint = " / ".join(conclusion_tools) or "结论工具"
            guidance = (
                "[系统提示·输出被截断] 上一次输出达到长度上限，结构化结论未生成——"
                f"已采集的 {len(s['calls'])} 项取证数据仍在上下文中，无需重新查询。"
                f"请立即**调用结论工具 {tool_hint}** 提交精简结论：只填写 "
                "verdict / key_evidence / negative_evidence 三个字段，每字段不超过 "
                "40 字，其余字段留空；不要用纯文本作答（纯文本不会被记为结论）。"
            )
            self._ledger.mark_conclusion_hinted(key, guidance)
            logger.warning(
                "G28 conclusion checkpoint: delegation %s bounced once "
                "for a minimal resubmission (%d executed calls retained, "
                "conclusion tool %s)",
                key, len(s["calls"]), tool_hint,
            )
            return {"jump_to": "model"}
        except Exception:
            return None  # the recovery must never break the wrap-up path

    def _report_truncated_delegation(self, state: Any, key: str) -> None:
        """Surface a lost channel to the Coordinator (once per delegation)."""
        try:
            if self._ledger.truncation_reported(key):
                return
            self._ledger.mark_truncation_reported(key)
            s = self._ledger.session(key)
            entry = {
                "delegation": key,
                "channels": _channels(s.get("tools") or []),
                "truncations": self._ledger.truncation_count(key),
                "calls": len(s["calls"]),
                # Fact, not a verdict: how much non-structured text the
                # last turn left behind.  Mirrors the vendor handling of
                # incomplete responses, which distinguishes "ran out of
                # tokens during reasoning" from "partial output" — the
                # Coordinator is told a fragment exists, but it is never
                # promoted to evidence here.
                "fallback_chars": _last_ai_text_chars(state),
            }
            ledger = state.get("_diagnosis_ledger") if isinstance(state, dict) else None
            if not isinstance(ledger, dict):
                ledger = self._shared_ledger()
            if isinstance(ledger, dict):
                entry["round"] = ledger.get("current_round", 0)
                # Ledger data (not a private marker): the Coordinator
                # renders it, so "channel lost" can never be read as
                # "channel found nothing".
                ledger.setdefault("truncated_delegations", []).append(entry)
            logger.error(
                "G28 delegation %s produced no conclusion after %d "
                "truncated turns (channels=%s, fallback_chars=%d) — "
                "marked as a lost channel",
                key, entry["truncations"],
                ",".join(entry["channels"]) or "?", entry["fallback_chars"],
            )
        except Exception:
            return None
