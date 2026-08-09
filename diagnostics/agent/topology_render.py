"""RelationGraph injection rendering (single rendering path).

The RelationGraph dict is the authoritative topology fact.  This module only
*trims* and *serializes* it for prompt injection — it never rewrites fields
(design document §5.2/§7.3).  Per-role slices:

- Coordinator block: three-view identity-level compact inventory (+ etcd_views
  identity + host_nodes) — Pod-level placement (physical_pods / node_name /
  host_name) stripped; the Coordinator routes delegations by resource ownership.
- Argus block (compact+落点): keeps name/node_name landing points and the
  association chain so the argus expert can resolve tool parameters
  (namespace/workload/pod/node) directly from the slice; strips host_name
  except for EtcdMember entries (etcd tools take host_name).
- Host block: host_views — physical nodes aggregated from the three views +
  etcd_views, deduped by node IP, with roles.
- Deep-expert block: full view (Pod details kept).

All blocks are JSON code blocks — the structure is the single source of truth;
no markdown renderer is maintained (design document §5.2/§7.3).
"""

from __future__ import annotations

import json
from typing import Any

# roles 枚举（§5A.8）——host_views 派生用
_SHARED_ETCD_ROLE = "shared_etcd"
_KMC_CP_ROLE = "kmc_control_plane"
_KMC_ETCD_ROLE = "kmc_etcd"
_SHARED_COMPONENT_ROLE = "shared_component"
_SCI_CP_ROLE = "sci_control_plane"
_VPC_CNI_ROLE = "vpc_cni"
_WORKLOAD_ROLE = "workload"
_SCI_ETCD_ROLE = "sci_etcd"

# 共享组件命名空间（KMC 侧，roles+shared_component 判定；design §8.1）
_KMC_SHARED_NS = ("kube-system", "kmc-system", "kmc-ingress")
# SCI 侧 vpc-cni / coreDNS 前缀
_SCI_SVC_PREFIXES = ("vpc-cni-", "coredns-")


def _strip_none(obj: dict | None) -> dict:
    """Drop null-valued keys (zero-information noise)."""
    if not obj:
        return {}
    return {k: v for k, v in obj.items() if v is not None}


def _resource_ref(ref: dict | None, extra: tuple[str, ...] = ()) -> dict:
    """ObjectRef / PhysicalResourceRef → kind/namespace/name (+ optional extra).

    空字符串字段（如共享 etcd 无 namespace）一并剔除。
    """
    if not ref:
        return {}
    keys = ("kind", "namespace", "name") + extra
    out = {k: ref[k] for k in keys if k in ref}
    return {k: v for k, v in out.items() if v not in (None, "")}


def _pod_identity(pod: dict | None) -> dict:
    """PodNodeRef → namespace/name (drop node/host for the Coordinator view)."""
    if not pod:
        return {}
    out = {k: pod[k] for k in ("namespace", "name") if k in pod}
    return {k: v for k, v in out.items() if v is not None}


def _pod_landing(pod: dict | None) -> dict:
    """PodNodeRef → namespace/name/node_name (+ pod_id when present).

    Used by the argus compact view: keeps the landing points needed to resolve
    pod_name / node_name / (pod_id) tool parameters.
    """
    if not pod:
        return {}
    out = {k: pod[k] for k in ("namespace", "name", "node_name") if k in pod}
    if pod.get("pod_id"):
        out["pod_id"] = pod["pod_id"]
    return {k: v for k, v in out.items() if v is not None}


def build_coordinator_inventory(topology: dict) -> dict:
    """Compact identity-level inventory for the Coordinator (§5A.1).

    Pod-level placement (physical_pods / node_name / host_name) is stripped —
    it is reserved for the per-expert full-view injection.  Adds etcd_views
    identity + host_nodes (host-argus delegation parameter source).
    """
    inventory: dict[str, Any] = {"mapping": _strip_none(topology.get("mapping") or {})}

    sls: list[dict[str, Any]] = []
    for v in topology.get("serverless_views", []) or []:
        item: dict[str, Any] = {
            "resource": _resource_ref(v.get("resource")),
            "physical_cluster_type": v.get("physical_cluster_type"),
            "is_control_plane": bool(v.get("is_control_plane", False)),
        }
        if v.get("control_plane_role"):
            item["control_plane_role"] = v["control_plane_role"]
        sls.append(item)
    inventory["serverless_views"] = sls

    kmc: list[dict[str, Any]] = []
    for v in topology.get("kmc_views", []) or []:
        item: dict[str, Any] = {
            "kmc_resource": _resource_ref(v.get("kmc_resource")),
            "is_kmc_control_plane": bool(v.get("is_kmc_control_plane", False)),
        }
        for key in ("owner_kmc_deployment", "serverless_resource"):
            if v.get(key):
                item[key] = _resource_ref(v[key])
        if v.get("kmc_control_plane_role"):
            item["kmc_control_plane_role"] = v["kmc_control_plane_role"]
        kmc.append(item)
    inventory["kmc_views"] = kmc

    sci: list[dict[str, Any]] = []
    for v in topology.get("sci_views", []) or []:
        item: dict[str, Any] = {
            "sci_pod": _pod_identity(v.get("sci_pod")),
            "is_sci_control_plane": bool(v.get("is_sci_control_plane", False)),
        }
        if v.get("serverless_resource"):
            item["serverless_resource"] = _resource_ref(v["serverless_resource"])
        if v.get("sci_control_plane_role"):
            item["sci_control_plane_role"] = v["sci_control_plane_role"]
        sci.append(item)
    inventory["sci_views"] = sci

    # etcd_views 身份级（共享 etcd：etcd_member 身份 + cluster/roles；去 node_name——§5A.1）
    etcd: list[dict[str, Any]] = []
    for v in topology.get("etcd_views", []) or []:
        item: dict[str, Any] = {
            "etcd_member": _resource_ref(v.get("etcd_member")),
            "cluster": v.get("cluster") or "shared_etcd",
        }
        if v.get("roles"):
            item["roles"] = v["roles"]
        etcd.append(item)
    inventory["etcd_views"] = etcd

    # host_nodes：物理节点身份级投影（host-argus 委派参数来源）——§5A.1
    host_nodes = _build_host_nodes(topology)
    if host_nodes:
        inventory["host_nodes"] = host_nodes

    # Boundary note (design document §9-4): target not found in topology.
    if topology.get("_not_found"):
        inventory["_not_found"] = topology["_not_found"]
    return inventory


