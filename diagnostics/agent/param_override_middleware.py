"""Tool parameter override middleware — enforce correct tool call arguments.

When a diagnostic session starts with pre-known environment metadata, the LLM
may invent incorrect values for tool parameters or omit required ones.

This middleware operates in two modes based on parameter classification:

**Strict override** (session-immutable):
    container: cluster_name, start_time, end_time
    host:      name_chunk, start_time, end_time
    Always replaces LLM-provided values — these must never change during a
    diagnosis session.

    **Flexible injection → block on missing** (all other tool params):
        When LLM omits them: BLOCK the call (can't inject — multiple valid
        values exist, e.g. 10 pods, 3 nodes).  LLM must explicitly provide.
        When LLM provides: trusted (ScopeGuardMiddleware enforces range).

Only diagnostic tools are intercepted — scaffolding, ledger-management,
and delegation calls pass through unchanged.

Usage (in factory.py):

    config = {
        "task_type": "container",
        "cluster_name": "pks-ehpc-1013-734964",
        "fault_time_range": {
            "start_time": "2026-06-30 14:15:00",
            "end_time": "2026-06-30 14:30:00",
        },
    }
    mw = ToolParamOverrideMiddleware(config=config, tools=all_tools)
    create_deep_agent(..., middleware=[..., mw])
"""

from __future__ import annotations

import inspect
import logging
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

from langchain.agents.middleware.types import AgentMiddleware
from langchain_core.messages import ToolMessage
from langgraph.prebuilt.tool_node import ToolCallRequest
from langgraph.types import Command

logger = logging.getLogger(__name__)


def _flatten(d: dict, max_depth: int = 1) -> dict:
    """Flatten nested dicts up to *max_depth* for direct key matching.

    >>> _flatten({"a": 1, "b": {"c": 2}})
    {"a": 1, "b": {"c": 2}, "c": 2}

    Nested values win over top-level keys with the same name.
    """
    result: dict = {}
    for k, v in d.items():
        result[k] = v
        if isinstance(v, dict) and max_depth > 0:
            result.update(_flatten(v, max_depth - 1))
    return result


def _build_param_map(tools: Sequence[Any]) -> dict[str, set[str]]:
    """Extract parameter names from tool function signatures.

    Returns dict: tool_name → set of parameter names.
    Used by the middleware to decide whether a config key is a valid
    injection target for a given tool.
    """
    param_map: dict[str, set[str]] = {}
    for t in tools:
        name = getattr(t, "name", "")
        if not name:
            continue
        fn = getattr(t, "func", None) or getattr(t, "coroutine", None)
        if fn is None:
            continue
        try:
            params = set(inspect.signature(fn).parameters.keys())
        except (ValueError, TypeError):
            params = set()
        param_map[name] = params
    return param_map


# Tools that should NOT receive parameter overrides — scaffolding,
# ledger-management, and delegation tools.
_DIAGNOSTIC_ONLY_SKIP = frozenset({
    # Scaffolding (deepagents built-ins)
    "write_file", "read_file", "edit_file",
    "write_todos", "read_todos",
    "ls", "glob", "grep",
    # Ledger management
    "commit_hypotheses", "select_path", "record_finding", "backtrack",
    # Delegation (args are LLM-generated instructions, not diagnostic params)
    "task",
})


