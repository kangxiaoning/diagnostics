"""Tool registry — single import point for all agent tools."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from diagnostics.tools.gpu_tools import check_gpu_health, check_gpu_memory, check_gpu_utilization
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
        *extra_tools,
    ]