def build_argus_compact_view(topology: dict, view_key: str) -> dict:
    """Compact+落点 view payload for an argus expert (§5A.5/5A.6/5A.7).

    Rules:
    - serverless_views: resource identity + physical_cluster_type +
      is_control_plane + physical_pods[].{name, node_name, pod_id};
      host_name / control_plane_role stripped.
    - kmc_views / sci_views: resource identity (kind/ns/name/node_name) +
      is_*_control_plane + serverless_resource association;
      owner_kmc_deployment / roles stripped.
    - EtcdMember entries keep host_name (etcd tools take host_name) — §5A.6/5A.7.
    - etcd_views: etcd_member {name, node_name, host_name} + cluster
      (no roles — argus only queries metrics) — §5A.5.
    """
    payload: dict[str, Any] = {"mapping": _strip_none(topology.get("mapping") or {})}

    if view_key == "serverless_views":
        items: list[dict[str, Any]] = []
        for v in topology.get("serverless_views", []) or []:
            item: dict[str, Any] = {
                "resource": _resource_ref(v.get("resource")),
                "physical_cluster_type": v.get("physical_cluster_type"),
                "is_control_plane": bool(v.get("is_control_plane", False)),
            }
            pods = [p for p in (_pod_landing(p) for p in (v.get("physical_pods") or [])) if p]
            if pods:
                item["physical_pods"] = pods
            items.append(item)
        payload["serverless_views"] = items
        # 共享 etcd 独立块（供 query_argus_shared_etcd 的 host_name 参数）——§5A.5
        etcd = [_etcd_member_compact(v) for v in (topology.get("etcd_views") or [])]
        if etcd:
            payload["etcd_views"] = etcd
    elif view_key == "kmc_views":
        items = []
        for v in topology.get("kmc_views", []) or []:
            kr = v.get("kmc_resource") or {}
            is_etcd = kr.get("kind") == "EtcdMember"
            extra = ("node_name", "host_name") if is_etcd else ("node_name",)
            item: dict[str, Any] = {
                "kmc_resource": _resource_ref(kr, extra=extra),
                "is_kmc_control_plane": bool(v.get("is_kmc_control_plane", False)),
            }
            if v.get("serverless_resource"):
                item["serverless_resource"] = _resource_ref(v["serverless_resource"])
            items.append(item)
        payload["kmc_views"] = items
    elif view_key == "sci_views":
        items = []
        for v in topology.get("sci_views", []) or []:
            sp = v.get("sci_pod") or {}
            is_etcd = "etcd" in sp.get("name", "")
            extra = ("node_name", "host_name") if is_etcd else ("node_name",)
            item: dict[str, Any] = {
                "sci_pod": _resource_ref(sp, extra=extra),
                "is_sci_control_plane": bool(v.get("is_sci_control_plane", False)),
            }
            if v.get("serverless_resource"):
                item["serverless_resource"] = _resource_ref(v["serverless_resource"])
            items.append(item)
        payload["sci_views"] = items
    else:
        # fallback: unknown key → empty view
        payload[view_key] = []

    return payload


def _etcd_member_compact(v: dict) -> dict:
    """etcd_views 条目 → {etcd_member: {kind, name, node_name, host_name}, cluster}（去 roles）。"""
    em = v.get("etcd_member") or {}
    return {
        "etcd_member": _resource_ref(em, extra=("node_name", "host_name")),
        "cluster": v.get("cluster") or "shared_etcd",
    }


