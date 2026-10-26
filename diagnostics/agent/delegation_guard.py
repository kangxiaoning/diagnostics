"""Coordinator-side delegation parameter guard (design document §8 G30).

The `task` tool is the Coordinator's only channel to a specialist, and every
one of its failure modes is silent rather than loud:

- an oversized argument payload fails to parse and reaches the subagent as
  ``args={}`` — measured 2026-10-22: the model pre-filled a whole expert
  conclusion (27k characters of a structured findings object) into the
  delegation, the arguments were dropped, and the expert ran for three
  minutes on an empty task before returning "all dimensions unavailable";
- an invented ``subagent_type`` dispatches nothing while the round still
  burns;
- a ``description`` that pastes a conclusion, a JSON schema or the session
  baseline consumes the expert's context before it can investigate.

Deterministic pre-dispatch validation turns all three into a tool error the
model can correct on its next turn.  The receipt is a positive recipe (what
to write instead), not a bare prohibition — the project principle is
"前置引导优先于反应式拦截": a block that only says "no" teaches nothing.

Scope: this guard reads only the tool-call arguments.  It never inspects the
model's prose, so it stays backend- and model-agnostic (no dependence on a
particular tool-call syntax).
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Awaitable, Callable, Sequence

from langchain.agents.middleware.types import AgentMiddleware
from langchain_core.messages import ToolMessage
from langgraph.prebuilt.tool_node import ToolCallRequest
from langgraph.types import Command

logger = logging.getLogger(__name__)

_TASK_TOOL = "task"

_ALLOWED_ARGS = ("subagent_type", "description")
"""The two parameters the delegation contract defines.  Anything else is a
model-invented key: it is stripped before dispatch (never forwarded to the
subagent) and its content is folded into `description` when it is a short
scalar, so the intent behind the invented key is not silently lost."""

_INSTRUCTION_ALIASES = frozenset({"prompt", "instructions", "task"})
"""Model conventions that carry the actual task instruction."""

_CONFIG_ONLY_KEYS = frozenset({
    "path", "skills", "mode", "system_prompt", "tools", "model",
    "middleware", "permissions", "response_format", "interrupt_on",
})
"""SubAgent definition-time fields; never valid as runtime task arguments."""

_BASELINE_MARKER = "## 诊断会话基线"
"""Opening line of the system-injected session baseline block."""

_INSTRUCTION_MARKER = "[Coordinator 委派指令]"
"""Separator the system uses when it wraps a delegation (ctx + this + desc)."""

_MIN_INSTRUCTION_CHARS = 50
"""Below this length a strip is treated as a mis-parse and skipped."""


def _strip_injected_context(description: str) -> str:
    """Drop a system-context block the Coordinator copied into a delegation.

    Every delegation the Coordinator sees in its history is stored in the
    wrapped form (system baseline + `[Coordinator 委派指令]` + the model's
    own instruction), so it sometimes reproduces that whole block in its
    next `task` call.  The copy is pure duplication: `awrap_tool_call`
    injects the block anyway (it skips injection only when the marker is
    already present), so stripping it loses nothing and keeps the delegation
    instruction measurable.

    Measured 2026-10-22 (scenario 32, kmc-expert): a 6613-character
    description whose bulk was the pasted baseline tripped the 3000-char
    contract and cost a full retry round — while the actual instruction was
    a few hundred characters.

    Literature: repetitive copying of input evidence is a documented
    long-context failure mode (GEAR, "Copy Less, Ground More"), and
    sanitising before validation is the standard order for handling
    model-produced arguments.
    """
    if not isinstance(description, str) or not description:
        return description

    if _INSTRUCTION_MARKER in description:
        tail = description.split(_INSTRUCTION_MARKER, 1)[1]
        tail = tail.lstrip("：: \n").strip()
        if len(tail) >= _MIN_INSTRUCTION_CHARS:
            return tail
        return description

    if _BASELINE_MARKER in description:
        start = description.index(_BASELINE_MARKER)
        rest = description[start:]
        body = len(_BASELINE_MARKER)
        # Preferred cut: the wrapper's own "---" separator, which closes the
        # whole injected block (baseline + entities + overrides + budget).
        cut = rest.find("\n---", body)
        if cut != -1:
            cleaned = (description[:start] + rest[cut + 4:]).strip()
        else:
            # No separator: drop only the first system section (conservative).
            cut = rest.find("\n## ", body)
            cleaned = (description[:start]
                       + (rest[cut:] if cut != -1 else "")).strip()
        if len(cleaned) >= _MIN_INSTRUCTION_CHARS:
            return cleaned
    return description


def _env_int(name: str, default: int) -> int:
    """Env var with safe fallback (operational knob, not a product API)."""
    try:
        return max(1, int(os.environ.get(name, "") or default))
    except (TypeError, ValueError):
        return default


# A delegation instruction is a few sentences of goal + signals.  The observed
# failure was three orders of magnitude larger, so a ceiling far above
# legitimate use still catches it, while never truncating a real instruction.
MAX_ARGS_BYTES = _env_int("DIAGNOSTICS_DELEGATION_MAX_ARGS_BYTES", 8192)
MAX_DESCRIPTION_CHARS = _env_int("DIAGNOSTICS_DELEGATION_MAX_DESC_CHARS", 3000)

_RECEIPT_EMPTY = (
    "委派被拒绝：task 参数为空或无法解析（参数可能在生成时被截断）。\n"
    "请用两个命名参数重发："
    "task(subagent_type=\"<专家名>\", description=\"<分析目标 + 要确认的信号>\")。\n"
    "description 聚焦分析目标与关键信号；专家返回格式由其自身指令定义。"
)

_RECEIPT_TOO_LARGE = (
    "委派被拒绝：参数共 {size} 字节，超过上限 {limit} 字节。\n"
    "description 只写「查什么 + 验证哪个假设 + 看哪个信号」；"
    "诊断参数（hostname/cluster_name/namespace/pod_name/时间窗口）由系统自动注入。\n"
    "结构化结论、证据原文、JSON 结构与会话基线由系统管理。"
)

_RECEIPT_DESC_TOO_LONG = (
    "委派被拒绝：description 长 {size} 字符，超过上限 {limit}。\n"
    "请压到几句话：目标、待验证的问题、需要确认的关键信号；其余上下文由系统注入。"
)

_RECEIPT_MISSING_TYPE = (
    "委派被拒绝：缺少 subagent_type。请从 task 工具说明列出的专家中选择一个。"
)

_RECEIPT_UNKNOWN_TYPE = (
    "委派被拒绝：未知的 subagent_type {given!r}。当前可委派的专家：{allowed}。"
)

_RECEIPT_MISSING_DESC = (
    "委派被拒绝：缺少 description。请写明分析目标与需要确认的信号。"
)

_RECEIPT_UNKNOWN_KEYS = (
    "本次 task 未执行：参数中包含未支持字段 {keys}。"
    "请用标准两参数重发：subagent_type=专家名，description=任务目标与关键信号；"
    "需要技能时在 description 写明技能名。"
)


def _payload_bytes(args: object) -> int:
    """UTF-8 size of the serialized arguments (0 when unserializable)."""
    try:
        return len(
            json.dumps(args, ensure_ascii=False, default=str).encode("utf-8"))
    except (TypeError, ValueError):
        return 0


class DelegationGuardMiddleware(AgentMiddleware):
    """Reject malformed ``task`` delegations before they reach a subagent."""

    def __init__(
        self,
        allowed_subagents: Sequence[str] | None = None,
        max_args_bytes: int = MAX_ARGS_BYTES,
        max_description_chars: int = MAX_DESCRIPTION_CHARS,
    ) -> None:
        super().__init__()
        self._allowed = tuple(allowed_subagents or ())
        self._max_bytes = max_args_bytes
        self._max_desc = max_description_chars

    # ── validation ──

    def _violation(self, tool_call: dict) -> str | None:
        args = tool_call.get("args")
        if not isinstance(args, dict) or not args:
            return _RECEIPT_EMPTY

        # Size first: an oversized payload is the failure that silently
        # degrades to an empty dispatch, and the fix ("stop pasting the
        # conclusion") is more useful than a per-field complaint.
        size = _payload_bytes(args)
        if size > self._max_bytes:
            return _RECEIPT_TOO_LARGE.format(size=size, limit=self._max_bytes)

        subagent = args.get("subagent_type")
        if not isinstance(subagent, str) or not subagent.strip():
            return _RECEIPT_MISSING_TYPE
        if self._allowed and subagent not in self._allowed:
            return _RECEIPT_UNKNOWN_TYPE.format(
                given=subagent, allowed=", ".join(self._allowed))

        description = args.get("description")
        if not isinstance(description, str) or not description.strip():
            return _RECEIPT_MISSING_DESC
        if len(description) > self._max_desc:
            return _RECEIPT_DESC_TOO_LONG.format(
                size=len(description), limit=self._max_desc)
        return None

    def _fold_unknown_keys(self, tool_call: dict) -> str | None:
        """Classify unknown keys without silently losing real instructions.

        - instruction aliases (``prompt``/``instructions``/``task``):
          append to description with neutral framing;
        - SubAgent definition fields (``path``/``skills``/``mode``...):
          drop, never merge (they are not runtime instructions);
        - anything else: return an actionable tool error so the model can
          resend with the two-field contract.

        2026-10-25 evidence: eight identical ``path=/agent_data/skills``
        occurrences showed the old "merge every short unknown value" policy
        both polluted the description and hid the invalid key from the model.
        deepagents>=0.7.29 now rejects unknown task keys itself; this guard
        must not recreate the older silent-drop behavior for long prompts.
        """
        args = tool_call.get("args")
        if not isinstance(args, dict) or not args:
            return None
        unknown = [key for key in args if key not in _ALLOWED_ARGS]
        if not unknown:
            return None

        description = args.get("description")
        description = description if isinstance(description, str) else ""
        merged: list[str] = []
        dropped: list[str] = []
        rejected: list[str] = []
        for key in unknown:
            value = args.pop(key)
            if key in _CONFIG_ONLY_KEYS:
                dropped.append(key)
                continue
            if key in _INSTRUCTION_ALIASES and isinstance(value, str):
                text = value.strip()
                if text:
                    if description:
                        description = (
                            f"{description}\n\n[补充任务指令]\n{text}")
                    else:
                        description = text
                    merged.append(key)
                else:
                    dropped.append(key)
                continue
            rejected.append(key)

        if merged:
            args["description"] = description
        tool_call["args"] = args
        logger.warning(
            "委派参数未知键已处理: aliases=%s dropped=%s rejected=%s "
            "(合法参数: %s)",
            merged, dropped, rejected, ", ".join(_ALLOWED_ARGS),
        )
        if rejected:
            return _RECEIPT_UNKNOWN_KEYS.format(keys=", ".join(rejected))
        return None

    def _normalize_description(self, tool_call: dict) -> None:
        """Strip a duplicated system-context block before measuring length.

        Order matters (sanitise → validate): the length contract is meant to
        bound the Coordinator's OWN instruction, not the transcript it
        echoed back.  When stripping empties the instruction below the
        guard's floor, the original text is kept so the length check still
        fires and the model gets a corrective receipt.
        """
        args = tool_call.get("args")
        if not isinstance(args, dict):
            return
        description = args.get("description")
        if not isinstance(description, str):
            return
        cleaned = _strip_injected_context(description)
        if cleaned == description:
            return
        args["description"] = cleaned
        tool_call["args"] = args
        logger.warning(
            "委派描述已剥离重复的系统上下文: %d → %d 字符",
            len(description), len(cleaned),
        )

    def _gate(self, request: ToolCallRequest) -> ToolMessage | None:
        tool_call = getattr(request, "tool_call", None) or {}
        if tool_call.get("name") != _TASK_TOOL:
            return None
        unknown_problem = self._fold_unknown_keys(tool_call)
        if unknown_problem is not None:
            return ToolMessage(
                content=unknown_problem,
                tool_call_id=tool_call.get("id", ""),
                name=_TASK_TOOL,
            )
        self._normalize_description(tool_call)
        problem = self._violation(tool_call)
        if problem is None:
            return None

        args = tool_call.get("args")
        args = args if isinstance(args, dict) else {}
        description = args.get("description")
        logger.warning(
            "委派参数拦截: subagent=%r args=%d bytes description=%d chars",
            args.get("subagent_type"),
            _payload_bytes(args),
            len(description) if isinstance(description, str) else 0,
        )
        return ToolMessage(
            content=problem,
            tool_call_id=tool_call.get("id", ""),
            name=_TASK_TOOL,
        )

    # ── middleware hooks ──

    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], ToolMessage | Command],
    ) -> ToolMessage | Command:
        blocked = self._gate(request)
        if blocked is not None:
            return blocked
        return handler(request)

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command]],
    ) -> ToolMessage | Command:
        blocked = self._gate(request)
        if blocked is not None:
            return blocked
        return await handler(request)
