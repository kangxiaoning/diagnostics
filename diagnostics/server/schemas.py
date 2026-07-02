from typing import Any

from pydantic import BaseModel, Field


class DiagnosticScope(BaseModel):
    """Pre-discovered diagnostic scope — limits what resources tools can query.

    Assembled server-side by querying the cluster for the target
    workload's pods and extracting node/host metadata.
    """

    cluster_name: str = Field(..., min_length=1, description="K8s 集群名称")
    allowed_namespace: str = Field(..., min_length=1, description="允许查询的 namespace")
    allowed_nodename: list[str] = Field(
        default_factory=list,
        description="允许查询的节点名称列表（从 Pod.spec.nodeName 提取）",
    )
    allowed_hostname: list[str] = Field(
        default_factory=list,
        description="允许查询的主机名称列表（从 Pod.status.hostIP 提取）",
    )
    allowed_podname: list[str] = Field(
        default_factory=list,
        description="允许查询的 Pod 名称列表",
    )
    start_time: str = Field(default="", description="诊断时间窗口开始")
    end_time: str = Field(default="", description="诊断时间窗口结束")


class ChatRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=8000)
    session_id: str | None = None
    entity_type: str = Field(
        default="",
        description="Entity category: 'hosts' or 'kubernetes'. Empty means auto-detect.",
    )
    entity_name: str = Field(
        default="",
        description="Entity identifier: hostname or cluster name. Empty means auto-detect.",
    )
    param_overrides: dict[str, Any] | None = Field(
        default=None,
        description=(
            "Optional per-session tool parameter overrides.  Keys that match "
            "tool parameter names will have their values replaced (e.g. "
            "cluster_name, namespace, pod_name, name_chunk, start_time, end_time)."
        ),
    )
    diagnostic_scope: DiagnosticScope | None = Field(
        default=None,
        description=(
            "Optional pre-discovered diagnostic scope.  When provided, "
            "ScopeGuardMiddleware enforces that tool calls only access "
            "resources within this scope (namespace, node, pod)."
        ),
    )
