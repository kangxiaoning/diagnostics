from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

TAG_RE = re.compile(r"<next_task>(.*?)</next_task>", re.DOTALL)


class StepStatus(Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    ERROR = "error"


class NodeType(Enum):
    ROOT = "root"
    PHASE = "phase"  # one LLM-call round
    TOOL = "tool"    # tool execution result


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
    """Reactive tree driven by the Agent ↔ LLM interaction cycle.

    Every round:
      - tools-complete  → create phase node (before NEXT LLM call)
      - LLM returns     → update phase title from tool names (after LLM result)
      - tool nodes      → children of the phase

    Titles come from actual tool names + optional <next_task> tags,
    never from hardcoded defaults.
    """

    nodes: dict[str, TreeNode] = field(default_factory=dict)
    node_order: list[str] = field(default_factory=list)
    state: str = "init"  # init | thinking | executing | answering | done
    think_segments: list[str] = field(default_factory=list)
    think_buffer: list[str] = field(default_factory=list)
    answer_buffer: list[str] = field(default_factory=list)
    seen_tool_call_ids: set[str] = field(default_factory=set)
    seen_next_tasks: set[str] = field(default_factory=set)
    _counter: int = 0

    # ── tree navigation ──
    _current_phase_id: str = ""       # phase collecting tools right now
    _prev_phase_id: str = "root"      # parent for the NEXT phase
    _awaiting_phase: bool = True      # need to create phase on next tool_call
    _pending_phase_title: str = ""    # from <next_task>, used when creating phase

    # ── tag parsing buffer ──
    _tag_buf: str = ""

    # ── public API ──

    def start(self) -> list[dict[str, Any]]:
        root = TreeNode("root", "开始诊断", None, StepStatus.COMPLETED, NodeType.ROOT)
        self._add_node(root)
        self.state = "thinking"
        return [self._snapshot_event()]

    def handle_token(self, text: str) -> list[dict[str, Any]]:
        """Route text.  Also extract <next_task> for naming the next phase."""
        if self.state == "done":
            return []

        # Extract <next_task> tags from streaming text
        clean, next_task = self._parse_tags(text)
        if next_task:
            self._pending_phase_title = next_task

        if self.state in ("thinking", "executing"):
            self.think_buffer.append(clean)
            return [{"type": "think_token", "text": clean}]

        if self.state == "answering":
            self.answer_buffer.append(clean)
            return [{"type": "token", "text": clean}]

        return []

    def handle_tool_call(self, tool_calls: list[dict]) -> list[dict[str, Any]]:
        """Before LLM call: ensure phase exists.
        After LLM result: update phase title from tool names, create tool nodes.
        """
        new_calls = self._filter_new(tool_calls)
        if not new_calls:
            return []

        if self.state in ("thinking", "answering"):
            self.state = "executing"
            self._flush_think()

        # ── Before LLM: create phase if waiting for one ──
        if self._awaiting_phase:
            self._create_phase()
            self._awaiting_phase = False
        elif not self._current_phase_id or self._current_phase_id not in self.nodes:
            self._create_phase()

        # ── After LLM: update phase title from what the LLM actually decided ──
        tool_purposes = list(dict.fromkeys(self._tool_title(c) for c in new_calls))
        self._set_phase_title(tool_purposes)

        # Complete phase: its LLM response arrived
        phase = self.nodes[self._current_phase_id]
        if phase.status == StepStatus.RUNNING:
            phase.status = StepStatus.COMPLETED

        # ── Create tool nodes under phase ──
        for call, purpose in zip(new_calls, tool_purposes):
            nid = self._next_id()
            tool = TreeNode(nid, purpose, self._current_phase_id,
                            StepStatus.RUNNING, NodeType.TOOL)
            self._add_node(tool)

        return [self._snapshot_event()]

    def handle_update(self, _data: dict, namespace: list[str]) -> list[dict[str, Any]]:
        """Tools completed → mark tools done, prepare for next LLM round."""
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

        # ── Before next LLM: flag that a new phase should be created ──
        self._awaiting_phase = True
        # Assume final round; if more tools arrive, flipped back in handle_tool_call
        self.state = "answering"

        return [self._snapshot_event()]

    def finalize(self) -> list[dict[str, Any]]:
        for node in self.nodes.values():
            if node.status == StepStatus.RUNNING:
                node.status = StepStatus.COMPLETED

        self._flush_think()

        fallback: list[dict[str, Any]] = []
        if not self.answer_buffer:
            fb = "".join(self.think_segments)
            if fb.strip():
                fallback.append({"type": "token", "text": fb})

        self.state = "done"
        return fallback + [self._snapshot_event()]

    # ── helpers ──

    def _create_phase(self) -> str:
        """Create a phase node.  Title: from <next_task> or templated from tool info later."""
        title = self._pending_phase_title or "正在分析…"
        self._pending_phase_title = ""
        nid = self._next_id()
        phase = TreeNode(nid, title, self._prev_phase_id,
                         StepStatus.RUNNING, NodeType.PHASE)
        self._add_node(phase)
        self._prev_phase_id = nid
        self._current_phase_id = nid
        return nid

    def _set_phase_title(self, tool_purposes: list[str]) -> None:
        """Update phase title based on what the LLM actually called.
        Only overwrites the generic placeholder — preserves <next_task> titles.
        """
        if not self._current_phase_id or self._current_phase_id not in self.nodes:
            return

        phase = self.nodes[self._current_phase_id]
        # If already named by <next_task>, keep it
        if phase.title != "正在分析…":
            return

        if len(tool_purposes) == 1:
            phase.title = tool_purposes[0]
        elif len(tool_purposes) <= 3:
            phase.title = "执行: " + " + ".join(tool_purposes)
        else:
            phase.title = "批量诊断"

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

    def _parse_tags(self, text: str) -> tuple[str, str | None]:
        """Extract <next_task> from accumulated text."""
        self._tag_buf += text
        next_task = None
        for match in TAG_RE.finditer(self._tag_buf):
            content = match.group(1).strip()
            if content and content not in self.seen_next_tasks:
                next_task = content
                self.seen_next_tasks.add(content)
        clean = TAG_RE.sub("", text)
        clean = clean.replace("<next_task>", "").replace("</next_task>", "")
        return clean, next_task

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
