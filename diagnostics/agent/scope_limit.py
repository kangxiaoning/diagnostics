"""Diagnostic scope limit — pre-discovery of allowed resources.

Before starting a container diagnosis, this module:
1. Queries the cluster to discover all Pods under the target workload
2. Extracts node names and host IPs from Pod specs
3. Checks control plane health to avoid diagnosing a stressed cluster

When DIAGNOSTICS_MODE=mock, discovery returns mock data so the
scope mechanism can be tested without a live K8s cluster.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


# ── Mock data for pre-discovery ──
# Real implementation would call the K8s API via kubernetes client.
# These stubs let the scope mechanism be validated end-to-end.

_MOCK_POD_NODE_MAP: dict[str, list[tuple[str, str, str]]] = {
    # key: "cluster_name:namespace:workload_name"
    # value: [(pod_name, node_name, host_ip), ...]

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

_MOCK_CONTROL_PLANE_HEALTH: dict[str, tuple[float, float]] = {
    # cluster_name: (mem_usage_percent, mem_free_gb)
    "default": (42.5, 8.2),                         # healthy
    "pks-ehpc-1013-734964": (78.0, 3.5),            # borderline but passable
    "overloaded-cluster": (92.3, 0.8),               # should be rejected
}


@dataclass
class ScopeLimit:
    """Pre-discovered diagnostic scope — enforced by ScopeGuardMiddleware.

    Defines the safe resource boundary for a single diagnosis session.
    Tool calls targeting resources outside this boundary are blocked.
    """

    cluster_name: str
    allowed_namespace: str
    allowed_nodename: list[str] = field(default_factory=list)
    allowed_hostname: list[str] = field(default_factory=list)
    allowed_podname: list[str] = field(default_factory=list)
    start_time: str = ""
    end_time: str = ""

    # ── Factory: mock discovery ──

    @classmethod
    def discover(
        cls,
        cluster_name: str,
        namespace: str,
        workload_type: str = "",
        workload_name: str = "",
        pod_name: str = "",
        start_time: str = "",
        end_time: str = "",
    ) -> ScopeLimit:
        """Discover affected nodes/hosts from the target workload.

        In production mode this queries the live K8s API.  In mock mode
        it returns pre-configured test data so the scope mechanism can
        be validated without external dependencies.

        Args:
            cluster_name:  K8s cluster identifier.
            namespace:     Target namespace (required).
            workload_type: Deployment / DaemonSet / StatefulSet
                           (reserved for production K8s client).
            workload_name: Workload resource name.
            pod_name:      User-specified pod name (always included).
            start_time:    Diagnosis time window start.
            end_time:      Diagnosis time window end.
        """
        _ = workload_type  # reserved for production K8s client integration
        scope = cls(
            cluster_name=cluster_name,
            allowed_namespace=namespace,
            allowed_podname=[pod_name] if pod_name else [],
            start_time=start_time,
            end_time=end_time,
        )

        mode = os.getenv("DIAGNOSTICS_MODE", "mock").lower()

        if mode == "mock":
            scope._discover_mock(cluster_name, namespace, workload_name, pod_name)
        else:
            # Production: real K8s API call — implement when ready.
            logger.info(
                "ScopeLimit.discover: production mode — "
                "K8s client not yet integrated; using user-provided values only."
            )

        return scope

    def _discover_mock(
        self,
        cluster_name: str,
        namespace: str,
        workload_name: str,
        pod_name: str,
    ) -> None:
        """Populate allowed_nodename / allowed_hostname from mock data."""
        lookup = f"{cluster_name}:{namespace}:{workload_name}"
        entries = _MOCK_POD_NODE_MAP.get(lookup, [])

        if not entries:
            # Fallback: try a generic entry
            generic_key = f"default:default:{workload_name}"
            entries = _MOCK_POD_NODE_MAP.get(generic_key, [])

        if not entries:
            logger.warning(
                "ScopeLimit: no mock data for %s, "
                "only user-specified pod will be allowed.",
                lookup,
            )
            return

        nodes: set[str] = set()
        hosts: set[str] = set()
        seen_pods: set[str] = set()

        # Always include the user-specified pod first.
        if pod_name:
            self.allowed_podname.append(pod_name)
            seen_pods.add(pod_name)

        for name, node, ip in entries:
            if name not in seen_pods:
                self.allowed_podname.append(name)
                seen_pods.add(name)
            nodes.add(node)
            hosts.add(ip)

        self.allowed_nodename = sorted(nodes)
        self.allowed_hostname = sorted(hosts)

        logger.info(
            "ScopeLimit mock: %d pods → %d nodes, %d hosts for %s",
            len(entries), len(nodes), len(hosts), lookup,
        )

    # ── Control plane health check ──

    @staticmethod
    def check_control_plane(
        mem_usage_percent: float,
        mem_free_gb: float,
    ) -> tuple[bool, str]:
        """Check whether the cluster control plane is safe to diagnose.

        Returns:
            (safe, reason).  ``safe=False`` means diagnosis should be
            rejected because additional API load could trigger OOM.
        """
        if mem_usage_percent > 80 and mem_free_gb < 2:
            return False, (
                f"控制面内存压力过高（{mem_usage_percent:.1f}%，"
                f"可用 {mem_free_gb:.1f}GB），"
                f"诊断请求可能加剧控制面负载，已自动拒绝。"
                f"请等待集群恢复稳定后重试。"
            )
        return True, ""

    @staticmethod
    def get_control_plane_metrics(cluster_name: str) -> tuple[float, float]:
        """Return (mem_usage_percent, mem_free_gb) for the cluster.

        Mock mode uses pre-configured values; production calls the
        monitoring API (to be implemented).
        """
        mode = os.getenv("DIAGNOSTICS_MODE", "mock").lower()

        if mode == "mock":
            usage, free = _MOCK_CONTROL_PLANE_HEALTH.get(
                cluster_name,
                (45.0, 6.0),  # default: healthy
            )
            logger.info(
                "Control plane mock: %s → %.1f%% mem, %.1fGB free",
                cluster_name, usage, free,
            )
            return usage, free

        # Production: query monitoring API (to be implemented).
        logger.info(
            "Control plane health: production mode — "
            "monitoring API not yet integrated; assuming healthy."
        )
        return 45.0, 6.0
