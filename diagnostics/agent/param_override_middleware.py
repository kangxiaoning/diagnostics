"""Tool parameter override middleware — enforce correct tool call arguments.

When a diagnostic session starts with pre-known environment metadata
(cluster name, namespace, pod name, host name, time range), the LLM
may invent incorrect values for tool parameters.  This middleware
intercepts every tool call — for both Coordinator and subagents — and
replaces any parameter whose name matches a key in the session config.

Usage (in factory.py or app.py):

    config = {
        "cluster_name": "pks-ehpc-1013-734964",
        "namespace": "sfe-default",
        "fault_time_range": {
            "start_time": "2026-06-30 14:15:00",
            "end_time": "2026-06-30 14:30:00",
        },
    }
    override_mw = ToolParamOverrideMiddleware(config=config)
    create_deep_agent(..., middleware=[..., override_mw])
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

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


class ToolParamOverrideMiddleware(AgentMiddleware):
    """Replace tool call arguments with pre-configured session values.

    Every tool call — including those made inside subagents — passes
    through ``awrap_tool_call``.  If a parameter name matches a key
    in the flat session config, its value is silently overridden.

    Typical config shapes:

    **Kubernetes / container scenario**:

        {
            "task_type": "container",
            "cluster_name": "pks-ehpc-1013-734964",
            "namespace": "sfe-default",
            "pod_name": "eip-assistant-xxx",
            "fault_time_range": {
                "start_time": "2026-06-30 14:15:00",
                "end_time": "2026-06-30 14:30:00",
            },
        }

    **Host scenario**:

        {
            "task_type": "host",
            "name_chunk": "CQX-L0150660",
            "fault_time_range": {
                "start_time": "2026-07-01 18:10:17",
                "end_time": "2026-07-01 18:17:17",
            },
        }
    """

    def __init__(self, config: dict | None = None) -> None:
        self._config = config or {}
        self._flat_config = _flatten(self._config)
        logger.info(
            "ParamOverride MW: %d top-level keys, %d flat keys",
            len(self._config), len(self._flat_config),
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

    # Config keys that describe the session but are NOT tool parameter names.
    # They are excluded from both injection and override to avoid polluting
    # tool calls with irrelevant keys.
    _META_KEYS = frozenset({"task_type", "fault_description", "fault_time_range"})

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command]],
    ) -> ToolMessage | Command:
        tool_name = request.tool_call.get("name", "")
        tool_args: dict = request.tool_call.get("args", {})

        if not self._flat_config:
            return await handler(request)

        changes: list[str] = []

        for param_name, param_value in self._flat_config.items():
            if param_name in self._META_KEYS:
                continue  # meta key, not a tool parameter
            if param_name in tool_args:
                # ── Override: replace incorrect value ──
                if tool_args[param_name] != param_value:
                    tool_args[param_name] = param_value
                    changes.append(f"{param_name}={param_value!r}")
            else:
                # ── Inject: parameter missing from LLM call ──
                tool_args[param_name] = param_value
                changes.append(f"+{param_name}={param_value!r}")

        if changes:
            logger.info(
                "ParamOverride: %s → %s",
                tool_name, ", ".join(changes),
            )
            try:
                request.tool_call["args"] = tool_args
            except (TypeError, AttributeError):
                pass  # request is immutable; proceed without mutation

        return await handler(request)
