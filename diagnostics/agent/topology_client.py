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


def query_dedicated_environment(
    cluster_name: str,
    namespace: str = "",
    workload_type: str = "",
    workload_name: str = "",
    pod_name: str = "",
    scene: str = "single_pod",
) -> dict | None:
    """Return the DedicatedEnvironment dict for a dedicated-cluster scene.

    Dedicated-cluster counterpart of query_environment_topology — same
    single-migration-point role (design document — dedicated environment
    spec): on production migration replace the body with the real CMDB /
    cloud-API query; callers depend only on this signature and the dict
    shape below.

    Per-scene input contract (design document — dedicated environment
    spec; enforced upstream by the scene input schema):
    - scene="single_pod": cluster_name + namespace + workload_type +
      workload_name + pod_name are ALL required — the full workload
      anchor; the query resolves EVERY replica landing of that workload
      into kubernetes.workload_pods.
    - scene="single_cluster": cluster_name only — cluster-level
      diagnostic object; no workload anchor, the workload/pod parameters
      are ignored and kubernetes.workload_pods is omitted.

    Args:
        cluster_name: Dedicated cluster name (required, both scenes).
        namespace: Pod namespace (required for single_pod).
        workload_type: Workload kind, e.g. "Deployment" (required for
            single_pod).
        workload_name: Workload name (required for single_pod).
        pod_name: User Pod name — one replica of the target workload
            (required for single_pod).
        scene: "single_pod" | "single_cluster".

    Returns:
        DedicatedEnvironment as dict, or None on query failure (caller
        degrades to input-only diagnosis, same convention as §9-3).

    DedicatedEnvironment dict shape — the field names and nesting below ARE
    the contract, the values are placeholders.  Per-scene difference is
    marked inline: workload_pods exists ONLY in the single_pod return.
    {
      "cluster": {"name": "<cluster>", "k8s_version": "<version>",
                  "monitor_name": "<argus-monitor>"},
      "kubernetes": {
        // ── single_pod ONLY: every replica of the target workload with
        //    its landing (omitted entirely for single_cluster) ──
        "workload_pods": [
          {"pod_name": "<pod>", "nodename": "<node-ip>", "hostname": "<ecs>"}
        ],
        "control_plane": {
          // apiserver access topology (kubeadm controlPlaneEndpoint
          // semantics): all components/nodes reach kube-apiserver via
          // the EXTERNAL ELB's VIP; the ELB itself is outside the
          // cluster (not a diagnostic object) — only the access locator
          // {vip, port} is carried; backends derive from the
          // kube-apiserver component (never duplicated here):
          "apiserver_endpoint": {"vip": "<vip>", "port": 6443},
          "components": [
            // container control-plane component = K8s STATIC POD —
            // replicas = one pod per hosts entry (mirror-pod naming),
            // full landing triple (uniform pod shape):
            {"name": "kube-apiserver", "workload_type": "StaticPod",
             "workload_name": "kube-apiserver", "namespace": "kube-system",
             "pods": [{"pod_name": "<pod>", "nodename": "<node-ip>",
                       "hostname": "<ecs>"}]},
            // binary (systemd) component — replicas = one process per
            // hosts entry (host-layer replica; unit derives as
            // "<workload_name>.service" inside tools):
            {"name": "etcd", "workload_type": "systemd",
             "workload_name": "etcd",
             "processes": [{"process_name": "<proc>",
                            "hostname": "<ecs>"}]}
          ]
        },
        "platform_components": [
          // always API-managed container workloads with per-pod landings
          // (replicas scatter — irreducible; DaemonSet → one per node):
          {"name": "vpc-cni-agent", "workload_type": "DaemonSet",
           "workload_name": "vpc-cni-agent", "namespace": "kube-system",
           "pods": [{"pod_name": "<pod>", "nodename": "<node-ip>",
                     "hostname": "<ecs>"}]}
        ]
      },
      // ECS↔physical topology (cloud layer, sibling of the kubernetes
      // view — worker nodes are ECS too, so the association is not
      // control-plane specific; scope = the diagnostic-object ECS,
      // currently the co-located control-plane ECS (typically 3);
      // worker ECS can join later with the same entry shape):
      "ecs_to_physical_host": [
        {"nodename": "<node-ip>", "hostname": "<ecs>",
         "physical_host": "<physical-host>"}
      ]
    }

    Field semantics / construction rules:
    - Top level = cluster identity + the kubernetes view + the
      ECS↔physical topology.  There is NO separate nodes inventory:
      the environment is the diagnostic-object SUBGRAPH (a cluster may
      have hundreds of nodes — only the nodes carrying queried
      components / workloads matter); worker nodes are DERIVED by
      consumers from the platform_components / workload_pods landings.
    - ecs_to_physical_host is the ECS-layer inventory/topology
      (production invariant: every control-plane component co-locates
      on these ECS — typically 3): nodename (K8s Node object name,
      k8s-tool parameter — the ONLY master-nodename source in the
      systemd form, where process replicas carry no nodename) +
      hostname (OS hostname, host-tool parameter) + physical_host
      (physical-host association).  It sits BESIDE the kubernetes view
      (cloud layer — worker nodes are ECS too, so the association is
      not control-plane specific); worker physical hosts stay
      tool-resolvable, and the same entry shape can carry worker ECS
      when diagnosis needs them.  The distinct physical-host set
      derives from this list (single source of truth).
    - Control-plane components carry IDENTITY + REPLICAS — the
      multi-replica nature is first-class in the structure: "StaticPod"
      → `pods` (one pod per hosts entry named
      "<workload_name>-<hostname>", K8s mirror-pod naming; full landing
      triple {pod_name, nodename, hostname} — uniform pod shape across
      the whole structure); "systemd" → `processes` (one process per
      hosts entry {process_name, hostname} — host-layer replicas, no
      nodename: processes are not K8s objects; the systemd unit derives
      as       "<workload_name>.service" inside tools).  Replica landings
      REFERENCE the ecs_to_physical_host entries — physical_host is
      never duplicated into replicas; replica count ==
      len(ecs_to_physical_host) (co-location invariant).
    - CONTROL-PLANE components are EXPLICITLY discriminated by
      `workload_type` (the K8s KEP-1027 union-discriminator pattern):
      "StaticPod" = container form (pods payload); "systemd" = binary
      form (processes payload).  Consumers dispatch deterministically
      on the value (switch), never probe keys; an unknown workload_type
      value is a contract violation (fail loudly).
    - PLATFORM components are ALWAYS containerized (API-managed
      workloads): `workload_type` carries the K8s kind ("Deployment" /
      "DaemonSet" / ...) and `workload_name` the workload identity;
      payload is always `namespace` + `pods` (per-pod landings —
      replicas scatter across different nodes, irreducible).
    - Official terminology mapping (K8s documentation):
        * control-plane "StaticPod" IS the K8s "static pod / self-hosted
          control plane" — managed directly by the node-local kubelet
          (starts before the API server is available; NOT managed by the
          control plane: no rollout/rollback/scale; deleting the mirror
          pod via kubectl is a no-op); mirror pods live in kube-system
          named "<component>-<node hostname>" (the derivation rule).
        * "systemd" is a systemd host service — the Kubespray
          etcd_deployment_type="host" pattern; static-pod apiserver +
          systemd etcd is Kubespray's DEFAULT, so mixed forms within one
          cluster are mainstream (per-component granularity is required).
        * etcd topology (production fact): etcd is STACKED — co-located
          with the other control-plane components on the same
          control-plane ECS (both forms).  The external-etcd variant
          (dedicated etcd machines) is not the production form — this
          structure cannot express it (co-location invariant; fail
          loudly if it ever appears).
        * apiserver access topology (production fact): the
          kube-apiserver replicas sit behind an EXTERNAL ELB; every
          other component and node talks to the apiserver through the
          ELB VIP.  `control_plane.apiserver_endpoint` = {vip, port}
          (kubeadm controlPlaneEndpoint semantics — TCP-forwarding LB
          distributing to healthy instances, health check TCP :6443).
          The ELB itself lives OUTSIDE the cluster boundary — it is NOT
          a diagnostic object of this environment (no elb identity), the
          endpoint is purely the access locator (VIP-path errors:
          connect timeout / TLS reset to the VIP).
    - Every Pod landing (platform pods / workload_pods) carries BOTH
      `nodename` (the K8s Node object name — an IP in IP-named clusters,
      the k8s-tool parameter) and `hostname` (the OS hostname — the
      host-tool parameter); the two naming spaces follow the official
      Node status.addresses InternalIP/Hostname split.
    - workload_pods is present only for single_pod scenes: EVERY replica
      of the target workload with its landing (multi-replica workloads
      spread across nodes); single_cluster carries no workload_pods.
    - cluster.monitor_name is the cluster-level argus monitor identifier
      — the ONLY legal source for the k8s-argus / host-argus monitor_name
      tool parameter.  Production MUST return the real monitor identifier
      from the CMDB (never a placeholder, never a string-rule derivation);
      when present it SUPERSEDES any param-resolved value (single source
      of truth) — the param-resolved value survives only on the degraded
      path (query returned None).
    - High-signal only: diagnosis tools take hostname/nodename as their
      parameters, so cloud-identity details (instance id/type/zone),
      physical-host ids/status and pod IPs are NOT carried — details are
      resolved inside tools, never carried by the environment.

    Replacement acceptance checklist:
    - Returned dict uses exactly the key names / nesting above; top
      level is exactly {cluster, kubernetes, ecs_to_physical_host} (NO
      nodes inventory); every list key is a list (possibly empty);
      workload_pods omitted for single_cluster; cluster carries exactly
      {name, k8s_version, monitor_name} and monitor_name is the REAL
      argus monitor identifier (never a placeholder);
      ecs_to_physical_host entries carry exactly {nodename, hostname,
      physical_host}.
    - control_plane carries exactly {apiserver_endpoint, components}:
      apiserver_endpoint = {vip, port} (real VIP of the external ELB,
      never a placeholder; NO elb identity).
    - Control-plane component entries carry identity + replicas:
      "StaticPod" → {name, workload_type, workload_name, namespace,
      pods} with one pod per hosts entry (full landing triple);
      "systemd" → {name, workload_type, workload_name, processes} with
      one process per hosts entry ({process_name, hostname}); NEVER
      both payloads, NEVER physical_host inside replicas; platform
      components always carry `namespace`+`pods` with per-pod landings.
    - Every pod landing (platform pods / workload_pods) carries BOTH
      `nodename` and `hostname`.
    - Target-not-found returns "_not_found"; query failure returns None —
      never raise.
    """
    # Input-contract check (single_pod requires the full workload anchor).
    # Warn and pass through — never raise: the degraded path must stay
    # alive (same convention as the query itself).
    if scene == "single_pod" and not all(
            (namespace, workload_type, workload_name, pod_name)):
        logger.warning(
            "dedicated single_pod query with incomplete workload anchor: "
            "namespace=%r workload_type=%r workload_name=%r pod_name=%r",
            namespace, workload_type, workload_name, pod_name,
        )

    # Mock implementation (local dev only).  On migration, replace this body
    # with the production CMDB / cloud-API query — keep the signature and
    # dict shape.
    try:
        from diagnostics.tools.mock.dedicated import (
            query_dedicated_environment as _mock_query,
        )

        return _mock_query(
            cluster_name=cluster_name,
            namespace=namespace,
            workload_type=workload_type,
            workload_name=workload_name,
            pod_name=pod_name,
            scene=scene,
        )
    except ImportError:
        # Mock package not installed (e.g. GitHub clone, production mode):
        # degrade — the caller marks the environment unavailable.
        logger.warning("dedicated env mock unavailable; degrade to input-only diagnosis")
        return None