def build_host_views(topology: dict) -> list[dict]:
    """Aggregate physical nodes from the three views + etcd_views (§7.3/§5A.8).

    One record per node (deduped by node IP), roles merged:
    - serverless_views: physical_pods → cluster from physical_cluster_type
      (KMC→kmc, SCI→sci); is_control_plane → +kmc/sci_control_plane.
    - kmc_views: kmc_resource → cluster=kmc; is_kmc_control_plane →
      +kmc_control_plane; kind=EtcdMember → +kmc_etcd; shared ns →
      +shared_component.
    - sci_views: sci_pod → cluster=sci; is_sci_control_plane →
      +sci_control_plane; kind=EtcdMember / name 含 etcd → +sci_etcd;
      vpc-cni/coredns → +vpc_cni/shared_component.
    - etcd_views: etcd_member → cluster=shared_etcd, roles+shared_etcd.
    """
    nodes: dict[str, dict[str, Any]] = {}

    def add(host_name: str, node_name: str, cluster: str, role: str) -> None:
        if not host_name and not node_name:
            return
        key = node_name or host_name
        rec = nodes.setdefault(key, {"host_name": host_name, "node_name": node_name, "cluster": cluster, "roles": []})
        if host_name and not rec["host_name"]:
            rec["host_name"] = host_name
        if node_name and not rec["node_name"]:
            rec["node_name"] = node_name
        if role and role not in rec["roles"]:
            rec["roles"].append(role)

    # serverless_views
    for v in topology.get("serverless_views", []) or []:
        ctype = v.get("physical_cluster_type")
        cluster = "kmc" if ctype == "KMC" else "sci" if ctype == "SCI" else ""
        if not cluster:
            continue
        cp_role = _KMC_CP_ROLE if cluster == "kmc" else _SCI_CP_ROLE
        for p in v.get("physical_pods", []) or []:
            add(p.get("host_name") or "", p.get("node_name") or "", cluster, cp_role if v.get("is_control_plane") else "")

    # kmc_views
    for v in topology.get("kmc_views", []) or []:
        kr = v.get("kmc_resource") or {}
        roles: list[str] = []
        if v.get("is_kmc_control_plane"):
            roles.append(_KMC_CP_ROLE)
        if kr.get("kind") == "EtcdMember":
            roles.append(_KMC_ETCD_ROLE)
        elif kr.get("namespace") in _KMC_SHARED_NS:
            roles.append(_SHARED_COMPONENT_ROLE)
        for role in roles:
            add(kr.get("host_name") or "", kr.get("node_name") or "", "kmc", role)

    # sci_views
    for v in topology.get("sci_views", []) or []:
        sp = v.get("sci_pod") or {}
        roles: list[str] = []
        if v.get("is_sci_control_plane"):
            roles.append(_SCI_CP_ROLE)
        if sp.get("kind") == "EtcdMember" or "etcd" in sp.get("name", ""):
            roles.append(_SCI_ETCD_ROLE)
        elif sp.get("namespace") == "kube-system":
            if sp.get("name", "").startswith("vpc-cni-"):
                roles.append(_VPC_CNI_ROLE)
            elif sp.get("name", "").startswith("coredns-"):
                roles.append(_SHARED_COMPONENT_ROLE)
        elif sp.get("namespace", "").startswith("burst-"):
            roles.append(_WORKLOAD_ROLE)
        for role in roles:
            add(sp.get("host_name") or "", sp.get("node_name") or "", "sci", role)

    # etcd_views
    for v in topology.get("etcd_views", []) or []:
        em = v.get("etcd_member") or {}
        add(em.get("host_name") or "", em.get("node_name") or "", "shared_etcd", _SHARED_ETCD_ROLE)

    out = list(nodes.values())
    out.sort(key=lambda r: (r.get("cluster") or "", r.get("host_name") or r.get("node_name") or ""))
    return out


def _build_host_nodes(topology: dict) -> list[dict]:
    """host_nodes: identity projection of host_views — {host_name, cluster}（去 roles，§5A.1）。"""
    seen: dict[str, str] = {}
    for rec in build_host_views(topology):
        h = rec.get("host_name") or rec.get("node_name") or ""
        if not h:
            continue
        seen.setdefault(h, rec.get("cluster") or "")
    out = [{"host_name": h, "cluster": c} for h, c in sorted(seen.items())]
    return out


def render_json_block(title: str, payload: dict) -> str:
    """Serialize a payload as a JSON code block under a section title.

    紧凑序列化（§7.4 增强 A）：indent=None + separators=(",", ":")，省 30-40% token。
    """
    body = json.dumps(payload, ensure_ascii=False, indent=None, separators=(",", ":"))
    return f"## {title}\n\n```json\n{body}\n```\n"


def coordinator_block(topology: dict, title: str = "环境拓扑（Serverless RelationGraph）") -> str:
    """Render the Coordinator's compact topology block."""
    return render_json_block(title, build_coordinator_inventory(topology))


def expert_view(topology: dict, view_key: str) -> dict:
    """Full view payload for a deep expert (Pod details kept)."""
    return {"mapping": _strip_none(topology.get("mapping") or {}), view_key: topology.get(view_key, []) or []}
