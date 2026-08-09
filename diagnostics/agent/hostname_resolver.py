"""Hostname resolver — pre-resolution of K8s workload → host node names.

In container diagnosis scenarios, the frontend collects K8s-level parameters
(cluster_name, namespace, workload_type, workload_name, pod_name) but not
the underlying host node name.  However, ``host-argus-expert`` needs a
concrete hostname to call ``query_argus_cpu`` / ``query_argus_memory`` etc.

This module resolves the hostname **before** the diagnosis starts and
injects it into ``param_overrides`` so that host-level Argus tool calls
receive the concrete ``hostname``.

Mock mode uses the static pod→node mapping defined below (migrated here
when the scope_limit module was removed — see README).  Production mode
would query the K8s API to resolve pod → node mappings.
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)


# ── Mock pod→node→host mapping ──
# Migrated from the removed ``scope_limit`` module (2026-08-06): the scope
# guard middleware was removed because production scope should be naturally
# constrained by tool results, but hostname pre-resolution still needs this
# static data in mock mode.
# Key: "cluster_name:namespace:workload_name"
# Value: [(pod_name, node_name, host_ip), ...]
MOCK_POD_NODE_MAP: dict[str, list[tuple[str, str, str]]] = {
    # ── Test scenarios: default cluster ──
    "default:default:nginx-deployment": [
        ("nginx-deployment-7d8c9f6b4-abc12", "node-worker-1", "10.0.1.10"),
        ("nginx-deployment-7d8c9f6b4-def34", "node-worker-2", "10.0.1.11"),
        ("nginx-deployment-7d8c9f6b4-ghi56", "node-worker-3", "10.0.1.12"),
    ],

    # ── Test scenarios: prod-us-east cluster ──
    "prod-us-east:default:api-gateway": [
        ("api-gateway-6f8b7c9d5-xyz01", "worker-3", "10.0.2.10"),
        ("api-gateway-6f8b7c9d5-xyz02", "worker-5", "10.0.2.11"),
        ("api-gateway-6f8b7c9d5-xyz03", "worker-8", "10.0.2.12"),
    ],
    "prod-us-east:default:nginx-deployment": [
        ("nginx-deployment-7d8c9f6b4-abc12", "worker-1", "10.0.2.20"),
        ("nginx-deployment-7d8c9f6b4-def34", "worker-2", "10.0.2.21"),
    ],
    "prod-us-east:default:backend-svc": [
        ("backend-svc-5c8d4f6a2-pod01", "worker-1", "10.0.2.20"),
        ("backend-svc-5c8d4f6a2-pod02", "worker-2", "10.0.2.21"),
        ("backend-svc-5c8d4f6a2-pod03", "worker-4", "10.0.2.22"),
    ],
    "prod-us-east:default:java-app": [
        ("java-app-3b9f2d1e5-pod01", "worker-3", "10.0.2.10"),
        ("java-app-3b9f2d1e5-pod02", "worker-5", "10.0.2.11"),
    ],
    "prod-us-east:kube-system:coredns": [
        ("coredns-8f7b6c9d4-abc01", "worker-2", "10.0.2.21"),
        ("coredns-8f7b6c9d4-abc02", "worker-4", "10.0.2.22"),
        ("coredns-8f7b6c9d4-abc03", "worker-6", "10.0.2.23"),
    ],

    # ── Production scenario ──
    "pks-ehpc-1013-734964:sfe-default:eip-assistant": [
        ("eip-assistant-6f8b7c9d5-xyz01", "eklet-subnet-abc-001", "172.16.10.5"),
        ("eip-assistant-6f8b7c9d5-xyz02", "eklet-subnet-abc-002", "172.16.10.6"),
        ("eip-assistant-6f8b7c9d5-xyz03", "eklet-subnet-def-003", "172.16.20.7"),
    ],
}


def _resolve_mock(param_overrides: dict) -> dict:
    """Mock hostname resolution using the static pod-node mapping."""
    cluster_name = param_overrides.get("cluster_name", "")
    namespace = param_overrides.get("namespace", "")
    workload_name = param_overrides.get("workload_name", "")
    pod_name = param_overrides.get("pod_name", "")

    # ── Try exact workload lookup ──
    lookup_key = f"{cluster_name}:{namespace}:{workload_name}"
    pods = MOCK_POD_NODE_MAP.get(lookup_key, [])

    # ── Fallback: try generic default:default:<workload> ──
    if not pods and cluster_name and namespace:
        generic_key = f"default:default:{workload_name}"
        pods = MOCK_POD_NODE_MAP.get(generic_key, [])
        if pods:
            logger.info(
                "Hostname resolver: fallback %r → %r (found %d pods)",
                lookup_key, generic_key, len(pods),
            )

    if not pods:
        logger.warning(
            "Hostname resolver: no pods found for key=%r "
            "(cluster=%r namespace=%r workload=%r)",
            lookup_key, cluster_name, namespace, workload_name,
        )
        return param_overrides

    # ── Select hostname ──
    hostname = ""
    if pod_name:
        for pn, node, _ in pods:
            if pn == pod_name:
                hostname = node
                break
        if not hostname:
            logger.warning(
                "Hostname resolver: pod %r not in workload %r "
                "(available: %s), using first pod",
                pod_name, lookup_key,
                ", ".join(pn for pn, _, _ in pods),
            )

    if not hostname:
        hostname = pods[0][1]  # first pod's node name

    logger.info(
        "Hostname resolver: %s/%s/%s → hostname=%r (%d pods total)",
        cluster_name, namespace, workload_name, hostname, len(pods),
    )
    param_overrides["hostname"] = hostname
    return param_overrides


def _resolve_production(param_overrides: dict) -> dict:
    """Production hostname resolution via K8s API (not yet implemented).

    When implemented, this would:
    1. Query the K8s API for pods matching workload_name
    2. Extract nodeName from pod.spec
    3. Set hostname in param_overrides

    For now, falls through unchanged.
    """
    logger.debug("Hostname resolver: production mode — no K8s API integration yet")
    return param_overrides


def resolve_hostnames(param_overrides: dict | None) -> dict | None:
    """Pre-resolve host node names from K8s workload info.

    Called before ``build_agent()`` in the stream handler.  Only operates
    on container scenarios that are missing ``hostname`` in their
    ``param_overrides``.

    Returns the enriched ``param_overrides`` dict (mutated in-place for
    convenience, but the return value is the canonical reference).
    """
    if not param_overrides:
        return param_overrides

    # Only for container scenarios
    if param_overrides.get("task_type") != "container":
        return param_overrides

    # Don't override if hostname is already explicitly provided
    if param_overrides.get("hostname"):
        logger.debug(
            "Hostname resolver: hostname already set to %r, skipping",
            param_overrides["hostname"],
        )
        return param_overrides

    # Need at least cluster + workload to resolve
    if not param_overrides.get("cluster_name") or not param_overrides.get("workload_name"):
        logger.debug(
            "Hostname resolver: missing cluster_name or workload_name, skipping"
        )
        return param_overrides

    mode = os.getenv("DIAGNOSTICS_MODE", "mock").lower()
    if mode == "production":
        return _resolve_production(param_overrides)
    return _resolve_mock(param_overrides)
