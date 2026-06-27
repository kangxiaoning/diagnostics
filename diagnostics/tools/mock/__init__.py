"""Mock diagnostic tools — scenario-driven for local testing and development.

Directory:
    hosts.py       — Host-level tools (CPU, memory, disk, network, processes)
    gpu.py         — GPU tools (health, memory, utilization)
    kubernetes.py  — Kubernetes tools (pods, nodes, etcd, CoreDNS, Helm, etc.)
    data.py        — Scenario data factory
    scenarios.py   — Scenario definitions and state management
    registry.py    — Tool aggregation for agent injection
"""

from diagnostics.tools.mock.registry import get_coordinator_mock_tools, get_k8s_mock_tools, get_mock_tools

__all__ = ["get_coordinator_mock_tools", "get_mock_tools", "get_k8s_mock_tools"]
