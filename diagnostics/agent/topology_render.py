"""RelationGraph injection rendering (single rendering path).

The RelationGraph dict is the authoritative topology fact.  This module only
*trims* and *serializes* it for prompt injection — it never rewrites fields
(design document §5.2/§7.3).  Per-role slices:

- Coordinator block: three-view identity-level compact inventory (+ etcd_views
  identity + host_nodes) — Pod-level placement (physical_pods / node_name /
  host_name) stripped; the Coordinator routes delegations by resource ownership.
  etcd_views identity keeps only {name} (EtcdMemberRef — no kind/namespace,
  systemd host service, design document §7.2).
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
            "etcd_member": _etcd_member_identity(v.get("etcd_member")),
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


def _etcd_member_identity(em: dict | None) -> dict:
    """etcd_member → {name}（Coordinator 身份级，去 node/host——§5A.1）。

    EtcdMemberRef 为 systemd 主机服务引用（无 kind/namespace）；身份级仅保留
    ETCD_NAME，落点（node_name/host_name）见 argus 紧凑版（§5A.5）。
    """
    if not em:
        return {}
    return {k: em[k] for k in ("name",) if em.get(k) is not None}


def _etcd_member_compact(v: dict) -> dict:
    """etcd_views 条目 → {etcd_member: {name, node_name, host_name}, cluster}（去 roles）。"""
    em = v.get("etcd_member") or {}
    member = {k: em[k] for k in ("name", "node_name", "host_name") if em.get(k) is not None}
    return {
        "etcd_member": member,
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


# ── Argus unified context contract (design document §7.3) ──
# One JSON block per argus expert: every tool-parameter value (monitor_name /
# cluster_name / hostname / time window) appears exactly once (single source
# of truth); the diagnostic target is extracted deterministically from
# param_overrides + naming rules; absence is explicit (§9-4), never fabricated.
_ARGUS_MONITOR_KEY = {
    "serverless-argus-expert": "serverless_monitor_name",
    "kmc-argus-expert": "kmc_monitor_name",
    "sci-argus-expert": "sci_monitor_name",
}
_ARGUS_CLUSTER_KEY = {
    "serverless-argus-expert": "serverless_cluster",
    "kmc-argus-expert": "kmc_cluster",
    "sci-argus-expert": "sci_cluster",
}
_ARGUS_VIEW_KEY = {
    "serverless-argus-expert": "serverless_views",
    "kmc-argus-expert": "kmc_views",
    "sci-argus-expert": "sci_views",
}


def _derive_sci_pod(target: dict) -> str:
    """SCI Pod 名确定性派生：burst-<serverless_ns>-<serverless_pod_name>。"""
    ns, pod = target.get("namespace", ""), target.get("pod_name", "")
    return f"burst-{ns}-{pod}" if ns and pod else ""


def _match_logical_resource(topology: dict, target: dict) -> dict | None:
    """serverless_views 中精确匹配诊断目标（kind 可选过滤）。"""
    ns, wl = target.get("namespace", ""), target.get("workload_name", "")
    if not (ns and wl):
        return None
    kind = target.get("kind", "")
    for v in topology.get("serverless_views") or []:
        r = v.get("resource") or {}
        if r.get("namespace") == ns and r.get("name") == wl \
                and (not kind or r.get("kind") == kind):
            return v
    return None


def _target_node_names(topology: dict, target: dict, derived: str) -> set[str]:
    """诊断目标的物理落点节点 IP 集合（serverless_views 落点 + SCI 派生 Pod）。"""
    nodes: set[str] = set()
    item = _match_logical_resource(topology, target)
    if item:
        for p in item.get("physical_pods") or []:
            if p.get("node_name"):
                nodes.add(p["node_name"])
    if derived:
        for v in topology.get("sci_views") or []:
            sp = v.get("sci_pod") or {}
            if sp.get("name") == derived and sp.get("node_name"):
                nodes.add(sp["node_name"])
    return nodes


def build_argus_context_view(topology: dict, expert: str,
                             param_overrides: dict | None = None,
                             start_time: str = "", end_time: str = "") -> dict:
    """Unified JSON parameter-contract block for one argus expert (§7.3).

    Single source of truth — each tool-parameter value appears exactly once
    in the expert's context, so the LLM never arbitrates between duplicated
    sources (empirically it picks the wrong one).  The diagnostic target is
    resolved deterministically; absence markers are explicit (§9-4).
    """
    mapping = _strip_none(topology.get("mapping") or {})
    po = param_overrides or {}
    block: dict[str, Any] = {"expert": expert}

    # ── diagnostic target (from user input, verbatim) ──
    target = {k: v for k, v in (
        ("kind", po.get("workload_type", "")),
        ("namespace", po.get("namespace", "")),
        ("workload_name", po.get("workload_name", "")),
        ("pod_name", po.get("pod_name", "")),
    ) if v}
    derived = _derive_sci_pod(target)
    if derived:
        target["derived_sci_pod"] = derived
    if target:
        block["diagnostic_target"] = target

    # ── naming rules (deterministic derivation basis, JSON form) ──
    k8s_name, master_id = mapping.get("k8s_name", ""), mapping.get("master_id", "")
    if k8s_name and master_id:
        block["naming_rules"] = {
            "control_plane_ns": f"{k8s_name}-{master_id}",
            "workload_ns": f"burst-ns-{master_id}",
            "sci_pod": "burst-<serverless_ns>-<serverless_pod_name>",
        }

    # ── host-argus: hostname dimension (no monitor_name; §8.1⑥) ──
    if expert == "host-argus-expert":
        block["tool_params"] = {"start_time": start_time, "end_time": end_time}
        # host_name only — node_name (node IP) deliberately excluded: dual
        # values made the model fill node IPs as the hostname parameter.
        hosts = [
            {k: r[k] for k in ("host_name", "cluster", "roles") if r.get(k)}
            for r in build_host_views(topology)
            if r.get("host_name")
        ]
        if hosts:
            block["hosts"] = hosts
        if target:
            land_nodes = _target_node_names(topology, target, derived)
            land_hosts = [
                r["host_name"] for r in build_host_views(topology)
                if r.get("node_name") in land_nodes and r.get("host_name")
            ]
            if land_hosts:
                block["target_landing_hosts"] = sorted(land_hosts)
            elif land_nodes:
                block["boundary"] = {"target_host_not_located": True}
        if topology.get("_not_found"):
            block.setdefault("boundary", {})["not_found"] = topology["_not_found"]
        return block

    view_key = _ARGUS_VIEW_KEY.get(expert)
    if view_key is None:
        return block

    # ── tool params (sole occurrence in the whole context) ──
    block["tool_params"] = {
        "monitor_name": mapping.get(_ARGUS_MONITOR_KEY[expert]),
        "cluster_name": mapping.get(_ARGUS_CLUSTER_KEY[expert]),
        "start_time": start_time,
        "end_time": end_time,
    }

    # ── target matching + landings per view semantics ──
    boundary: dict[str, Any] = {}
    if target:
        if view_key in ("serverless_views", "kmc_views"):
            item = _match_logical_resource(topology, target)
            if item is not None:
                pct = item.get("physical_cluster_type") or ""
                block["diagnostic_target"]["physical_cluster_type"] = pct
                if view_key == "serverless_views" or pct == "KMC":
                    landings = [l for l in (_pod_landing(p) for p in (item.get("physical_pods") or [])) if l]
                    if landings:
                        block["landings"] = landings
                # kmc-argus + SCI-hosted target → landings 为空属正常（目标不在 KMC）
            else:
                boundary["target_not_in_topology"] = True
        else:  # sci_views
            if derived:
                landings = []
                for v in topology.get("sci_views") or []:
                    sp = v.get("sci_pod") or {}
                    if sp.get("name") == derived:
                        land = _pod_landing(sp)
                        if land:
                            landings.append(land)
                if landings:
                    block["landings"] = landings
                else:
                    # 物理侧缺席可能是故障本体（VK→SCI 同步断链，§9-4）——
                    # 显式标记而非静默缺省，专家不得捏造该 Pod 的指标。
                    boundary["target_sci_pod_absent"] = True
            elif target.get("namespace") or target.get("workload_name"):
                boundary["target_sci_pod_underivable"] = True
    if topology.get("_not_found"):
        boundary["not_found"] = topology["_not_found"]
    if boundary:
        block["boundary"] = boundary

    # ── inventory: full compact slice (SSOT for ad-hoc queries) ──
    # mapping dropped here — monitor/cluster names are single-sourced in
    # tool_params; k8s_name/master_id are covered by naming_rules.
    inv = build_argus_compact_view(topology, view_key)
    inv.pop("mapping", None)
    if any(inv.get(k) for k in inv):
        block["inventory"] = inv

    return block


def render_json_block(title: str, payload: dict) -> str:
    """Serialize a payload as a JSON code block under a section title.

    紧凑序列化（§7.4 增强 A）：indent=None + separators=(",", ":")，省 30-40% token。
    """
    body = json.dumps(payload, ensure_ascii=False, indent=None, separators=(",", ":"))
    return f"## {title}\n\n```json\n{body}\n```\n"


def coordinator_block(topology: dict, title: str = "环境拓扑（Serverless RelationGraph）") -> str:
    """Render the Coordinator's compact topology block."""
    return render_json_block(title, build_coordinator_inventory(topology))


