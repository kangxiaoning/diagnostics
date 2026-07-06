from typing import Any

from pydantic import BaseModel, Field


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
            "Per-session tool parameter overrides provided by the frontend.  "
            "Keys that match tool parameter names are unconditionally "
            "replaced in all tool calls (e.g. cluster_name, namespace, "
            "pod_name, hostname, start_time, end_time)."
        ),
    )
