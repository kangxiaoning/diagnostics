from typing import Any

from pydantic import BaseModel, Field


class ChatRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=8000)
    session_id: str | None = None
    entity_type: str = Field(
        default="",
        description="Entity category: 'host' or 'kubernetes'. Empty means auto-detect.",
    )
    entity_name: str = Field(
        default="",
        description=(
            "Legacy generic entity identifier (deprecated alias): hostname or "
            "cluster name.  Prefer the semantic fields host_name / cluster_name "
            "below — entity_name is still accepted for backward compatibility "
            "(legacy clients / auto-detect path) and is derived from the "
            "semantic fields when provided."
        ),
    )
    host_name: str = Field(
        default="",
        description=(
            "Semantic entity identifier for host-type scenes (single_host): "
            "the hostname under diagnosis.  Mutually exclusive with "
            "cluster_name; mirrors OpenTelemetry's type-specific entity "
            "identification attributes (host.name) instead of a generic "
            "entity name."
        ),
    )
    cluster_name: str = Field(
        default="",
        description=(
            "Semantic entity identifier for cluster-type scenes "
            "(single_pod / single_cluster): the cluster under diagnosis.  "
            "Mutually exclusive with host_name; mirrors OpenTelemetry's "
            "type-specific entity identification attributes "
            "(k8s.cluster.name)."
        ),
    )
    scene_id: str | None = Field(
        default=None,
        description=(
            "Scene identifier: single_host / single_pod / ...  "
            "Empty means legacy auto-detect (no scene framework)."
        ),
    )
    cluster_type: str | None = Field(
        default=None,
        description="Cluster type for the single_pod scene: 'dedicated' or 'serverless'.",
    )
    param_overrides: dict[str, Any] | None = Field(
        default=None,
        description=(
            "Per-session tool parameter overrides provided by the frontend.  "
            "Keys that match tool parameter names are unconditionally "
            "replaced in all tool calls (e.g. cluster_name, namespace, "
            "pod_name, hostname, start_time, end_time)."
        ),
    )
