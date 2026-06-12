"""Tool registry — single import point for all agent tools."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from diagnostics.tools.gpu_tools import check_gpu_health, check_gpu_memory, check_gpu_utilization
from diagnostics.tools.kubernetes_live_tools import (
    describe_pod,
    get_cluster_events,
    get_cluster_overview,
    get_node_info,
    get_node_resource_usage,
    get_pod_events,
    get_pod_logs,
    get_pod_resource_usage,
)
from diagnostics.tools.kubernetes_tools import check_kubernetes_control_plane, check_kubernetes_nodes, check_kubernetes_pods
from diagnostics.tools.network_tools import check_network
from diagnostics.tools.storage_tools import check_disk
from diagnostics.tools.system_tools import (
    check_cpu,
    check_memory,
    check_processes,
    get_system_overview,
    list_diagnostic_capabilities,
)


def get_agent_tools(extra_tools: Sequence[Any] = ()) -> list[Any]:
    """Return all diagnostic tools for the main agent."""
    return [
        get_system_overview,
        check_cpu,
        check_memory,
        check_disk,
        check_network,
        check_processes,
        check_gpu_health,
        check_gpu_memory,
        check_gpu_utilization,
        check_kubernetes_pods,
        check_kubernetes_nodes,
        check_kubernetes_control_plane,
        list_diagnostic_capabilities,
        # Live K8s tools
        get_cluster_overview,
        get_pod_logs,
        describe_pod,
        get_pod_events,
        get_node_info,
        get_cluster_events,
        get_pod_resource_usage,
        get_node_resource_usage,
        *extra_tools,
    ]


def get_k8s_live_tools() -> list[Any]:
    """Return only the live K8s tools (for subagent binding)."""
    return [
        get_cluster_overview,
        get_pod_logs,
        describe_pod,
        get_pod_events,
        get_node_info,
        get_cluster_events,
        get_pod_resource_usage,
        get_node_resource_usage,
    ]
