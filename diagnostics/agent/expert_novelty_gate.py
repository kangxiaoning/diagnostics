"""Expert low-information-gain gate (design document §8 G26, v3.24.0).

Completes the zero-increment defence line at the RESULT layer:

- G18: zero-increment LEDGER restatements (Coordinator);
- G24: zero-increment RE-ISSUES (identical name+args, dedup-hit);
- G19: zero-YIELD executions (empty / no-data / error);
- THIS gate (G26): executions that RETURN DATA but add no increment —
  same tool, different args (invisible to dedup), yielding content
  (invisible to G19) whose shape AND payload are near-identical to a
  result already collected in this delegation.  Observed failure mode:
  enumerating healthy objects one by one after an overview tool already
  returned the batch picture — every call "succeeds", none adds
  evidence (MAST FM-1.3 step repetition).

Detection (dual fingerprint, conservative intersection — same
philosophy as the G19 predicate):

1. STRUCTURAL fingerprint: digit/whitespace-normalised template hash —
   "is the returned data the same SHAPE as something already seen";
2. CONTENT fingerprint: token-set Jaccard ≥ 0.90 — "is the payload
   near-identical".

Both must hold for the SAME tool name (cross-tool comparison is an
open semantic problem — out of scope for v1; the G19-ext total budget
backstops it).  Numeric tokens are KEPT in the content fingerprint, so
materially divergent readings lower the Jaccard; the residual
false-positive class (same-shape results whose differing values each
carry diagnostic meaning) is accepted by design (design document §11
v3.24.0): the soft/hard ladder grants a 2-call grace, and the hard
block forces SYNTHESIS of already-collected data — never data loss.

Intervention ladder (G19-shaped):
- soft (default 2, DIAGNOSTICS_EXPERT_NOVELTY_SOFT): append a positive
  wrap-up/switch-dimension recipe to the tool result;
- hard (default 4, DIAGNOSTICS_EXPERT_NOVELTY_HARD): refuse further
  tool calls with a mandatory wrap-up message; the expert can still
  emit its final synthesis, so convergence is guaranteed.  Conclusion
  tools are exempt (they are the wrap-up path — and are normally
  intercepted by the model node's structured-output handling before
  reaching this middleware anyway).

Literature: novelty-and-redundancy detection is a mature deterministic
technique (Zhang et al., SIGIR'02); MAST FM-1.3 (17.14%, the most
frequent multi-agent failure mode); Pirolli & Card — a foraging patch
whose yield no longer covers its cost should be abandoned.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import HumanMessage, ToolMessage

from diagnostics.agent.expert_session_ledger import ExpertSessionLedger
from diagnostics.agent.expert_stall_watchdog import _is_zero_yield

logger = logging.getLogger(__name__)


def _env_int(name: str, default: int) -> int:
    """Env var with safe fallback (test-only knob, not a product API)."""
    try:
        return max(1, int(os.environ.get(name, "") or default))
    except (TypeError, ValueError):
        return default


def _soft_threshold() -> int:
    return _env_int("DIAGNOSTICS_EXPERT_NOVELTY_SOFT", 2)


def _hard_threshold() -> int:
    return _env_int("DIAGNOSTICS_EXPERT_NOVELTY_HARD", 4)


def _similarity_threshold() -> float:
    try:
        v = float(os.environ.get("DIAGNOSTICS_EXPERT_NOVELTY_SIM", "") or 0.9)
        return min(1.0, max(0.5, v))
    except (TypeError, ValueError):
        return 0.9


# Tokens: ascii words/numbers (with unit/separator suffixes) or single
# CJK chars.  Numbers stay in the fingerprint — divergent readings must
# lower the similarity (see module docstring).
_TOKEN_RE = re.compile(r"[a-z0-9]+(?:[._%/-][a-z0-9]+)*|[一-鿿]")
_DIGIT_RUN_RE = re.compile(r"\d+(?:\.\d+)?")
_WS_RE = re.compile(r"\s+")
# Fingerprinting input cap — defensive; deepagents' eviction already
# keeps ToolMessage content bounded (~80k chars).
_FP_INPUT_CAP = 60000


def _struct_hash(text: str) -> int:
    """Digit/whitespace-normalised template hash (same shape ⇒ same
    hash, regardless of the concrete values)."""
    norm = _DIGIT_RUN_RE.sub("N", text[:_FP_INPUT_CAP])
    norm = _WS_RE.sub(" ", norm)
    return hash(norm)


def _tokens(text: str) -> frozenset:
    return frozenset(_TOKEN_RE.findall(text[:_FP_INPUT_CAP].lower()))


def _jaccard(a: frozenset, b: frozenset) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


# Same exemption philosophy as G19: planner scaffolding and the
# structured-return conclusion tools are never evidence gathering.
# File tools belong to G11's domain (path discipline + read budget) —
# cross-mechanism double counting is avoided by excluding them here.
_SKIP_TOOLS = frozenset({
    "write_todos", "read_todos",
    "read_file", "edit_file", "write_file", "ls", "glob", "grep",
    "task", "list_diagnostic_capabilities",
    "DeepExpertFindings", "ArgusExpertFindings",
})


class ExpertNoveltyGateMiddleware(AgentMiddleware):
    """Subagent-only low-information-gain gate (G26).

    Shares the ExpertSessionLedger with the guidance middleware
    (bookkeep once): fingerprints live in the per-delegation session.
    """

    def __init__(self, ledger: ExpertSessionLedger) -> None:
        self._ledger = ledger

    @staticmethod
    def _delegation_key(request: Any) -> str:
        """Same derivation as G19/G11/G24 (first-HumanMessage hash)."""
        state = getattr(request, "state", None) or {}
        messages = state.get("messages", []) if isinstance(state, dict) else []
        for msg in messages:
            if isinstance(msg, HumanMessage):
                content = msg.content if isinstance(msg.content, str) else str(msg.content)
                return f"del:{hash(content[:800])}"
        return "del:unknown"

    async def awrap_tool_call(self, request, handler):
        tool_call = getattr(request, "tool_call", None) or {}
        tool_name = tool_call.get("name", "")
        if tool_name in _SKIP_TOOLS:
            return await handler(request)

        key = self._delegation_key(request)
        streak = self._ledger.novelty_streak(key)

        if streak >= _hard_threshold():
            tool_call_id = tool_call.get("id") or f"nov_{abs(hash(key))}"
            logger.warning(
                "G26 hard block: expert delegation %s tool %s — %d "
                "consecutive low-gain calls; mandatory wrap-up",
                key, tool_name, streak,
            )
            return ToolMessage(
                content=(
                    f"⛔ 系统强制收尾（低信息增量防护）：已连续 {streak} 次调用"
                    "返回与已有结果高度相似的数据（同形态、内容重合≥90%）——"
                    "继续同类调用的边际信息已低于成本。\n"
                    "你不得再调用任何工具。立即基于已采集的信息输出最终结论"
                    "（如实说明数据缺口与置信度），结束本次委派。"
                ),
                tool_call_id=tool_call_id,
            )

        result = await handler(request)
        if not isinstance(result, ToolMessage):
            return result

        content = result.content if isinstance(result.content, str) else str(result.content)
        # G19's domain (zero-yield / error / outer rejection) is NOT
        # re-counted here — each guard owns exactly one failure class.
        if (getattr(result, "status", "success") == "error"
                or content.strip().startswith("⛔")
                or _is_zero_yield(result)):
            return result

        struct_hash = _struct_hash(content)
        tokens = _tokens(content)
        sim_threshold = _similarity_threshold()
        low_gain = any(
            h == struct_hash and _jaccard(tokens, t) >= sim_threshold
            for h, t in self._ledger.fingerprints(key, tool_name)
        )
        self._ledger.add_fingerprint(key, tool_name, struct_hash, tokens)

        if not low_gain:
            if streak:
                self._ledger.reset_novelty(key)
            return result

        streak = self._ledger.bump_novelty(key)
        if streak == _soft_threshold():
            guidance = (
                f"\n\n[系统提示·低信息增量防护] 最近 {streak} 次调用返回的数据"
                "与已有结果高度相似（同形态、内容重合≥90%），未带来新证据。\n"
                "请盘点已采集证据：足够置信判断则立即输出结论；仍有明确缺口则换"
                "一个**新维度**（不同工具/不同观测视角）取证，而非换措辞重试同类"
                "查询。持续低增量调用将被系统强制收尾。"
            )
            result = ToolMessage(
                content=content + guidance,
                tool_call_id=result.tool_call_id,
                name=getattr(result, "name", None),
                status=getattr(result, "status", "success"),
            )
            logger.warning(
                "G26 soft warning: expert delegation %s — %d consecutive "
                "low-gain calls", key, streak,
            )
        else:
            logger.info(
                "G26 streak: delegation %s low-gain streak=%d", key, streak,
            )
        return result
