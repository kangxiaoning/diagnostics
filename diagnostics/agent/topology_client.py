"""Environment topology query client — single migration point.

Contract (design document §9-3/§9-4):
    query_environment_topology(cluster_name, namespace, workload_name, pod_name)
        -> RelationGraph dict (design document model), or None when the
        topology cannot be resolved (caller degrades to input-only diagnosis,
        marking the topology unavailable).

Local dev: mock implementation sourced from the private mock tool package.
Production / migration: replace this function body with the real CMDB query —
the caller (server app) and the injection layer depend only on this signature
and the RelationGraph dict shape, so migrating to another project touches this
module alone.

The RelationGraph dict shape and the field semantics / construction rules are
documented inline in query_environment_topology() — self-contained, no
external document required.  Read it before implementing the replacement query.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def query_environment_topology(
    cluster_name: str,
    namespace: str = "",
    workload_name: str = "",
    pod_name: str = "",
) -> dict | None:
    """Return the RelationGraph dict for the diagnostic target.

    Args:
        cluster_name: Serverless logical cluster name — routes to the topology
            of the matching Serverless cluster.
        namespace: Serverless namespace (optional).
        workload_name: Serverless workload name (optional).
        pod_name: Serverless Pod name (optional).

    Returns:
        RelationGraph as dict, or None on query failure (caller degrades
        per design document §9-3).

    RelationGraph dict shape — generic example; the field names and nesting
    below ARE the contract, the values are placeholders:
    {
      "mapping": {
        "serverless_cluster": "<sls-cluster>",
        "kmc_cluster": "<kmc-cluster>",
        "sci_cluster": "<sci-cluster>",
        "serverless_monitor_name": "<sls-monitor>",
        "kmc_monitor_name": "<kmc-monitor>",
        "sci_monitor_name": "<sci-monitor>",
        "k8s_name": "<k8s-cr-name>",
        "master_id": "<master-id>"
      },
      "serverless_views": [
        {
          "resource": {"kind": "ControlPlaneComponent|Deployment", "namespace": "<ns>", "name": "<name>"},
          "physical_cluster_type": "KMC|SCI",
          "physical_pods": [
            {"namespace": "<pod-ns>", "name": "<pod-name>", "node_name": "<node-ip>", "host_name": "<hostname>"}
          ],
          "is_control_plane": false,
          "control_plane_role": null
        }
      ],
      "kmc_views": [
        {
          "kmc_resource": {"kind": "Deployment|Pod|DaemonSet|EtcdMember", "namespace": "<ns>", "name": "<name>",
                           "node_name": "<node-ip>", "host_name": "<hostname>"},
          "owner_kmc_deployment": {"kind": "Deployment", "namespace": "<ns>", "name": "<name>"},
          "serverless_resource": {"kind": "...", "namespace": "<ns>", "name": "<name>"},
          "is_kmc_control_plane": false,
          "kmc_control_plane_role": null
        }
      ],
      "sci_views": [
        {
          "sci_pod": {"namespace": "<pod-ns>", "name": "<pod-name>", "node_name": "<node-ip>", "host_name": "<hostname>"},
          "serverless_resource": {"kind": "...", "namespace": "<ns>", "name": "<name>"},
          "is_sci_control_plane": false,
          "sci_control_plane_role": null
        }
      ],
      "etcd_views": [
        {
          "etcd_member": {"name": "<etcd-name>", "node_name": "<node-ip>", "host_name": "<hostname>"},
          "cluster": "shared_etcd",
          "roles": ["shared_etcd"]
        }
      ]
    }

    Field semantics / construction rules (design document §7.3):
    - Three views, three perspectives:
        * serverless_views — logical-cluster view: control-plane components,
          shared services and user workloads, each mapped to its hosting
          physical cluster (physical_cluster_type) and physical Pods.
        * kmc_views — physical KMC view: Deployments/Pods hosting the logical
          control plane, KMC's own static Pods, shared services and the shared
          etcd cluster.
        * sci_views — physical SCI view: burst workload Pods, SCI static Pods,
          vpc-cni DaemonSet Pods and coreDNS.
        * etcd_views — shared-etcd view: systemd host services (no namespace),
          `etcd_member` is an EtcdMemberRef {name, node_name, host_name} — the
          etcd tools take host_name as the parameter.
    - Derived views (NOT part of the RelationGraph model, injected layer only,
      design document §7.3): host_views / host_nodes are aggregated by
      topology_render from the three views + etcd_views.
    - Naming rules:
        * control-plane namespace = "<k8s_name>-<master_id>".
        * SCI workload namespace = "burst-ns-<master_id>".
        * SCI Pod name = "burst-<serverless_ns>-<serverless_pod_name>".
    - mapping.*_monitor_name are the ONLY legal source for the argus-expert
      monitor_name tool parameter — never fabricate a monitor name.
    - node_name carries the node IP (from K8s), host_name carries the node
      hostname; both are required — the host-expert resolves physical-node
      sub-metrics by hostname.
    - Boundary (design document §9-4): when the target workload/pod is absent
      from the topology, return the topology with a "_not_found" note instead
      of failing; when the whole query is unavailable, return None (§9-3).

    Replacement acceptance checklist:
    - Returned dict uses exactly the key names / nesting above; every view is
      a list (possibly empty).
    - Every physical Pod carries both node_name and host_name.
    - monitor_name fields carry real monitor identifiers, not placeholders.
    - Target-not-found returns _not_found; query failure returns None — never
      raise.
    """
    # Mock implementation (local dev only).  On migration, replace this body
    # with the production CMDB query — keep the signature and dict shape.
    try:
        from diagnostics.tools.mock.serverless import (
            query_environment_topology as _mock_query,
        )

        return _mock_query(
            cluster_name=cluster_name,
            namespace=namespace,
            workload_name=workload_name,
            pod_name=pod_name,
        )
    except ImportError:
        # Mock package not installed (e.g. GitHub clone, production mode):
        # degrade — the caller marks the topology unavailable (§9-3).
        logger.warning("topology mock unavailable; degrade to input-only diagnosis")
        return None
