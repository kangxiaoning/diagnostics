"""Scene configuration registry (SceneProfile) — scene differences converge into data.

Design: scenario-framework (design document §4-§5).  Every scene's differences
(input schema, entity type, expert set, UNDERSTAND argus coverage, topology
query, middleware, system prompt ref, enabled state) collapse into one
SceneProfile entry.  The diagnosis state machine stays scene-agnostic.

Scene naming convention (design document): scenario specs live in the local
spec ecosystem; code only references this registry by scene_id + cluster_type.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class InputField:
    """One scene input item definition (schema for validation/rendering)."""

    name: str
    label: str
    required: bool = True
    type: str = "str"  # "str" | "time_range" | "list"


@dataclass(frozen=True)
class SceneProfile:
    """Scene → Agent assembly configuration."""

    scene_id: str
    label: str
    cluster_type: str = ""
    entity_type: str = ""  # "host" | "kubernetes" (report archiving / filtering)
    input_fields: tuple[InputField, ...] = ()
    experts: tuple[str, ...] = ()          # bound subagent names
    understand_argus: tuple[str, ...] = ()  # UNDERSTAND coverage-gate argus experts
    topology_query: bool = False           # needs environment topology (RelationGraph)
    middleware: tuple[str, ...] = ()        # extra middleware ids (reserved)
    system_prompt_ref: str = ""            # dedicated system prompt template ("" = default)
    enabled: bool = True


# ── Scene input schemas ──
_SINGLE_HOST_FIELDS = (
    InputField("hostname", "主机名称"),
    InputField("time_range", "时间范围", type="time_range"),
    InputField("description", "异常描述", required=False),
)

_SINGLE_POD_FIELDS = (
    InputField("cluster_name", "集群名称"),
    InputField("namespace", "命名空间"),
    InputField("workload_type", "负载类型", required=False),
    InputField("workload_name", "负载名称", required=False),
    InputField("pod_name", "Pod 名称"),
    InputField("time_range", "时间范围", type="time_range"),
    InputField("description", "异常描述", required=False),
)

# 场景 3-5 预留（注册但 disabled，前端不可选）
_SINGLE_CLUSTER_FIELDS = (
    InputField("cluster_name", "集群名称"),
    InputField("time_range", "时间范围", type="time_range"),
    InputField("node_scope", "节点范围", type="list"),
    InputField("description", "异常描述", required=False),
)

_AZ_FIELDS = (
    InputField("availability_zone", "可用区"),
    InputField("time_range", "时间范围", type="time_range"),
    InputField("description", "异常描述", required=False),
)


def _serverless_experts() -> tuple[str, ...]:
    return (
        "serverless-argus-expert", "serverless-expert",
        "kmc-argus-expert", "kmc-expert",
        "sci-argus-expert", "sci-expert",
        "host-argus-expert", "host-expert",
    )


def _serverless_argus() -> tuple[str, ...]:
    """UNDERSTAND 必委派四专家：逻辑/控制面/数据面/物理主机四视角各一。

    Serverless 工作负载运行在 SCI 物理集群节点上，主机（节点）资源是底层
    排查维度（如场景 40 SCI 节点压力）；host-argus-expert 采集分项主机指标
    （CPU/内存/磁盘/网络），与 kmc/sci argus 的 node 概览互补。
    """
    return (
        "serverless-argus-expert", "kmc-argus-expert",
        "sci-argus-expert", "host-argus-expert",
    )


# ── Scene registry: (scene_id, cluster_type) → SceneProfile ──
SCENE_REGISTRY: dict[tuple[str, str], SceneProfile] = {
    ("single_host", ""): SceneProfile(
        scene_id="single_host",
        label="单主机",
        entity_type="host",
        input_fields=_SINGLE_HOST_FIELDS,
        # gpu-expert removed in v3.9.0 — GPU tools/skills merged into the
        # unified host-expert (GPUs live on physical nodes, so container-
        # scene GPU checks also route to host-expert).
        experts=("host-argus-expert", "host-expert"),
        understand_argus=("host-argus-expert",),
    ),
    ("single_pod", "dedicated"): SceneProfile(
        scene_id="single_pod",
        label="单 Pod",
        cluster_type="dedicated",
        entity_type="kubernetes",
        input_fields=_SINGLE_POD_FIELDS,
        experts=("host-argus-expert", "k8s-argus-expert", "host-expert", "k8s-expert"),
        understand_argus=("host-argus-expert", "k8s-argus-expert"),
    ),
    ("single_pod", "serverless"): SceneProfile(
        scene_id="single_pod",
        label="单 Pod",
        cluster_type="serverless",
        entity_type="kubernetes",
        input_fields=_SINGLE_POD_FIELDS,
        experts=_serverless_experts(),
        understand_argus=_serverless_argus(),
        topology_query=True,
        system_prompt_ref="serverless",
    ),
    # ── Scenes 3-5: reserved extension points (registered, disabled) ──
    ("single_cluster", ""): SceneProfile(
        scene_id="single_cluster",
        label="单集群",
        input_fields=_SINGLE_CLUSTER_FIELDS,
        enabled=False,
    ),
    ("multi_cluster", ""): SceneProfile(
        scene_id="multi_cluster",
        label="多集群",
        input_fields=_AZ_FIELDS,
        enabled=False,
    ),
    ("multi_host", ""): SceneProfile(
        scene_id="multi_host",
        label="多主机",
        input_fields=_AZ_FIELDS,
        enabled=False,
    ),
}

# 可选的 cluster_type 集合（single_pod 场景）
SINGLE_POD_CLUSTER_TYPES: tuple[str, ...] = ("dedicated", "serverless")


# ── Expert → view-phrase registry (design document parallel-delegation §5 D3) ──
# Static knowledge: which observability VIEW each expert covers.  Same view
# shares the same phrase so complementary-candidate computation can group by
# view (uncovered views first) and tiebreak deep-before-argus within a view
# ("-argus" in the name marks the inspection class).
EXPERT_VIEW: dict[str, str] = {
    # Serverless four views (argus inspection + deep analysis per view)
    "serverless-expert": "Serverless 逻辑面",
    "serverless-argus-expert": "Serverless 逻辑面",
    "kmc-expert": "KMC 数据面",
    "kmc-argus-expert": "KMC 数据面",
    "sci-expert": "SCI 控制面",
    "sci-argus-expert": "SCI 控制面",
    "host-expert": "物理主机",
    "host-argus-expert": "物理主机",
    # Dedicated cluster
    "k8s-expert": "K8s 控制面",
    "k8s-argus-expert": "K8s 控制面",
    # (gpu-expert removed in v3.9.0 — GPU view merged into "物理主机":
    # GPU checks run inside the unified host-expert, which covers the
    # physical-host view.)
}


def resolve_profile(scene_id: str, cluster_type: str = "") -> SceneProfile | None:
    """Resolve a SceneProfile by scene_id + cluster_type.

    ``single_pod`` defaults to ``dedicated`` when cluster_type is missing
    (legacy frontend compatibility).  Unknown combinations return None.
    """
    key = (scene_id, cluster_type or ("dedicated" if scene_id == "single_pod" else ""))
    return SCENE_REGISTRY.get(key)


def list_enabled_scenes() -> list[dict[str, object]]:
    """Return enabled scenes for frontend discovery (id/label/cluster_types)."""
    out: list[dict[str, object]] = []
    for (sid, ctype), p in SCENE_REGISTRY.items():
        if not p.enabled:
            continue
        entry: dict[str, object] = {"scene_id": sid, "label": p.label, "cluster_type": ctype}
        if sid == "single_pod":
            entry["cluster_types"] = list(SINGLE_POD_CLUSTER_TYPES)
        out.append(entry)
    return out
