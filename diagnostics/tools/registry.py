"""Tool registry — production tools (live)."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from diagnostics.tools.live.kubernetes import (
    check_etcd_health,
    check_rbac_permissions,
    check_service_endpoints,
    check_webhook_status,
    check_certificate_expiry,
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
    get_resource_yaml,
    get_system_pods,
    list_clusters,
    list_helm_releases,
    list_namespace_resources,
)


def get_agent_tools(extra_tools: Sequence[Any] = ()) -> list[Any]:
    """Return production diagnostic tools (live K8s)."""
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
        *extra_tools,
    ]


def get_k8s_live_tools() -> list[Any]:
    """Return only the live K8s tools (for subagent binding)."""
    return get_agent_tools()
