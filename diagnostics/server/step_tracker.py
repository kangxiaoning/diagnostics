from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

# Regex to extract <next_task>...</next_task> tags from LLM output
TAG_RE = re.compile(r"<next_task>(.*?)</next_task>", re.DOTALL)


class StepStatus(Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    ERROR = "error"


class NodeType(Enum):
    ROOT = "root"
    ANTICIPATION = "anticipation"  # created before LLM call
    TOOL = "tool"  # tool execution result
    RESULT = "result"  # final conclusion


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
    """State machine that builds a dynamic diagnosis tree.

    Tree structure:
        开始诊断 (root, completed)
        └── 诊断计划 (anticipation, completed)
            ├── 系统概览诊断 (tool, completed)
            ├── CPU诊断 (tool, completed)
            └── 深度诊断分析 (anticipation, running) ← from <next_task>
                ├── 网络诊断 (tool, running)
                └── ...
    """

    steps: dict[str, Step] = field(default_factory=dict)
    step_order: list[str] = field(default_factory=list)
    phase: str = "init"  # init | planning | executing | answering | done
    think_segments: list[str] = field(default_factory=list)
    think_buffer: list[str] = field(default_factory=list)
    answer_buffer: list[str] = field(default_factory=list)
    seen_tool_call_ids: set[str] = field(default_factory=set)
    seen_next_tasks: set[str] = field(default_factory=set)
    _step_counter: int = 0

    # Tree navigation
    current_parent_id: str = ""  # where new tool nodes attach
    pending_next_task: str | None = None  # from <next_task>, creates next anticipation
    _think_text_buf: str = ""  # accumulated think text for tag parsing
    _is_first_tool_batch: bool = True

    def _next_id(self) -> str:
        self._step_counter += 1
        return f"step_{self._step_counter}"

    # ── lifecycle ──

    def start(self) -> list[dict[str, Any]]:
        """Initialize the tree with root + first anticipation node."""
        events: list[dict[str, Any]] = []

        # Root node
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

        # First anticipation: "诊断计划" (before first LLM call)
        plan_id = self._next_id()
        plan = Step(plan_id, "诊断计划", "root", StepStatus.RUNNING, NodeType.ANTICIPATION)
        self.steps[plan_id] = plan
        self.step_order.append(plan_id)
        self.current_parent_id = plan_id
        events.append(
            {
                "type": "step_start",
                "step_id": plan_id,
                "title": "诊断计划",
                "parent_id": "root",
                "status": "running",
                "node_type": "anticipation",
            }
        )

        self.phase = "planning"
        return events

    # ── token handling with tag parsing ──

    def handle_token(self, text: str) -> list[dict[str, Any]]:
        """Process token: parse <next_task> tags, route to think or answer."""
        events: list[dict[str, Any]] = []

        if self.phase == "done":
            return events

        # Parse for <next_task> tags
        clean_text, next_task = self._parse_tags(text)
        if next_task:
            self.pending_next_task = next_task
            # Don't emit next_task event yet - wait until tools complete
            # to create the anticipation node

        if self.phase in ("planning", "executing"):
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
        """Create tool nodes under the current anticipation node."""
        events: list[dict[str, Any]] = []

        # Filter duplicates
        new_calls: list[dict] = []
        for call in tool_calls:
            call_id = call.get("id", "")
            if call_id and call_id in self.seen_tool_call_ids:
                continue
            if call_id:
                self.seen_tool_call_ids.add(call_id)
            new_calls.append(call)

        if not new_calls:
            return events

        # Transition planning → executing on first tool call batch
        if self._is_first_tool_batch and self.phase == "planning":
            self._is_first_tool_batch = False
            self.phase = "executing"

            # Flush think segment
            if self.think_buffer:
                think_text = "".join(self.think_buffer)
                self.think_segments.append(think_text)
                events.append({"type": "think_end", "text": think_text})
                self.think_buffer = []

            # Complete the planning anticipation node
            if self.current_parent_id in self.steps:
                step = self.steps[self.current_parent_id]
                step.status = StepStatus.COMPLETED
                events.append(
                    {
                        "type": "step_end",
                        "step_id": self.current_parent_id,
                        "status": "completed",
                    }
                )

        # Create tool nodes under current_parent
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

        return events

    # ── update handling ──

    def handle_update(self, _data: dict, namespace: list[str]) -> list[dict[str, Any]]:
        """Handle node completion. Detect tools completion, create next anticipation."""
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

        # Flush think buffer (reasoning between tool calls)
        if self.think_buffer:
            think_text = "".join(self.think_buffer)
            self.think_segments.append(think_text)
            events.append({"type": "think_end", "text": think_text})
            self.think_buffer = []

        # Check if there's a pending next_task → create next anticipation node
        if self.pending_next_task:
            next_title = self._format_next_task_title(self.pending_next_task)
            next_id = self._next_id()
            old_parent = self.current_parent_id
            next_step = Step(
                next_id, next_title, old_parent, StepStatus.RUNNING, NodeType.ANTICIPATION
            )
            self.steps[next_id] = next_step
            self.step_order.append(next_id)
            self.current_parent_id = next_id
            events.append(
                {
                    "type": "step_start",
                    "step_id": next_id,
                    "title": next_title,
                    "parent_id": old_parent,
                    "status": "running",
                    "node_type": "anticipation",
                }
            )
            self.pending_next_task = None
            self._is_first_tool_batch = True
            # Stay in executing - more LLM + tools rounds may follow
        else:
            # No next task → final answer phase
            self.phase = "answering"

            # Complete current anticipation node
            if self.current_parent_id in self.steps:
                step = self.steps[self.current_parent_id]
                if step.status == StepStatus.RUNNING:
                    step.status = StepStatus.COMPLETED
                    events.append(
                        {
                            "type": "step_end",
                            "step_id": self.current_parent_id,
                            "status": "completed",
                        }
                    )

        return events

    # ── finalize ──

    def finalize(self) -> list[dict[str, Any]]:
        """Emit remaining events and finalize all steps."""
        events: list[dict[str, Any]] = []

        # Complete any remaining running steps
        for step in self.steps.values():
            if step.status == StepStatus.RUNNING:
                step.status = StepStatus.COMPLETED
                events.append(
                    {"type": "step_end", "step_id": step.step_id, "status": "completed"}
                )

        # Flush remaining think buffer
        if self.think_buffer:
            think_text = "".join(self.think_buffer)
            self.think_segments.append(think_text)
            events.append({"type": "think_end", "text": think_text})
            self.think_buffer = []

        # Flush any remaining tags
        _, next_task = self._parse_tags("")
        if next_task and self.pending_next_task is None:
            self.pending_next_task = next_task

        # If no answer was produced, use think content as answer
        if not self.answer_buffer:
            fallback = "".join(self.think_segments)
            if fallback.strip():
                events.append({"type": "token", "text": fallback})

        self.phase = "done"
        return events

    # ── tree snapshot ──

    def get_tree_snapshot(self) -> list[dict[str, Any]]:
        """Return the full step tree for the frontend."""
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

    # ── helpers ──

    def _parse_tags(self, text: str) -> tuple[str, str | None]:
        """Parse <next_task> tags from accumulated text.
        Returns (clean_text_to_display, next_task_content_or_None).
        """
        self._think_text_buf += text

        # Extract complete tags
        next_task = None
        for match in TAG_RE.finditer(self._think_text_buf):
            content = match.group(1).strip()
            if content and content not in self.seen_next_tasks:
                next_task = content
                self.seen_next_tasks.add(content)

        # Clean for display: strip all next_task tags and partial fragments
        clean = TAG_RE.sub("", text)
        clean = clean.replace("<next_task>", "").replace("</next_task>", "")
        return clean, next_task

    @staticmethod
    def _is_tools_update(namespace: list[str]) -> bool:
        return any("tools" in str(n).lower() for n in namespace)

    @staticmethod
    def _format_next_task_title(task: str) -> str:
        """Format next_task content into a concise tree node title."""
        # Truncate long task descriptions
        if len(task) > 30:
            task = task[:28] + "…"
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
