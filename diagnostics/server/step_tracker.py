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


class Phase(Enum):
    PLANNING = "planning"
    EXECUTING = "executing"
    ANSWERING = "answering"
    DONE = "done"


@dataclass
class Step:
    step_id: str
    title: str
    parent_id: str | None
    status: StepStatus = StepStatus.PENDING
    detail: str = ""


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
class StepTracker:
    steps: dict[str, Step] = field(default_factory=dict)
    step_order: list[str] = field(default_factory=list)
    phase: Phase = Phase.PLANNING
    think_segments: list[str] = field(default_factory=list)
    think_buffer: list[str] = field(default_factory=list)
    answer_buffer: list[str] = field(default_factory=list)
    seen_tool_call_ids: set[str] = field(default_factory=set)
    _step_counter: int = 0

    def _next_id(self) -> str:
        self._step_counter += 1
        return f"step_{self._step_counter}"

    def _ensure_plan_step(self) -> str | None:
        if "plan" not in self.steps:
            step = Step("plan", "分析问题并制定诊断计划", None, StepStatus.RUNNING)
            self.steps["plan"] = step
            self.step_order.append("plan")
            return "plan"
        return None

    def handle_token(self, text: str) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []

        if self.phase == Phase.PLANNING:
            plan_id = self._ensure_plan_step()
            if plan_id:
                events.append(
                    {
                        "type": "step_start",
                        "step_id": plan_id,
                        "title": "分析问题并制定诊断计划",
                        "parent_id": None,
                        "status": "running",
                    }
                )
            self.think_buffer.append(text)
            events.append({"type": "think_token", "text": text})
            return events

        if self.phase == Phase.EXECUTING:
            self.think_buffer.append(text)
            events.append({"type": "think_token", "text": text})
            return events

        if self.phase == Phase.ANSWERING:
            self.answer_buffer.append(text)
            events.append({"type": "token", "text": text})
            return events

        return events

    def handle_tool_call(self, tool_calls: list[dict]) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []

        # Filter out duplicate tool calls
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

        # Transition from PLANNING → EXECUTING
        if self.phase == Phase.PLANNING:
            self._ensure_plan_step()
            self.phase = Phase.EXECUTING
            # Flush think segment
            if self.think_buffer:
                think_text = "".join(self.think_buffer)
                self.think_segments.append(think_text)
                events.append({"type": "think_end", "text": think_text})
                self.think_buffer = []
            # Complete plan step
            if "plan" in self.steps:
                self.steps["plan"].status = StepStatus.COMPLETED
                events.append(
                    {"type": "step_end", "step_id": "plan", "status": "completed"}
                )

        # Create steps for new tool calls
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
            step = Step(step_id, title, "plan", StepStatus.RUNNING)
            self.steps[step_id] = step
            self.step_order.append(step_id)
            events.append(
                {
                    "type": "step_start",
                    "step_id": step_id,
                    "title": title,
                    "parent_id": "plan",
                    "status": "running",
                }
            )

        return events

    def handle_update(self, _data: dict, namespace: list[str]) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []

        # If update comes from a tools node, mark running steps as completed
        if any("tools" in str(n) for n in namespace):
            completed_any = False
            for step in self.steps.values():
                if step.status == StepStatus.RUNNING:
                    step.status = StepStatus.COMPLETED
                    events.append(
                        {"type": "step_end", "step_id": step.step_id, "status": "completed"}
                    )
                    completed_any = True

            if completed_any and self.phase == Phase.EXECUTING:
                # Transition to answering - next agent output will be the final answer
                if self.think_buffer:
                    think_text = "".join(self.think_buffer)
                    self.think_segments.append(think_text)
                    events.append({"type": "think_end", "text": think_text})
                    self.think_buffer = []
                self.phase = Phase.ANSWERING

        return events

    def handle_agent_message(self, text: str, has_tool_calls: bool) -> list[dict[str, Any]]:
        """Handle a complete agent message (non-streaming context)."""
        if has_tool_calls:
            return []
        # If agent produced text without tool calls after tools ran, it's the final answer
        if self.phase == Phase.EXECUTING:
            self.phase = Phase.ANSWERING
        return [{"type": "token", "text": text}]

    def finalize(self) -> list[dict[str, Any]]:
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

        # If no answer was produced (all think, no final answer), flush think as answer
        if not self.answer_buffer and self.think_segments:
            all_think = "".join(self.think_segments)
            events.append({"type": "token", "text": all_think})

        self.phase = Phase.DONE
        return events

    def get_tree_snapshot(self) -> list[dict[str, Any]]:
        """Return the full step tree for the frontend."""
        return [
            {
                "id": step.step_id,
                "title": step.title,
                "parent_id": step.parent_id,
                "status": step.status.value,
                "detail": step.detail,
            }
            for step_id in self.step_order
            if step_id in self.steps
            for step in [self.steps[step_id]]
        ]

    def get_think_content(self) -> str:
        return "".join(self.think_segments)

    @staticmethod
    def _tool_to_title(tool_name: str, args: dict) -> str:
        if tool_name == "run_diagnostic_profile":
            profile = str(args.get("profile", "")).strip().lower()
            return PROFILE_NAME_MAP.get(profile, f"执行诊断: {profile}")
        if tool_name == "read_proc_file":
            path = str(args.get("path", ""))
            return f"读取系统文件: {path}"
        if tool_name == "explain_available_diagnostics":
            return "查询可用诊断工具"
        return f"执行: {tool_name}"