class ToolParamOverrideMiddleware(AgentMiddleware):
    """Replace tool call arguments with pre-configured session values.

    Parameters are classified:

    **Strict** — always override; inject when missing:
        container: cluster_name, start_time, end_time
        host:      name_chunk, start_time, end_time

    **Flexible** — trusted when LLM provides; BLOCKED when missing:
        namespace, pod_name, node_name, and all other tool params.
        Cannot inject because multiple valid values may exist.

    (ScopeGuardMiddleware enforces resource boundaries downstream.)

    Only diagnostic tools are intercepted — scaffolding (read_file,
    write_file, …), ledger-management (commit_hypotheses, …), and
    delegation (task) calls pass through unchanged.
    """

    # Config keys that describe the session but are NOT tool parameter names.
    _META_KEYS = frozenset({"task_type", "fault_description", "fault_time_range"})

    # Params that are ALWAYS overridden — session-immutable, LLM must never change.
    # Scope-specific: container → cluster identity + time; host → host identity + time.
    _STRICT_CONTAINER = frozenset({"cluster_name", "start_time", "end_time"})
    _STRICT_HOST = frozenset({"name_chunk", "start_time", "end_time"})

    def __init__(
        self,
        config: dict | None = None,
        tools: Sequence[Any] | None = None,
    ) -> None:
        self._config = config or {}
        self._flat_config = _flatten(self._config)
        self._param_map = _build_param_map(tools or [])
        logger.info(
            "ParamOverride MW: %d top-level keys, %d flat keys, %d tools",
            len(self._config), len(self._flat_config), len(self._param_map),
        )

    @property
    def config(self) -> dict:
        return self._config

    def update_config(self, config: dict) -> None:
        """Replace the session config (e.g. per-request)."""
        self._config = config
        self._flat_config = _flatten(config)
        logger.info(
            "ParamOverride MW: config updated — %d flat keys",
            len(self._flat_config),
        )

    # ── Tool call interception ─────────────────────────────────────────

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command]],
    ) -> ToolMessage | Command:
        tool_name = request.tool_call.get("name", "")
        tool_args: dict = request.tool_call.get("args", {})
        tool_call_id = request.tool_call.get("id", "")

        # Only intercept diagnostic tools.
        if tool_name in _DIAGNOSTIC_ONLY_SKIP or not self._flat_config:
            return await handler(request)

        changes: list[str] = []
        valid_params = self._param_map.get(tool_name)

        # Determine strict-override set from task type.
        task_type = self._config.get("task_type", "")
        strict_set = (
            self._STRICT_CONTAINER if task_type == "container"
            else self._STRICT_HOST if task_type == "host"
            else frozenset()
        )

        for param_name, param_value in self._flat_config.items():
            if param_name in self._META_KEYS:
                continue  # meta key, not a tool parameter

            # ── Multi-value params (lists) skipped — handled as LLM context ──
            if isinstance(param_value, (list, tuple, set)):
                continue

            if param_name in tool_args:
                if param_name in strict_set:
                    # ── Strict: always override — LLM must not change ──
                    if tool_args[param_name] != param_value:
                        tool_args[param_name] = param_value
                        changes.append(f"{param_name}={param_value!r}")
                # else: Flexible — LLM-provided value is trusted (may have
                #        discovered new targets).  ScopeGuard enforces range.
            elif valid_params and param_name in valid_params:
                if param_name in strict_set:
                    # ── Strict: inject — LLM omitted an immutable param ──
                    tool_args[param_name] = param_value
                    changes.append(f"+{param_name}={param_value!r}")
                else:
                    # ── Flexible missing → block: can't guess which value
                    #     to inject (e.g. which pod among dozens).
                    logger.warning(
                        "ParamOverride BLOCKED: %s missing %s",
                        tool_name, param_name,
                    )
                    return ToolMessage(
                        content=(
                            f"⛔ [参数拦截] {tool_name} 缺少必要参数 {param_name}，"
                            f"系统无法自动填入（存在多个可选值）。"
                            f"请在调用时明确指定 {param_name} 参数。"
                        ),
                        tool_call_id=tool_call_id,
                        name=tool_name,
                    )
            # else: param not in tool_args AND not in tool signature → skip

        if changes:
            logger.info(
                "ParamOverride: %s → %s",
                tool_name, ", ".join(changes),
            )
            try:
                request.tool_call["args"] = tool_args
            except (TypeError, AttributeError):
                pass

        return await handler(request)