def _is_cluster_level_topology(topology: dict) -> bool:
    """Cluster-level RelationGraph form detection.

    The session-init query stamps ``_query_anchor`` onto the topology
    dict (private key, same convention as ``_not_found``): cluster-level
    form = anchor present but carries NO pod/workload/namespace scoping
    (single_cluster serverless queries pass only cluster_name).  When
    the marker is absent (legacy callers / unit-constructed dicts) the
    Pod-anchored rendering applies unchanged — conservative default that
    keeps every existing consumer's output identical.
    """
    anchor = topology.get("_query_anchor")
    if not isinstance(anchor, dict):
        return False
    return not any(
        str(anchor.get(k) or "").strip()
        for k in ("namespace", "workload_name", "pod_name")
    )


def _render_cluster_level_mapping(topology: dict) -> str:
    """Cluster-level topology mapping for the single-cluster serverless form.

    The diagnostic subject is the three-cluster relationship, so the
    mapping renders relation header + per-view scale summary rows
    instead of per-resource landing rows.  Purely derived from the
    RelationGraph — deterministic, no generative transcription.
    Returns "" when the mapping lacks the logical cluster name.
    """
    m = topology.get("mapping") or {}
    sls_c = m.get("serverless_cluster") or ""
    if not sls_c:
        return ""

    rel = f"逻辑集群 **{sls_c}**"
    phys = [f"{role} **{name}**" for role, name in
            (("KMC", m.get("kmc_cluster")), ("SCI", m.get("sci_cluster")))
            if name]
    if phys:
        rel += " → " + "，".join(phys)
    monitors = [f"{c}（{m[k]}）" for c, k in
                ((sls_c, "serverless_monitor_name"),
                 (m.get("kmc_cluster") or "", "kmc_monitor_name"),
                 (m.get("sci_cluster") or "", "sci_monitor_name"))
                if m.get(k)]
    head = rel + "\n\n"
    if monitors:
        head += "监控名：" + "，".join(monitors) + "\n\n"

    def _cp_count(views: list[dict], flag: str) -> tuple[int, int]:
        total = len(views)
        cp = sum(1 for v in views if v.get(flag))
        return total, cp

    sls_views = topology.get("serverless_views") or []
    kmc_views = topology.get("kmc_views") or []
    sci_views = topology.get("sci_views") or []
    etcd_views = topology.get("etcd_views") or []
    host_nodes = build_host_views(topology)

    rows: list[str] = []

    def _row(view: str, total: int, cp: int, desc: str) -> None:
        comp = f"控制面组件 {cp}" if cp else "—"
        rest = total - cp
        if rest:
            comp = comp + f" + 其他资源 {rest}" if cp else "—"
        rows.append(f"| {view} | {total} | {comp} | {desc} |")

    t, cp = _cp_count(sls_views, "is_control_plane")
    _row("逻辑集群资源（serverless_views）", t, cp,
         "控制面 Deployment 形态下沉于 KMC")
    t, cp = _cp_count(kmc_views, "is_kmc_control_plane")
    _row("KMC 物理资源（kmc_views）", t, cp, "承载逻辑控制面与共享组件")
    t, cp = _cp_count(sci_views, "is_sci_control_plane")
    _row("SCI 数据面资源（sci_views）", t, cp, "用户工作负载物理 Pod")
    etcd_hosts = sorted({((v.get("etcd_member") or {}).get("host_name") or "")
                         for v in etcd_views} - {""})
    rows.append(f"| 共享 etcd（etcd_views） | {len(etcd_views)} | — "
                f"| {'/'.join(etcd_hosts) or '—'} |")
    if host_nodes:
        clusters: dict[str, int] = {}
        for n in host_nodes:
            c = n.get("cluster") or "?"
            clusters[c] = clusters.get(c, 0) + 1
        breakdown = "，".join(f"{k} {v}" for k, v in sorted(clusters.items()))
        rows.append(f"| 物理节点（host_views 派生） | {len(host_nodes)} | — "
                    f"| {breakdown} |")

    return (head
            + "| 视图 | 条目数 | 控制面构成 | 说明 |\n"
            + "|---|---|---|---|\n" + "\n".join(rows))


