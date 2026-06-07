"""Kubernetes diagnostic tools: pods and nodes."""

from langchain_core.tools import tool

from diagnostics.tools.data import (
    kubernetes_pods_data,
    kubernetes_nodes_data,
)
from diagnostics.tools.scenarios import _active_scenario


@tool
def check_kubernetes_pods(namespace: str = "default") -> str:
    """Check Kubernetes pod statuses, restart counts, and OOM events.

    Args:
        namespace: Kubernetes namespace (default: "default")
    """
    pods = kubernetes_pods_data(_active_scenario)
    return f"Namespace: {namespace}\n{pods}"


@tool
def check_kubernetes_nodes() -> str:
    """Check Kubernetes node statuses, resource usage, and conditions (MemoryPressure, etc.)."""
    return kubernetes_nodes_data(_active_scenario)
