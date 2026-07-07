"""Hostname resolver — pre-resolution of K8s workload → host node names.

In container diagnosis scenarios, the frontend collects K8s-level parameters
(cluster_name, namespace, workload_type, workload_name, pod_name) but not
the underlying host node name.  However, ``host-argus-expert`` needs a
concrete hostname to call ``query_argus_cpu`` / ``query_argus_memory`` etc.

This module resolves the hostname **before** the diagnosis starts and
injects it into ``param_overrides`` so that ``ToolParamOverrideMiddleware``
can unconditionally overwrite ``hostname`` in host-level Argus tool calls.

Mock mode uses the same static mapping as ``ScopeLimit``.
Production mode would query the K8s API to resolve pod → node mappings.
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)


def _resolve_mock(param_overrides: dict) -> dict:
    """Mock hostname resolution using the static pod-node mapping.

    Imports from ``ScopeLimit.MOCK_POD_NODE_MAP`` at call time to avoid
    circular imports at module load time.
    """
    from diagnostics.agent.scope_limit import MOCK_POD_NODE_MAP

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