def report_mapping_table(topology: dict) -> str:
    """Report-level topology mapping table (design document §9, v3.14.0).

    Two forms dispatched by ``_query_anchor`` stamp:
    - Cluster-level (single_cluster serverless): three-cluster relation
      header + per-view scale summary rows.
    - Pod-anchored (default): logical resource → physical layer/object/
      landing rows.

    The report's 拓扑映射 section is a transcription of structured
    ledger data, so it is rendered by code and injected at report-write
    time instead of being transcribed by the LLM (generative
    transcription of structured data is a known fidelity-failure
    surface, design document §12).
    Returns "" when the topology carries no mappable rows.
    """
    if not topology:
        return ""
    if _is_cluster_level_topology(topology):
        return _render_cluster_level_mapping(topology)

    def _ref(ref: dict | None) -> str:
        r = _resource_ref(ref)
        if not r:
            return ""
        kind, name = r.get("kind", ""), r.get("name", "")
        return f"{kind}/{name}" if kind else name

    def _pod_desc(pod: dict) -> str:
        desc = pod.get("name") or ""
        if desc and pod.get("pod_id"):
            desc += f"（IP {pod['pod_id']}）"
        return desc

    def _landing(pod: dict) -> str:
        return pod.get("host_name") or pod.get("node_name") or ""

    sci_views = topology.get("sci_views") or []
    kmc_views = topology.get("kmc_views") or []
    rows: list[str] = []
    for v in topology.get("serverless_views") or []:
        logical = _ref(v.get("resource"))
        if not logical:
            continue
        res_name = (v.get("resource") or {}).get("name")
        layer_raw = v.get("physical_cluster_type")
        layer = getattr(layer_raw, "value", layer_raw) or ""
        physical: list[str] = []
        nodes: list[str] = []

        def _add(desc: str, host: str) -> None:
            if desc and desc not in physical:
                physical.append(desc)
            if host and host not in nodes:
                nodes.append(host)

        for p in v.get("physical_pods") or []:
            _add(_pod_desc(p), _landing(p))
        for sv in sci_views:
            if (sv.get("serverless_resource") or {}).get("name") != res_name:
                continue
            _add(_pod_desc(sv.get("sci_pod") or {}),
                 _landing(sv.get("sci_pod") or {}))
        existing = {d.split("（")[0] for d in physical}
        for kv in kmc_views:
            if (kv.get("serverless_resource") or {}).get("name") != res_name:
                continue
            owner = kv.get("owner_kmc_deployment")
            if owner:
                oref = _resource_ref(owner)
                _add(f"{oref.get('kind', 'Deployment')}/{oref.get('name', '')}", "")
                continue
            bare = ((kv.get("kmc_resource") or {}).get("name")) or ""
            if bare and bare in existing:
                continue
            _add(_ref(kv.get("kmc_resource")), "")
        rows.append(
            f"| {logical} | {layer} | {'<br>'.join(physical) or '—'} "
            f"| {'/'.join(nodes) or '—'} |"
        )
    etcd_hosts: list[str] = []
    for ev in topology.get("etcd_views") or []:
        h = _landing(ev.get("etcd_member") or {})
        if h and h not in etcd_hosts:
            etcd_hosts.append(h)
    if etcd_hosts:
        rows.append(f"| 共享 etcd | shared_etcd | etcd 集群 "
                    f"| {'/'.join(etcd_hosts)} |")
    if not rows:
        return ""
    m = topology.get("mapping") or {}
    head = ""
    if m.get("serverless_cluster"):
        parts = [f"{k}={v}" for k, v in
                 (("KMC", m.get("kmc_cluster")), ("SCI", m.get("sci_cluster")))
                 if v]
        head = (f"逻辑集群 **{m['serverless_cluster']}**"
                + (f" → {'，'.join(parts)}" if parts else "") + "\n\n")
    return (head
            + "| 逻辑资源 | 物理层 | 物理对象 | 落点节点 |\n"
            + "|---|---|---|---|\n" + "\n".join(rows))


def expert_view(topology: dict, view_key: str) -> dict:
    """Full view payload for a deep expert (Pod details kept)."""
    return {"mapping": _strip_none(topology.get("mapping") or {}), view_key: topology.get(view_key, []) or []}
