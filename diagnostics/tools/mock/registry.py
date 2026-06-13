"""Mock tool registry — scenario-driven diagnostic tools for local testing."""

from collections.abc import Sequence
from typing import Any

from diagnostics.tools.mock.gpu_tools import check_gpu_health, check_gpu_memory, check_gpu_utilization
from diagnostics.tools.mock.kubernetes_tools import check_kubernetes_control_plane, check_kubernetes_nodes, check_kubernetes_pods
from diagnostics.tools.mock.network_tools import check_network
from diagnostics.tools.mock.storage_tools import check_disk
from diagnostics.tools.mock.system_tools import (
    check_cpu,
    check_memory,
    check_processes,
    get_system_overview,
    list_diagnostic_capabilities,
)


def get_mock_tools(extra_tools: Sequence[Any] = ()) -> list[Any]:
    """Return all mock diagnostic tools (scenario-driven)."""
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
