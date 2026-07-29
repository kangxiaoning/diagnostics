"""Expert file-tool governance middleware (A1-A4).

Attach ONLY to expert subagents (factory.py ``sa["middleware"]``) — never
to the Coordinator.  Production observation (2026-07-29): task-delegated
experts issued dozens of paginated read_file/grep calls against
/large_tool_results and /_dedup_cache, and even grep against /proc, /sys.
The gain<cost gate only intercepts Coordinator task() delegations, so
expert-internal file-tool calls had zero cost constraint.  This middleware
closes that gap with four rules:

A1  Path whitelist hard gate — file tools may only touch
    ``/agent_data/skills/`` (skill files), ``/large_tool_results/``,
    ``/_dedup_cache/``, ``/tool_results/`` (mechanism read-back paths),
    plus ``ls``/``glob`` on ``/`` for navigation.  Everything else is
    denied.  Rationale: diagnosed-host data is reachable exclusively
    through the expert's dedicated remote diagnostic tools; paths like
    /proc or /sys do not exist in the virtual backend (StateBackend), so
    such calls always return "not found" — pure waste with gain ≡ 0 —
    and would read the WRONG host if a real-disk route is ever added.

A2  Read-window boost — read_file on the three read-back prefixes with
    limit<=100 (deepagents' DEFAULT_READ_LIMIT and the eviction message's
    suggested page size) is raised to _READ_WINDOW.  One wide read
    replaces ~5 paginated model round-trips; the marginal tokens of a
    wider page are far cheaper than a full model call per 100 lines.

A3  Exact-repeat dedup — identical (tool, args) read-class calls within
    the same delegation are blocked: repeating the same page yields zero
    information gain.  Pagination with different offsets is unaffected.

A4  File-tool budget — read_file/grep calls are counted per delegation.
    Past the soft budget, a remaining-budget hint is appended to results
    (nudging grep-targeted reads over sequential paging); at the hard
    budget, calls are blocked with a wrap-up instruction.  This sinks the
    delegation-level gain<cost philosophy (ledger.KAPPA_STOP) down to the
    read level: past κ, expected marginal gain < fixed call cost.

Per-delegation state is keyed by the delegation's initial HumanMessage
(subagents.py puts the task description as the subagent's first message),
so concurrent delegations are isolated without relying on config
internals.  Maps are LRU-bounded.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from typing import Any

from langchain.agents.middleware.types import AgentMiddleware
from langchain_core.messages import ToolMessage
from langgraph.prebuilt.tool_node import ToolCallRequest
from langgraph.types import Command

logger = logging.getLogger(__name__)

# ── Configuration (env-overridable; read lazily so tests can monkeypatch) ──

def _env_int(name: str, default: int) -> int:
    try:
        return max(1, int(os.getenv(name, str(default))))
    except ValueError:
        return default


def _read_window() -> int:
    """A2: boosted read_file limit for read-back paths."""
    return _env_int("DIAGNOSTICS_FILE_READ_WINDOW", 500)


def _soft_budget() -> int:
    """A4: counted calls beyond this get a remaining-budget hint."""
    return _env_int("DIAGNOSTICS_FILE_TOOL_SOFT_BUDGET", 8)


def _hard_budget() -> int:
    """A4: counted calls per delegation are hard-capped at this."""
    return _env_int("DIAGNOSTICS_FILE_TOOL_HARD_BUDGET", 15)


# ── Constants ──────────────────────────────────────────────────────────────

_FILE_TOOLS = frozenset({
    "read_file", "write_file", "edit_file", "ls", "glob", "grep",
})
"""All deepagents FilesystemMiddleware tools (path-bearing)."""

_READ_CLASS = frozenset({"read_file", "grep", "glob", "ls"})
"""A3 exact-repeat dedup applies to side-effect-free read tools."""

_COUNTED_TOOLS = frozenset({"read_file", "grep"})
"""A4 budget counts the storm-prone tools (observed in production)."""

_ALLOWED_PREFIXES = (
    "/agent_data/skills/",
    "/large_tool_results/",
    "/_dedup_cache/",
    "/tool_results/",
)
"""A1 whitelist: skill files + mechanism read-back paths."""

_READBACK_PREFIXES = (
    "/large_tool_results/",
    "/_dedup_cache/",
    "/tool_results/",
)
"""A2 boost scope: tool-result read-back paths (not skill files)."""

_MAX_TRACKED_DELEGATIONS = 32
"""LRU bound for per-delegation state maps."""

_DEFAULT_PAGE_LIMIT = 100
"""deepagents filesystem.py DEFAULT_READ_LIMIT — boost anything ≤ this."""


class ExpertFileToolGovernanceMiddleware(AgentMiddleware):
    """Path gate + read-window boost + repeat dedup + budget for experts.

    Must only be attached to expert subagents; the Coordinator's own
    file-tool behaviour (report writing under /agent_data/reports/) must
    stay untouched.
    """

    def __init__(self) -> None:
        # Per-delegation state, keyed by delegation description hash.
        self._seen: OrderedDict[str, set[str]] = OrderedDict()
        self._budget: OrderedDict[str, int] = OrderedDict()
        logger.info(
            "ExpertFileToolGovernance: window=%d, soft=%d, hard=%d",
            _read_window(), _soft_budget(), _hard_budget(),
        )

    # ── Per-delegation state (LRU-bounded) ─────────────────────────────

    @staticmethod
    def _delegation_key(request: ToolCallRequest) -> str:
        """Hash of the delegation's initial HumanMessage (= task description).

        Each task() invocation seeds the subagent state with exactly one
        HumanMessage holding the (unique) delegation instruction, so its
        hash isolates concurrent delegations.  Fallback "_unknown" shares
        one bucket — conservative but safe.
        """
        try:
            for msg in (request.state or {}).get("messages") or []:
                if getattr(msg, "type", None) == "human":
                    text = str(getattr(msg, "content", ""))
                    return hashlib.sha1(text.encode()).hexdigest()[:16]
        except Exception:
            pass
        return "_unknown"

    @staticmethod
    def _bucket(od: OrderedDict, key: str, default: Any) -> Any:
        if key in od:
            od.move_to_end(key)
            return od[key]
        od[key] = default
        if len(od) > _MAX_TRACKED_DELEGATIONS:
            od.popitem(last=False)
        return od[key]

    # ── A1: path policy ────────────────────────────────────────────────

    @staticmethod
    def _extract_path(tool_name: str, args: dict) -> str:
        if tool_name in ("read_file", "write_file", "edit_file"):
            return str(args.get("file_path") or "")
        # ls / glob / grep: missing path means virtual-root scope
        return str(args.get("path") or "/")

    @staticmethod
    def _path_allowed(tool_name: str, path: str) -> bool:
        if not path:
            return False
        for prefix in _ALLOWED_PREFIXES:
            if path.startswith(prefix) or path == prefix.rstrip("/"):
                return True
        # Root listing/globbing is harmless navigation inside the
        # virtual backend; grep on "/" (unscoped full-FS search) is not.
        if tool_name in ("ls", "glob") and path == "/":
            return True
        return False

    # ── Tool call interception ─────────────────────────────────────────

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command]],
    ) -> ToolMessage | Command:
        tool_call = getattr(request, "tool_call", None) or {}
        tool_name = tool_call.get("name", "")
        if tool_name not in _FILE_TOOLS:
            return await handler(request)

        tool_call_id = tool_call.get("id", "")
        tool_args = tool_call.get("args") or {}
        path = self._extract_path(tool_name, tool_args)
        dkey = self._delegation_key(request)

        # ── A1: path whitelist hard gate ──
        if not self._path_allowed(tool_name, path):
            logger.warning(
                "治理拦截(路径): %s(%s) delegation=%s", tool_name, path, dkey,
            )
            if tool_name == "grep" and path == "/":
                hint = (
                    "grep 必须限定到具体文件或允许的前缀"
                    "（如 /large_tool_results/<文件名>），不允许全盘搜索。"
                )
            else:
                hint = (
                    "允许访问：/agent_data/skills/（技能文件）、"
                    "/large_tool_results/、/_dedup_cache/、/tool_results/"
                    "（工具结果缓存）。"
                )
            return ToolMessage(
                content=(
                    f"⛔ [文件工具治理] 路径 {path or '(空)'} 不在允许范围内。\n"
                    "诊断 Agent 通过远程调用获取被诊断主机数据：本地虚拟文件"
                    "系统中不存在 /proc、/sys 等主机路径，这些调用读不到任何"
                    "与被诊断对象相关的数据。\n"
                    f"{hint}\n"
                    "远程主机/集群数据必须使用本专家配备的专用诊断工具获取，"
                    "不能用文件工具代替。"
                ),
                tool_call_id=tool_call_id,
                name=tool_name,
            )

        # ── A3: exact-repeat dedup within this delegation ──
        sig = ""
        seen: set[str] = set()
        if tool_name in _READ_CLASS:
            sig = f"{tool_name}:{json.dumps(tool_args, sort_keys=True, default=str)}"
            seen = self._bucket(self._seen, dkey, set())
            if sig in seen:
                logger.info(
                    "治理拦截(重复): %s delegation=%s args=%s",
                    tool_name, dkey, sig[:120],
                )
                return ToolMessage(
                    content=(
                        f"⛔ [文件工具治理] 重复调用：本次委派中已用完全相同的参数"
                        f"执行过 {tool_name}，结果就在上方消息历史中，直接引用即可"
                        "（重复读取的信息增益为 0）。\n"
                        "若需文件的其他部分，请调整 offset/limit；"
                        "若需定位特定内容，请用 grep 正则一次定位。"
                    ),
                    tool_call_id=tool_call_id,
                    name=tool_name,
                )

        # ── A4: hard budget ──
        used = 0
        if tool_name in _COUNTED_TOOLS:
            used = self._bucket(self._budget, dkey, 0)
            if used >= _hard_budget():
                logger.warning(
                    "治理拦截(预算耗尽): %s delegation=%s used=%d",
                    tool_name, dkey, used,
                )
                return ToolMessage(
                    content=(
                        f"⛔ [文件工具治理] 本次委派的文件读取预算已耗尽"
                        f"（{_hard_budget()} 次 read_file/grep）。"
                        "继续翻页的期望信息增益已低于调用成本。\n"
                        "请立即基于已获得的数据产出结论并返回；"
                        "如证据不足，在返回中明确说明还缺什么数据，"
                        "由 Coordinator 决策下一步。"
                    ),
                    tool_call_id=tool_call_id,
                    name=tool_name,
                )

        # ── A2: read-window boost on read-back paths ──
        if tool_name == "read_file" and any(
            path.startswith(p) for p in _READBACK_PREFIXES
        ):
            limit = tool_args.get("limit")
            if limit is None or (
                isinstance(limit, int) and limit <= _DEFAULT_PAGE_LIMIT
            ):
                boosted = dict(tool_args)
                boosted["limit"] = _read_window()
                try:
                    request.tool_call["args"] = boosted
                    logger.debug(
                        "治理(窗口提升): %s limit=%s→%d",
                        path, limit, _read_window(),
                    )
                except (TypeError, AttributeError, KeyError):
                    pass  # request immutable; proceed without boost

        # Mark seen / consume budget BEFORE execution so concurrent
        # duplicate calls in the same response are also caught.
        if tool_name in _READ_CLASS:
            seen.add(sig)
        if tool_name in _COUNTED_TOOLS:
            used += 1
            self._budget[dkey] = used

        result = await handler(request)

        # ── A4: soft budget hint appended to the result ──
        if (
            tool_name in _COUNTED_TOOLS
            and used > _soft_budget()
            and isinstance(result, ToolMessage)
        ):
            remaining = _hard_budget() - used
            note = (
                f"\n\n[系统提示] 文件读取预算：本次第 {used} 次，剩余 {remaining} 次。"
                "顺序翻页的边际信息增益随页数递减——建议改用 grep 正则一次性"
                "定位目标行，再按行号小窗口 read_file；预算耗尽后必须基于"
                "已有数据收尾。"
            )
            content = result.content if isinstance(result.content, str) else str(result.content)
            result = result.model_copy(update={"content": content + note})
        return result
