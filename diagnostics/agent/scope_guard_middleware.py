"""Scope guard middleware — enforce diagnostic resource boundaries.

Prevents tool calls from accessing Kubernetes resources (nodes, pods,
namespaces) outside the pre-discovered diagnostic scope.  This is the
hard enforcement layer that blocks LLM-generated tool calls targeting
unrelated parts of the cluster.

Only activates when a :class:`ScopeLimit` is provided.  Without it,
all tool calls pass through unchanged (backward compatible).
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

from langchain.agents.middleware.types import AgentMiddleware
from langchain_core.messages import ToolMessage
from langgraph.prebuilt.tool_node import ToolCallRequest
from langgraph.types import Command

from diagnostics.agent.scope_limit import ScopeLimit

logger = logging.getLogger(__name__)

# ── Tools that accept node-level parameters (exact match) ──
_NODE_NAME_TOOLS = frozenset({
    "get_node_info",
})

# ── Tools that accept namespace — always checked ──
_NAMESPACE_TOOLS = frozenset({
    "check_kubernetes_pods",
    "get_pod_logs",
    "get_pod_events",
    "list_deployments",
    "list_services",
    "get_node_info",
    "get_namespaces",
})

# ── Pod-name tools (exact match, not name_chunk) ──
_POD_NAME_TOOLS = frozenset({
    "get_pod_logs",
    "get_pod_events",
})

_BLOCK_TEMPLATE = (
    "⛔ [范围守卫] {tool_name} 的 {param_name} 参数值不在诊断范围内。\n"
    "- 提供的值: {provided}\n"
    "- 允许的值: {allowed}\n"
    "- 请基于已有范围内数据分析，不要查询无关资源。"
)


def _is_in_scope(value: str, allowed: list[str]) -> bool:
    """Check if *value* matches any entry in *allowed*.

    Exact-match first, then substring (for name_chunk-style params).
    """
    if not value or not allowed:
        return True  # empty value or no restrictions → pass
    if value in allowed:
        return True
    # Substring match: "node-1" matches "eklet-node-1-abc"
    for a in allowed:
        if value in a or a in value:
            return True
    return False


class ScopeGuardMiddleware(AgentMiddleware):
    """Block tool calls targeting resources outside the diagnostic scope.

    Checks namespace, node_name, and pod_name parameters against the
    :class:`ScopeLimit` allow-lists.  Tools without these parameters
    pass through unchanged.

    Must be placed AFTER ``ToolParamOverrideMiddleware`` in the
    pipeline so it validates the final (corrected) arguments.
    """

    def __init__(self, scope: ScopeLimit | None = None) -> None:
        self._scope = scope
        if scope:
            logger.info(
                "ScopeGuard: ns=%s nodes=%d pods=%d hosts=%d",
                scope.allowed_namespace,
                len(scope.allowed_nodename),
                len(scope.allowed_podname),
                len(scope.allowed_hostname),
            )

    # ── Tool call interception ─────────────────────────────────────

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command]],
    ) -> ToolMessage | Command:
        scope = self._scope
        if scope is None:
            return await handler(request)

        tool_name = request.tool_call.get("name", "")
        tool_args: dict = request.tool_call.get("args", {})
        tool_call_id = request.tool_call.get("id", "")

        # ── Namespace check ──
        if tool_name in _NAMESPACE_TOOLS:
            ns = tool_args.get("namespace", "")
            if ns and ns != scope.allowed_namespace:
                logger.warning(
                    "ScopeGuard BLOCKED: %s namespace=%r (allowed=%r)",
                    tool_name, ns, scope.allowed_namespace,
                )
                return ToolMessage(
                    content=_BLOCK_TEMPLATE.format(
                        tool_name=tool_name,
                        param_name="namespace",
                        provided=repr(ns),
                        allowed=repr(scope.allowed_namespace),
                    ),
                    tool_call_id=tool_call_id,
                    name=tool_name,
                )

        # ── Node name check ──
        if tool_name in _NODE_NAME_TOOLS and scope.allowed_nodename:
            node = tool_args.get("node_name", "")
            if node and not _is_in_scope(node, scope.allowed_nodename):
                logger.warning(
                    "ScopeGuard BLOCKED: %s node_name=%r (allowed=%s)",
                    tool_name, node, scope.allowed_nodename,
                )
                return ToolMessage(
                    content=_BLOCK_TEMPLATE.format(
                        tool_name=tool_name,
                        param_name="node_name",
                        provided=repr(node),
                        allowed=repr(scope.allowed_nodename),
                    ),
                    tool_call_id=tool_call_id,
                    name=tool_name,
                )

        # ── Pod name check ──
        if tool_name in _POD_NAME_TOOLS and scope.allowed_podname:
            pod = tool_args.get("pod_name", "")
            if pod and not _is_in_scope(pod, scope.allowed_podname):
                logger.warning(
                    "ScopeGuard BLOCKED: %s pod_name=%r (allowed=%d pods)",
                    tool_name, pod, len(scope.allowed_podname),
                )
                return ToolMessage(
                    content=_BLOCK_TEMPLATE.format(
                        tool_name=tool_name,
                        param_name="pod_name",
                        provided=repr(pod),
                        allowed=repr(scope.allowed_podname[:5]),
                    ),
                    tool_call_id=tool_call_id,
                    name=tool_name,
                )

        return await handler(request)
