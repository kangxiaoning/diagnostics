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
    ANTICIPATION = "anticipation"  # pre-LLM node (level-3,5,7...)
    TOOL = "tool"  # tool / next_task result node (level-2,4,6...)


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
class Step:
    step_id: str
    title: str
    parent_id: str | None
    status: StepStatus = StepStatus.PENDING
    node_type: NodeType = NodeType.TOOL
    detail: str = ""


@dataclass
class StepTracker:
    """Builds a dynamic diagnosis tree.

    Expected tree structure:

        开始诊断 (root, level-1)
        ├── [LLM #1 return] 生成诊断计划  (level-2)
        ├── [LLM #1 return] 获取系统基础信息 (level-2)
        ├── [LLM #1 return] 分析CPU状态 (level-2)
        └── [pre-LLM #2] 执行诊断计划 (level-3, anticiptation)
            ├── [LLM #2 return] 检查CPU负载 (level-4)
            └── [pre-LLM #3] 综合分析 (level-5, anticiptation)
                └── [LLM #3 return] 生成报告 (level-6)

    Every round:
      - Before LLM: anticipation node (pre-LLM) created from prev round's <next_task>
      - After LLM: tool / next_task result nodes become children of the anticipation
    """

    steps: dict[str, Step] = field(default_factory=dict)
    step_order: list[str] = field(default_factory=list)
    phase: str = "init"  # init | executing | answering | done
    think_segments: list[str] = field(default_factory=list)
    think_buffer: list[str] = field(default_factory=list)
    answer_buffer: list[str] = field(default_factory=list)
    seen_tool_call_ids: set[str] = field(default_factory=set)
    seen_next_tasks: set[str] = field(default_factory=set)
    _step_counter: int = 0

    # Tree navigation
    current_parent_id: str = "root"  # where new result nodes attach
    current_anticipation_id: str | None = None  # latest pre-LLM node
    pending_next_task: str | None = None  # from <next_task>, used for next anticipation
    _think_text_buf: str = ""
    _has_tool_calls: bool = False  # whether any tool calls arrived this request
    _first_token: bool = True  # first batch of tokens

    def _next_id(self) -> str:
        self._step_counter += 1
        return f"step_{self._step_counter}"

    # ── lifecycle ──

    def start(self) -> list[dict[str, Any]]:
        """Create root node only."""
        events: list[dict[str, Any]] = []

        root = Step("root", "开始诊断", None, StepStatus.COMPLETED, NodeType.ROOT)
        self.steps["root"] = root
        self.step_order.append("root")
        events.append(
            {
                "type": "step_start",
                "step_id": "root",
                "title": "开始诊断",
                "parent_id": None,
                "status": "completed",
                "node_type": "root",
            }
        )

        self.phase = "init"
        return events

    # ── token handling ──

    def handle_token(self, text: str) -> list[dict[str, Any]]:
        """Parse <next_task> tags, route text to think or final answer."""
        events: list[dict[str, Any]] = []

        if self.phase == "done":
            return events

        clean_text, next_task = self._parse_tags(text)
        if next_task:
            self.pending_next_task = next_task

        if self.phase in ("init", "executing"):
            if clean_text:
                self.think_buffer.append(clean_text)
                events.append({"type": "think_token", "text": clean_text})
            return events

        if self.phase == "answering":
            if clean_text:
                self.answer_buffer.append(clean_text)
                events.append({"type": "token", "text": clean_text})
            return events

        return events

    # ── tool call handling ──

    def handle_tool_call(self, tool_calls: list[dict]) -> list[dict[str, Any]]:
        """Create result nodes under current_parent_id.
        On first batch: transition init→executing.
        On subsequent batches: complete the anticipation that preceded this LLM call.
        """
        events: list[dict[str, Any]] = []

        # Filter duplicates
        new_calls = self._filter_new_calls(tool_calls)
        if not new_calls:
            return events

        # First batch: transition init → executing
        if self.phase == "init":
            self.phase = "executing"
            self._flush_think(events)

        # Subsequent batches: complete the anticipation node
        # (the LLM response for this anticipation has arrived)
        if self.current_anticipation_id and self.current_anticipation_id in self.steps:
            step = self.steps[self.current_anticipation_id]
            if step.status == StepStatus.RUNNING:
                step.status = StepStatus.COMPLETED
                events.append(
                    {
                        "type": "step_end",
                        "step_id": self.current_anticipation_id,
                        "status": "completed",
                    }
                )

        # Create tool/result nodes under current_parent
        for call in new_calls:
            tool_name = call.get("name") or call.get("function", {}).get("name", "unknown")
            args = call.get("args") or call.get("function", {}).get("arguments", {})
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except (json.JSONDecodeError, TypeError):
                    args = {}

            title = self._tool_to_title(tool_name, args)
            step_id = self._next_id()
            step = Step(
                step_id, title, self.current_parent_id, StepStatus.RUNNING, NodeType.TOOL
            )
            self.steps[step_id] = step
            self.step_order.append(step_id)
            events.append(
                {
                    "type": "step_start",
                    "step_id": step_id,
                    "title": title,
                    "parent_id": self.current_parent_id,
                    "status": "running",
                    "node_type": "tool",
                }
            )

        self._has_tool_calls = True
        return events

    # ── update handling ──

    def handle_update(self, _data: dict, namespace: list[str]) -> list[dict[str, Any]]:
        """Tools completed: complete tool nodes, create next anticipation."""
        events: list[dict[str, Any]] = []

        if not self._is_tools_update(namespace):
            return events

        # Mark running tool nodes as completed
        completed_any = False
        for step in self.steps.values():
            if step.node_type == NodeType.TOOL and step.status == StepStatus.RUNNING:
                step.status = StepStatus.COMPLETED
                events.append(
                    {"type": "step_end", "step_id": step.step_id, "status": "completed"}
                )
                completed_any = True

        if not completed_any:
            return events

        self._flush_think(events)
        events.extend(self._complete_round())
        return events

    # ── finalize ──

    def finalize(self) -> list[dict[str, Any]]:
        """Emit remaining events, complete all steps."""
        events: list[dict[str, Any]] = []

        # Edge case: no tool calls at all
        # → create pending_next_task as a result node under root
        if not self._has_tool_calls and self.pending_next_task:
            title = self._format_next_task_title(self.pending_next_task)
            nid = self._next_id()
            step = Step(nid, title, "root", StepStatus.COMPLETED, NodeType.TOOL)
            self.steps[nid] = step
            self.step_order.append(nid)
            events.append(
                {
                    "type": "step_start",
                    "step_id": nid,
                    "title": title,
                    "parent_id": "root",
                    "status": "completed",
                    "node_type": "tool",
                }
            )
            self.pending_next_task = None
            self._has_tool_calls = True

        # Complete remaining running steps
        for step in self.steps.values():
            if step.status == StepStatus.RUNNING:
                step.status = StepStatus.COMPLETED
                events.append(
                    {"type": "step_end", "step_id": step.step_id, "status": "completed"}
                )

        self._flush_think(events)

        # Complete final round (force complete: no more LLM calls coming)
        if self.phase not in ("answering", "done"):
            events.extend(self._complete_round(force_complete=True))

        # Flush any remaining tags from buffer
        _, next_task = self._parse_tags("")
        if next_task and self.pending_next_task is None:
            self.pending_next_task = next_task

        # If no answer, use think content as fallback
        if not self.answer_buffer:
            fallback = "".join(self.think_segments)
            if fallback.strip():
                events.append({"type": "token", "text": fallback})

        self.phase = "done"
        return events

    # ── tree snapshot ──

    def get_tree_snapshot(self) -> list[dict[str, Any]]:
        """Return all steps in insertion order for the frontend to render."""
        return [
            {
                "id": step.step_id,
                "title": step.title,
                "parent_id": step.parent_id,
                "status": step.status.value,
                "node_type": step.node_type.value,
                "detail": step.detail,
            }
            for step_id in self.step_order
            if step_id in self.steps
            for step in [self.steps[step_id]]
        ]

    # ── internal helpers ──

    def _complete_round(self, force_complete: bool = False) -> list[dict[str, Any]]:
        """Create next anticipation from pending_next_task, or transition to answering.

        When force_complete=True (called from finalize), skip creating anticipation
        since no more LLM calls are coming.
        """
        events: list[dict[str, Any]] = []

        if self.pending_next_task and not force_complete:
            # Create anticipation node for the next LLM call
            # Parent: previous anticipation, or root if first
            parent = self.current_anticipation_id or "root"
            title = self._format_next_task_title(self.pending_next_task)
            nid = self._next_id()
            step = Step(nid, title, parent, StepStatus.RUNNING, NodeType.ANTICIPATION)
            self.steps[nid] = step
            self.step_order.append(nid)
            self.current_anticipation_id = nid
            self.current_parent_id = nid
            self.pending_next_task = None
            events.append(
                {
                    "type": "step_start",
                    "step_id": nid,
                    "title": title,
                    "parent_id": parent,
                    "status": "running",
                    "node_type": "anticipation",
                }
            )
            # Stay in executing phase for next LLM+Tools round
        else:
            # No more tasks → final answer
            self.phase = "answering"
            if self.current_anticipation_id and self.current_anticipation_id in self.steps:
                step = self.steps[self.current_anticipation_id]
                if step.status == StepStatus.RUNNING:
                    step.status = StepStatus.COMPLETED
                    events.append(
                        {
                            "type": "step_end",
                            "step_id": self.current_anticipation_id,
                            "status": "completed",
                        }
                    )

        return events

    def _flush_think(self, events: list[dict[str, Any]]) -> None:
        if self.think_buffer:
            think_text = "".join(self.think_buffer)
            self.think_segments.append(think_text)
            events.append({"type": "think_end", "text": think_text})
            self.think_buffer = []

    def _filter_new_calls(self, tool_calls: list[dict]) -> list[dict]:
        new_calls: list[dict] = []
        for call in tool_calls:
            call_id = call.get("id", "")
            if call_id and call_id in self.seen_tool_call_ids:
                continue
            if call_id:
                self.seen_tool_call_ids.add(call_id)
            new_calls.append(call)
        return new_calls

    def _parse_tags(self, text: str) -> tuple[str, str | None]:
        """Parse <next_task> from accumulated text.
        Returns (clean_display_text, next_task_content_or_none).
        """
        self._think_text_buf += text

        next_task = None
        for match in TAG_RE.finditer(self._think_text_buf):
            content = match.group(1).strip()
            if content and content not in self.seen_next_tasks:
                next_task = content
                self.seen_next_tasks.add(content)

        # Strip tags for display
        clean = TAG_RE.sub("", text)
        clean = clean.replace("<next_task>", "").replace("</next_task>", "")
        return clean, next_task

    @staticmethod
    def _is_tools_update(namespace: list[str]) -> bool:
        return any("tools" in str(n).lower() for n in namespace)

    @staticmethod
    def _format_next_task_title(task: str) -> str:
        if len(task) > 28:
            task = task[:26] + "…"
        return task

    @staticmethod
    def _tool_to_title(tool_name: str, args: dict) -> str:
        if tool_name == "run_diagnostic_profile":
            profile = str(args.get("profile", "")).strip().lower()
            return PROFILE_NAME_MAP.get(profile, f"执行诊断: {profile}")
        if tool_name == "read_proc_file":
            path = str(args.get("path", ""))
            filename = path.rsplit("/", 1)[-1] if "/" in path else path
            return f"读取: {filename}"
        if tool_name == "explain_available_diagnostics":
            return "查询可用诊断工具"
        return f"执行: {tool_name}"
