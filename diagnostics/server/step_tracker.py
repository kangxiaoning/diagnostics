from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class StepStatus(Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    ERROR = "error"


class NodeType(Enum):
    ROOT = "root"
    PHASE = "phase"  # agent thinking / LLM call round
    TOOL = "tool"    # tool execution


PROFILE_NAME_MAP: dict[str, str] = {
    "overview": "系统概览诊断",
    "cpu": "CPU诊断",
    "memory": "内存诊断",
    "io": "磁盘IO诊断",
    "network": "网络诊断",
    "processes_cpu": "进程CPU诊断",
    "processes_memory": "进程内存诊断",
    "containers": "容器诊断",
}

PHASE_DEFAULTS = [
    "分析问题，制定诊断计划",
    "执行初步诊断",
    "深入分析诊断结果",
    "综合判断",
    "生成诊断结论",
]


@dataclass
class TreeNode:
    id: str
    title: str
    parent_id: str | None
    status: StepStatus = StepStatus.PENDING
    node_type: NodeType = NodeType.TOOL
    detail: str = ""


@dataclass
class TreeBuilder:
    """Reactive tree state machine driven by LangGraph execution events.

    Watches the agent streaming stream and builds a tree that reflects
    the actual LLM→Tools→LLM execution cycle.  Sends a full snapshot
    (`tree_snapshot` event) after every meaningful change.
    """

    nodes: dict[str, TreeNode] = field(default_factory=dict)
    node_order: list[str] = field(default_factory=list)
    phase: str = "init"  # init | thinking | executing | answering | done
    think_segments: list[str] = field(default_factory=list)
    think_buffer: list[str] = field(default_factory=list)
    answer_buffer: list[str] = field(default_factory=list)
    seen_tool_call_ids: set[str] = field(default_factory=set)
    _counter: int = 0

    # Tree navigation
    _current_phase_id: str = ""   # phase node currently collecting tools
    _prev_phase_id: str = "root"  # parent for the NEXT phase (creates nesting)
    _phase_index: int = 0         # which default title to use next
    _awaiting_phase: bool = True  # True when we need to create a new phase on next tool_call

    # ── public API ──

    def start(self) -> list[dict[str, Any]]:
        """Create root node.  Frontend renders the initial tree."""
        root = TreeNode("root", "开始诊断", None, StepStatus.COMPLETED, NodeType.ROOT)
        self._add_node(root)
        self.phase = "thinking"
        return [self._snapshot_event()]

    def handle_token(self, text: str) -> list[dict[str, Any]]:
        """Route text to think display or final answer."""
        if self.phase == "done":
            return []

        if self.phase in ("thinking", "executing"):
            self.think_buffer.append(text)
            return [{"type": "think_token", "text": text}]

        if self.phase == "answering":
            self.answer_buffer.append(text)
            return [{"type": "token", "text": text}]

        return []

    def handle_tool_call(self, tool_calls: list[dict]) -> list[dict[str, Any]]:
        """Agent decided to call tools → create tool nodes under current phase.

        If we're awaiting a new phase (because tools just completed),
        create the phase node first.
        """
        new_calls = self._filter_new(tool_calls)
        if not new_calls:
            return []

        # Transition to executing on (first or subsequent) tool batch
        if self.phase in ("thinking", "answering"):
            self.phase = "executing"
            self._flush_think()

        # Create/complete phase node as needed
        if self._awaiting_phase:
            self._create_phase()
            self._awaiting_phase = False
        elif self._current_phase_id:
            # Ensure phase node exists (robustness)
            self._ensure_phase()

        # Complete current phase: its LLM response arrived, tools are its children
        if self._current_phase_id and self._current_phase_id in self.nodes:
            node = self.nodes[self._current_phase_id]
            if node.status == StepStatus.RUNNING:
                node.status = StepStatus.COMPLETED

        # Create tool nodes under current phase
        for call in new_calls:
            title = self._tool_title(call)
            nid = self._next_id()
            tool = TreeNode(nid, title, self._current_phase_id, StepStatus.RUNNING, NodeType.TOOL)
            self._add_node(tool)

        return [self._snapshot_event()]

    def handle_update(self, _data: dict, namespace: list[str]) -> list[dict[str, Any]]:
        """Node completed.  If tools finished, mark them done."""
        if not self._is_tools(namespace):
            return []

        changed = False
        for node in self.nodes.values():
            if node.node_type == NodeType.TOOL and node.status == StepStatus.RUNNING:
                node.status = StepStatus.COMPLETED
                changed = True

        if not changed:
            return []

        self._flush_think()
        self._awaiting_phase = True
        # Assume this is the last round — switch to answering.
        # If more tool_calls arrive, handle_tool_call will flip back to executing.
        self.phase = "answering"

        return [self._snapshot_event()]

    def finalize(self) -> list[dict[str, Any]]:
        """Stream ended.  Complete remaining nodes, emit final snapshot."""
        # Complete all running nodes
        for node in self.nodes.values():
            if node.status == StepStatus.RUNNING:
                node.status = StepStatus.COMPLETED

        self._flush_think()

        # If no tools were ever called, the tree only has root.
        # Still valid — the agent answered directly.

        # If no answer, use think as fallback
        fallback_events: list[dict[str, Any]] = []
        if not self.answer_buffer:
            fb = "".join(self.think_segments)
            if fb.strip():
                fallback_events.append({"type": "token", "text": fb})

        self.phase = "done"
        return fallback_events + [self._snapshot_event()]

    # ── helpers ──

    def _create_phase(self) -> str:
        """Create a phase node as child of the previous phase (increasing depth)."""
        title = PHASE_DEFAULTS[min(self._phase_index, len(PHASE_DEFAULTS) - 1)]
        self._phase_index += 1
        nid = self._next_id()
        phase = TreeNode(nid, title, self._prev_phase_id, StepStatus.RUNNING, NodeType.PHASE)
        self._add_node(phase)
        self._prev_phase_id = nid  # next phase nests under this one
        self._current_phase_id = nid
        return nid

    def _ensure_phase(self) -> str:
        """Ensure a phase node exists (for edge cases)."""
        if self._current_phase_id and self._current_phase_id in self.nodes:
            return self._current_phase_id
        return self._create_phase()

    def _snapshot_event(self) -> dict[str, Any]:
        return {
            "type": "tree_snapshot",
            "steps": [
                {
                    "id": node.id,
                    "title": node.title,
                    "parent_id": node.parent_id,
                    "status": node.status.value,
                    "node_type": node.node_type.value,
                    "detail": node.detail,
                }
                for nid in self.node_order
                if nid in self.nodes
                for node in [self.nodes[nid]]
            ],
        }

    def _add_node(self, node: TreeNode) -> None:
        self.nodes[node.id] = node
        self.node_order.append(node.id)

    def _next_id(self) -> str:
        self._counter += 1
        return f"n{self._counter}"

    def _flush_think(self) -> None:
        if self.think_buffer:
            self.think_segments.append("".join(self.think_buffer))
            self.think_buffer = []

    def _filter_new(self, tool_calls: list[dict]) -> list[dict]:
        result: list[dict] = []
        for call in tool_calls:
            cid = call.get("id", "")
            if cid and cid in self.seen_tool_call_ids:
                continue
            if cid:
                self.seen_tool_call_ids.add(cid)
            result.append(call)
        return result

    @staticmethod
    def _is_tools(namespace: list[str]) -> bool:
        return any("tools" in str(n).lower() for n in namespace)

    @staticmethod
    def _tool_title(call: dict) -> str:
        name = call.get("name") or call.get("function", {}).get("name", "unknown")
        args = call.get("args") or call.get("function", {}).get("arguments", {})
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except (json.JSONDecodeError, TypeError):
                args = {}
        if name == "run_diagnostic_profile":
            profile = str(args.get("profile", "")).strip().lower()
            return PROFILE_NAME_MAP.get(profile, f"执行诊断: {profile}")
        if name == "read_proc_file":
            path = str(args.get("path", ""))
            filename = path.rsplit("/", 1)[-1] if "/" in path else path
            return f"读取: {filename}"
        if name == "explain_available_diagnostics":
            return "查询可用诊断工具"
        return f"执行: {name}"
