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
from typing import Any

from langchain.agents.middleware import AgentMiddleware, hook_config
from langchain_core.messages import HumanMessage, ToolMessage

from deepagents.middleware._utils import append_to_system_message

from diagnostics.agent.expert_novelty_gate import (
    _hard_threshold as _g26_hard,
)
from diagnostics.agent.expert_session_ledger import (
    ExpertSessionLedger,
    expected_dimensions,
)
from diagnostics.agent.expert_stall_watchdog import (
    _hard_threshold as _g19_hard,
    _is_zero_yield,
    _total_hard,
    _total_soft,
)

logger = logging.getLogger(__name__)


# Tools that never carry diagnostic information into the ledger:
# planner scaffolding, file tools (G11's domain), ledger/delegation
# tools (Coordinator-side), and the structured-return conclusion tools
# (defense in depth — they are intercepted at the model node's
# structured-output path and normally never reach this middleware).
_SKIP_TOOLS = frozenset({
    "write_todos", "read_todos",
    "read_file", "edit_file", "write_file", "ls", "glob", "grep",
    "task", "list_diagnostic_capabilities",
    "DeepExpertFindings", "ArgusExpertFindings",
})


class ExpertGuidanceMiddleware(AgentMiddleware):
    """Subagent-only progress recorder + guidance injector."""

    def __init__(self, ledger: ExpertSessionLedger) -> None:
        self._ledger = ledger
        # Optional back-reference to the shared dedup middleware so the
        # injected block can report this delegation's dedup-hit count
        # (display-only — dedup remains the single bookkeeper).
        self._dedup: Any | None = None

    def bind_dedup(self, dedup: Any) -> None:
        self._dedup = dedup

    @staticmethod
    def _key_from_state(state: Any) -> str:
        """Same derivation as G19/G11/G24: each delegation is a fresh
        agent invocation whose first HumanMessage carries the task
        description — hashing it separates concurrent/stale delegations
        deterministically (the middleware instance is a factory
        singleton shared across all delegations)."""
        messages = state.get("messages", []) if isinstance(state, dict) else []
        for msg in messages:
            if isinstance(msg, HumanMessage):
                content = msg.content if isinstance(msg.content, str) else str(msg.content)
                return f"del:{hash(content[:800])}"
        return "del:unknown"

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
                    self._delegation_key(request), tool_name, verdict)
        return result

    # ── consumer A-2: inject the progress block before each model call ──

    async def awrap_model_call(self, request, handler):
        try:
            key = self._delegation_key(request)
            dedup_hits = 0
            if self._dedup is not None:
                hits = self._dedup._dedup_hit_counts.get(key)
                dedup_hits = sum(hits.values()) if hits else 0
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
        except Exception:
            pass  # guidance must never become a failure source
        return await handler(request)

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
                return None
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
            uncovered = expected_dimensions(s["tools"]) - s["covered"]
            if not uncovered:
                return None
            dims = "、".join(sorted(uncovered))
            guidance = (
                "[系统提示·结论完整性校验] 结论已收到。系统台账显示以下维度"
                f"尚无取证数据：{dims}——请补取相关维度数据后重新提交结论；"
                "若某维度与本次任务无关或其数据不可用，在结论的 "
                "negative_evidence 中如实声明该缺口后重新提交。"
            )
            self._ledger.mark_conclusion_hinted(key, guidance)
            logger.info(
                "G27 conclusion checkpoint: delegation %s bounced for "
                "uncovered dimensions: %s", key, dims,
            )
            return {"jump_to": "model", "structured_response": None}
        except Exception:
            return None  # the checkpoint must never break the wrap-up path
