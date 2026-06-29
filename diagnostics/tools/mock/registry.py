"""Mock tool registry — scenario-driven diagnostic tools for local testing."""

from collections.abc import Sequence
from typing import Any

from diagnostics.tools.mock.argus import (
    query_argus_cpu,
    query_argus_disk,
    query_argus_kubernetes,
    query_argus_memory,
    query_argus_network,
)
from diagnostics.tools.mock.gpu import check_gpu_health, check_gpu_memory, check_gpu_utilization
from diagnostics.tools.mock.hosts import (
    list_diagnostic_capabilities,
)
from diagnostics.tools.mock.kubernetes import (
    check_certificate_expiry,
    check_etcd_health,
    check_kubernetes_control_plane,
    check_kubernetes_nodes,
    check_kubernetes_pods,
    check_rbac_permissions,
    check_service_endpoints,
    check_webhook_status,
    describe_coredns,
    describe_pod,
    get_cluster_events,
    get_cluster_overview,
    get_configmap,
    get_coredns_logs,
    get_etcd_logs,
    get_etcd_metrics,
    get_etcd_status,
    get_helm_release_history,
    get_helm_release_values,
    get_ingress_status,
    get_namespaces,
    get_network_policies,
    get_node_conditions,
    get_node_info,
    get_node_resource_usage,
    get_pod_events,
    get_pod_logs,
    get_pod_logs_lines,
    get_pod_logs_since,
    get_pod_previous_logs,
    get_pod_resource_usage,
    get_pod_restart_counts,
    get_pv_pvc_status,
    get_system_pods,
    list_helm_releases,
    list_namespace_resources,
)


def get_mock_tools(extra_tools: Sequence[Any] = ()) -> list[Any]:
    """Return all mock diagnostic tools (scenario-driven — legacy, use get_coordinator_mock_tools)."""
    return [
        # ── GPU diagnostics ──
        check_gpu_health,
        check_gpu_memory,
        check_gpu_utilization,
        # ── Kubernetes diagnostics ──
        check_kubernetes_pods,
        check_kubernetes_nodes,
        check_kubernetes_control_plane,
        get_namespaces,
        get_cluster_overview,
        get_pod_logs,
        get_pod_logs_since,
        get_pod_logs_lines,
        get_pod_previous_logs,
        describe_pod,
        get_pod_events,
        get_cluster_events,
        get_node_info,
        get_node_conditions,
        get_pod_resource_usage,
        get_node_resource_usage,
        get_system_pods,
        get_coredns_logs,
        describe_coredns,
        list_helm_releases,
        get_helm_release_history,
        get_helm_release_values,
        get_network_policies,
        check_rbac_permissions,
        get_pod_restart_counts,
        check_certificate_expiry,
        check_webhook_status,
        get_etcd_status,
        get_etcd_logs,
        check_etcd_health,
        get_etcd_metrics,
        check_service_endpoints,
        get_configmap,
        list_namespace_resources,
        get_pv_pvc_status,
        get_ingress_status,
        # ── Meta ──
        list_diagnostic_capabilities,
        *extra_tools,
    ]


def get_coordinator_mock_tools(extra_tools: Sequence[Any] = ()) -> list[Any]:
    """Return Coordinator-only tools — GPU + meta (no Argus, no deep diagnostics).

    Argus monitoring tools are now EXCLUSIVELY available to Argus experts
    (host-argus-expert / k8s-argus-expert). Coordinator delegates to them
    in the UNDERSTAND phase instead of querying Argus directly. This keeps
    Coordinator's context lean and lets Argus experts perform focused
    time-series correlation analysis.
    """
    return [
        # ── GPU diagnostics ──
        check_gpu_health,
        check_gpu_memory,
        check_gpu_utilization,
        # ── Meta ──
        list_diagnostic_capabilities,
        *extra_tools,
    ]


def get_host_argus_tools() -> list[Any]:
    """Return host-level Argus monitoring tools (for host-argus-expert).

    Covers CPU / memory / disk / network 1min-granularity metrics so the
    expert can perform cross-subsystem time-series correlation analysis.
    """
    return [
        query_argus_cpu,
        query_argus_memory,
        query_argus_disk,
        query_argus_network,
    ]


def get_k8s_argus_tools() -> list[Any]:
    """Return K8s-level Argus monitoring tools (for k8s-argus-expert).

    Only the K8s cluster metrics tool — host-level metrics (CPU/memory/disk/
    network) are exclusively owned by host-argus-expert to avoid duplicate
    queries when Coordinator delegates to both experts in parallel.
    """
    return [
        query_argus_kubernetes,
    ]


def get_k8s_mock_tools() -> list[Any]:
    """Return only K8s-specific mock tools (for subagent injection)."""
    return [
        get_namespaces,
        get_cluster_overview,
        get_pod_logs,
        get_pod_logs_since,
        get_pod_logs_lines,
        get_pod_previous_logs,
        describe_pod,
        get_pod_events,
        get_cluster_events,
        get_node_info,
        get_node_conditions,
        get_pod_resource_usage,
        get_node_resource_usage,
        get_system_pods,
        get_coredns_logs,
        describe_coredns,
        list_helm_releases,
        get_helm_release_history,
        get_helm_release_values,
        get_network_policies,
        check_rbac_permissions,
        get_pod_restart_counts,
        check_certificate_expiry,
        check_webhook_status,
        get_etcd_status,
        get_etcd_logs,
        check_etcd_health,
        get_etcd_metrics,
        check_service_endpoints,
        get_configmap,
        list_namespace_resources,
        get_pv_pvc_status,
        get_ingress_status,
        check_kubernetes_pods,
        check_kubernetes_nodes,
        check_kubernetes_control_plane,
    ]
