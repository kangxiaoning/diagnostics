"""Tool registry — single import point for all agent tools."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from diagnostics.tools.gpu_tools import check_gpu_health, check_gpu_memory, check_gpu_utilization
from diagnostics.tools.kubernetes_live_tools import (
    check_service_endpoints,
    describe_controller,
    describe_coredns,
    describe_pod,
    explain_resource,
    get_api_resources,
    get_api_versions,
    get_cluster_events,
    get_cluster_overview,
    get_configmap,
    get_coredns_logs,
    get_helm_release_history,
    get_helm_release_values,
    get_ingress_status,
    get_namespaces,
    get_node_info,
    get_node_resource_usage,
    get_pod_events,
    get_pod_logs,
    get_pod_logs_lines,
    get_pod_logs_since,
    get_pod_previous_logs,
    get_pod_resource_usage,
    get_pv_pvc_status,
    get_resource_yaml,
    get_system_pods,
    list_clusters,
    list_helm_releases,
    list_namespace_resources,
    check_rbac_permissions,
    get_network_policies,
    get_node_conditions,
    get_pod_restart_counts,
    check_certificate_expiry,
    check_webhook_status,
    check_etcd_health,
    get_etcd_logs,
    get_etcd_metrics,
    get_etcd_status,
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
        get_system_overview, check_cpu, check_memory, check_disk, check_network,
        check_processes, check_gpu_health, check_gpu_memory, check_gpu_utilization,
        check_kubernetes_pods, check_kubernetes_nodes, check_kubernetes_control_plane,
        list_diagnostic_capabilities,
        # Live K8s tools
        list_clusters, get_namespaces, get_cluster_overview,
        get_pod_logs, get_pod_logs_since, get_pod_logs_lines, get_pod_previous_logs,
        describe_pod, get_pod_events, get_node_info, get_cluster_events,
        get_pod_resource_usage, get_node_resource_usage,
        get_api_resources, get_api_versions, get_resource_yaml, explain_resource,
        get_system_pods, describe_controller, check_service_endpoints,
        get_configmap, list_namespace_resources, get_pv_pvc_status, get_ingress_status,
        get_coredns_logs, describe_coredns,
        list_helm_releases, get_helm_release_history, get_helm_release_values,
        get_node_conditions, get_network_policies, check_rbac_permissions,
        get_pod_restart_counts,
        check_certificate_expiry, check_webhook_status,
        get_etcd_status, get_etcd_logs, check_etcd_health, get_etcd_metrics,
        *extra_tools,
    ]


def get_k8s_live_tools() -> list[Any]:
    """Return only the live K8s tools (for subagent binding)."""
    return [
        list_clusters, get_namespaces, get_cluster_overview,
        get_pod_logs, get_pod_logs_since, get_pod_logs_lines, get_pod_previous_logs,
        describe_pod, get_pod_events, get_node_info, get_cluster_events,
        get_pod_resource_usage, get_node_resource_usage,
        get_api_resources, get_api_versions, get_resource_yaml, explain_resource,
        get_system_pods, describe_controller, check_service_endpoints,
        get_configmap, list_namespace_resources, get_pv_pvc_status, get_ingress_status,
        get_coredns_logs, describe_coredns,
        list_helm_releases, get_helm_release_history, get_helm_release_values,
        get_node_conditions, get_network_policies, check_rbac_permissions,
        get_pod_restart_counts,
        check_certificate_expiry, check_webhook_status,
        get_etcd_status, get_etcd_logs, check_etcd_health, get_etcd_metrics,
    ]
