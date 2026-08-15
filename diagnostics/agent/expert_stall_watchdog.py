"""Expert zero-yield stall watchdog (design document §8 G19, v3.11.0).

Complements the two existing subagent guards, each covering a distinct
failure class:

- dedup middleware: SAME-call repetition (identical name+args) and
  repeated-failure circuit breaking;
- file-tool governance: FILE-tool path discipline + read budget;
- THIS middleware: CONSECUTIVE ZERO-YIELD executions — a run of
  successfully-executed tool calls (possibly all different, so invisible
  to dedup; mostly query tools, so invisible to file governance) that
  return no data, an error, or a system rejection.  Observed failure
  mode: an expert polling per-host metrics where no monitoring data
  exists keeps enumerating hosts (dozens of futile calls) instead of
  concluding "data unavailable" and wrapping up.

Literature: MAST (arXiv:2503.13657) FM-1.3 step repetition (17.14%, the
single most frequent multi-agent failure mode) and FM-1.5 unaware of
termination conditions (9.82% — AG2 mathproxyagent looped "Continue"
statements); HalMit (arXiv:2507.15903) black-box watchdog intervention.

Design (same graduated philosophy as the dedup F2 escalation):

- per-delegation streak of consecutive zero-yield calls; any yielding
  call resets it (information arrived → the loop is productive);
- soft threshold: append a wrap-up directive to the tool result
  (positive next-action recipe, P7);
- hard threshold: hard-block further tool calls with a mandatory
  wrap-up message — the expert can still emit its final synthesis
  (text), so convergence is guaranteed: every additional tool call is
  refused until the delegation ends.

Planner scaffolding (todo writes) is ignored entirely — neither counted
nor reset by — so it cannot game the streak either way.
"""

from __future__ import annotations

import logging
import os
import re
from collections import OrderedDict
from typing import Any

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import HumanMessage, ToolMessage

logger = logging.getLogger(__name__)


def _env_int(name: str, default: int) -> int:
    """Env var with safe fallback (test-only knob, not a product API)."""
    try:
        return max(1, int(os.environ.get(name, "") or default))
    except (TypeError, ValueError):
        return default


def _soft_threshold() -> int:
    return _env_int("DIAGNOSTICS_EXPERT_STALL_SOFT", 5)


def _hard_threshold() -> int:
    return _env_int("DIAGNOSTICS_EXPERT_STALL_HARD", 10)


# Zero-yield content patterns — matched ONLY on short results (a long
# payload mentioning "not found" inside real diagnostic data must not
# count as zero-yield).  Mirrors the dedup middleware's conservative
# short-content-only philosophy.
_ZERO_YIELD_RE = re.compile(
    r"无监控数据|无数据|暂无数据|查无|未查询到|没有查询到|查询失败|无法查询|"
    r"无可用|不可用|不存在|无匹配|没有数据|"
    r"not found|no data|no results|no metrics|no matching|unavailable",
    re.IGNORECASE,
)
_ZERO_YIELD_MAX_LEN = 300

# Tools that never carry diagnostic information (planner scaffolding).
_IGNORED_TOOLS = frozenset({"write_todos", "read_todos"})

# Per-instance delegation-key ledger bound.
_MAX_TRACKED_DELEGATIONS = 64


def _is_zero_yield(result: ToolMessage) -> bool:
    """Conservative zero-yield predicate (design document §8 G19)."""
    if getattr(result, "status", "success") == "error":
        return True
    content = result.content if isinstance(result.content, str) else str(result.content)
    text = content.strip()
    if not text:
        return True
    if text.startswith("⛔"):
        # System rejection reached execution depth (defense in depth —
        # outer guards normally intercept before this middleware).
        return True
    if len(text) <= _ZERO_YIELD_MAX_LEN and _ZERO_YIELD_RE.search(text):
        return True
    return False


class ExpertStallWatchdogMiddleware(AgentMiddleware):
    """Subagent-only zero-yield stall watchdog (G19).

    Attached to expert subagent middleware chains only; the coordinator
    loop has its own guards (G1/G3/G13).
    """

    def __init__(self) -> None:
        # delegation-key → consecutive zero-yield streak
        self._streaks: OrderedDict[str, int] = OrderedDict()
        self._blocked_announced: set[str] = set()

    # ── helpers ──────────────────────────────────────────────────

    @staticmethod
    def _delegation_key(request: Any) -> str:
        """Same derivation as the file-governance middleware: each expert
        delegation is its own agent invocation whose first HumanMessage
        carries the task description, so hashing the first HumanMessage
        separates concurrent/stale delegations deterministically."""
        state = getattr(request, "state", None) or {}
        messages = state.get("messages", []) if isinstance(state, dict) else []
        for msg in messages:
            if isinstance(msg, HumanMessage):
                content = msg.content if isinstance(msg.content, str) else str(msg.content)
                return f"del:{hash(content[:800])}"
        return "del:unknown"

    def _get_streak(self, key: str) -> int:
        streak = self._streaks.get(key, 0)
        if key in self._streaks:
            self._streaks.move_to_end(key)
        return streak

    def _set_streak(self, key: str, value: int) -> None:
        self._streaks[key] = value
        self._streaks.move_to_end(key)
        while len(self._streaks) > _MAX_TRACKED_DELEGATIONS:
            self._streaks.popitem(last=False)

    # ── interception ─────────────────────────────────────────────

    async def awrap_tool_call(self, request, handler):
        tool_call = getattr(request, "tool_call", None) or {}
        tool_name = tool_call.get("name", "")
        if tool_name in _IGNORED_TOOLS:
            return await handler(request)

        key = self._delegation_key(request)
        streak = self._get_streak(key)

        if streak >= _hard_threshold():
            tool_call_id = tool_call.get("id") or f"stall_{abs(hash(key))}"
            logger.warning(
                "G19 hard block: expert delegation %s tool %s — %d "
                "consecutive zero-yield calls; mandatory wrap-up",
                key, tool_name, streak,
            )
            return ToolMessage(
                content=(
                    f"⛔ 系统强制收尾（零产出停滞防护 G19）：已连续 {streak} 次工具调用"
                    "未获得任何有效数据——该方向的数据不可用，继续调用不会改善结果。\n"
                    "你不得再调用任何工具。立即基于已采集的信息输出最终结论"
                    "（如实说明数据缺口与置信度），结束本次委派。"
                ),
                tool_call_id=tool_call_id,
            )

        result = await handler(request)

        if not isinstance(result, ToolMessage):
            return result

        if _is_zero_yield(result):
            streak += 1
            self._set_streak(key, streak)
            if streak == _soft_threshold():
                guidance = (
                    f"\n\n[系统提示·零产出停滞防护 G19] 你已连续 {streak} 次工具调用"
                    "未获得有效数据。该方向大概率数据不可用——不要再逐对象穷举或"
                    "换路径重试同类查询；立即盘点已采集的信息并输出最终结论"
                    "（如实说明数据缺口）。继续零产出调用将被系统强制拦截。"
                )
                content = result.content if isinstance(result.content, str) else str(result.content)
                result = ToolMessage(
                    content=content + guidance,
                    tool_call_id=result.tool_call_id,
                    name=getattr(result, "name", None),
                    status=getattr(result, "status", "success"),
                )
                logger.warning(
                    "G19 soft warning: expert delegation %s — %d consecutive "
                    "zero-yield calls", key, streak,
                )
            elif _soft_threshold() < streak < _hard_threshold():
                logger.info(
                    "G19 streak: delegation %s zero-yield streak=%d",
                    key, streak,
                )
        else:
            if streak:
                self._set_streak(key, 0)
        return result
