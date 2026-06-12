from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

logger = logging.getLogger(__name__)


def format_tool_args(args: dict, tool_name: str = "") -> str:
    """Format tool arguments for display. Shows key values like paths, namespaces."""
    if not args:
        logger.debug("format_tool_args: empty args for '%s'", tool_name)
        return ""
    logger.debug("format_tool_args: tool='%s' args=%s", tool_name, args)
    # Write_todos: summarize task list
    todos = args.get("todos")
    if isinstance(todos, list) and todos:
        items = []
        for t in todos[:3]:
            if isinstance(t, dict):
                items.append(t.get("content", "")[:40])
            else:
                items.append(str(t)[:40])
        suffix = f" +{len(todos)-3}" if len(todos) > 3 else ""
        return "; ".join(items) + suffix
    # Path/file: show basename for readability
    path = args.get("path") or args.get("file_path") or args.get("filename")
    if isinstance(path, str) and path.strip():
        return path.rsplit("/", 1)[-1] if "/" in path else path
    # Namespace
    ns = args.get("namespace")
    if isinstance(ns, str) and ns.strip():
        return ns
    # Generic fallback
    parts = [str(v).strip() for v in args.values() if str(v).strip() and str(v).strip() != "{}"]
    return ", ".join(parts)


class StepStatus(Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    ERROR = "error"


class NodeType(Enum):
    ROOT = "root"
    PHASE = "phase"
    TOOL = "tool"
    TASK = "task"


TOOL_LABELS: dict[str, str] = {
    "get_system_overview": "获取系统概览",
    "check_cpu": "分析CPU指标",
    "check_memory": "分析内存使用",
    "check_disk": "分析磁盘IO",
    "check_network": "分析网络状态",
    "check_processes": "排查进程状态",
    "check_gpu_health": "检查GPU健康",
    "check_gpu_memory": "检查GPU显存",
    "check_gpu_utilization": "分析GPU利用率",
    "check_kubernetes_control_plane": "检查控制平面",
    "check_kubernetes_pods": "排查Pod状态",
    "check_kubernetes_nodes": "检查节点状态",
    "list_diagnostic_capabilities": "列出诊断工具",
    "write_todos": "规划诊断步骤",
    "read_todos": "查看任务清单",
    "task": "委派专家诊断",
    "read_file": "读取文件内容",
    "write_file": "写入诊断记录",
    "edit_file": "编辑文件内容",
    "ls": "浏览文件目录",
    "glob": "搜索匹配文件",
    "grep": "搜索文件内容",
    "execute": "执行诊断命令",
    # Live K8s tools
    "get_cluster_overview": "获取集群概览",
    "get_pod_logs": "查看Pod日志",
    "describe_pod": "查看Pod详情",
    "get_pod_events": "查看Pod事件",
    "get_node_info": "查看节点详情",
    "get_cluster_events": "查看集群事件",
    "get_pod_resource_usage": "查看Pod资源使用",
    "get_node_resource_usage": "查看节点资源使用",
    "get_pod_logs_since": "查看最近Pod日志",
    "get_pod_logs_lines": "查看Pod日志行数",
    "get_api_resources": "列出API资源",
    "get_api_versions": "列出API版本",
    "get_resource_yaml": "查看资源YAML",
    "explain_resource": "查看资源字段文档",
    "get_pod_previous_logs": "查看Pod崩溃前日志",
    "get_system_pods": "查看系统Pod状态",
    "describe_controller": "查看控制器详情",
    "check_service_endpoints": "检查Service端点",
    "get_configmap": "查看ConfigMap",
    "list_namespace_resources": "列出命名空间资源",
    "get_pv_pvc_status": "查看PV/PVC状态",
    "get_ingress_status": "查看Ingress状态",
    "list_clusters": "列出配置的集群",
    "get_namespaces": "列出命名空间",
}


@dataclass
class TreeNode:
    id: str
    title: str
    parent_id: str | None
    status: StepStatus = StepStatus.PENDING
    node_type: NodeType = NodeType.TOOL
    detail: str = ""
    description: str = ""
    tool_name: str = ""
    tool_args: str = ""
    parent_ids: list[str] = field(default_factory=list)


@dataclass
class TreeBuilder:
    """Reactive tree driven by Agent ↔ LLM interaction cycles.

    Phase creation timing:
      - Each LLM invocation starts a new phase, tracked via round_number.
      - First phase ("第1轮智能分析"): created at tree.start().
      - Subsequent phases: created when round_number changes.

    Tree structure:
      phase ("第1轮智能分析")
        ├── tool ("CPU诊断")
        └── tool ("系统概览")
      phase ("第2轮智能分析")  ← parent_ids from all round-1 tools
        ├── tool ("读取loadavg")
        └── tool ("进程诊断")
    """

    nodes: dict[str, TreeNode] = field(default_factory=dict)
    node_order: list[str] = field(default_factory=list)
    state: str = "init"
    think_segments: list[str] = field(default_factory=list)
    think_buffer: list[str] = field(default_factory=list)
    answer_buffer: list[str] = field(default_factory=list)
    seen_tool_call_ids: set[str] = field(default_factory=set)
    _counter: int = 0

    _current_phase_id: str = ""
    _last_tool_child_ids: list[str] = field(default_factory=list)
    _current_response_text: list[str] = field(default_factory=list)
    _round_descriptions: dict[int, str] = field(default_factory=dict)
    _current_round: int = 0

    # ── Public API ──

    def start(self) -> list[dict[str, Any]]:
        self.state = "thinking"
        self._create_first_phase()
        self._current_round = 1
        logger.info("Tree: started (first phase created)")
        return [self._snapshot_event()]

    def handle_token(self, text: str, round_num: int = 0) -> list[dict[str, Any]]:
        """Feed a text token. Creates a new phase when the LLM round changes."""
        if self.state == "done":
            return []

        result: list[dict[str, Any]] = []
        if round_num > 0 and round_num != self._current_round:
            self._current_round = round_num
            self._create_next_phase()
            self.state = "thinking"
            result.append(self._snapshot_event())

        self._current_response_text.append(text)
        if self.state in ("thinking", "executing"):
            self.think_buffer.append(text)
            result.append({"type": "think_token", "text": text})
            return result
        if self.state == "answering":
            self.answer_buffer.append(text)
            result.append({"type": "token", "text": text})
            return result
        return result

    def handle_tool_call(self, tool_calls: list[dict]) -> list[dict[str, Any]]:
        new_calls = self._filter_new(tool_calls)
        if not new_calls:
            return []

        if self.state in ("thinking", "answering"):
            self.state = "executing"
            self._flush_think()

        parent_id = self._current_phase_id
        parent = self.nodes.get(parent_id)
        if parent and parent.status == StepStatus.RUNNING:
            parent.status = StepStatus.COMPLETED

        created_ids: list[str] = []

        for call in new_calls:
            nid = self._next_id()
            title = self._tool_title(call)
            description = call.get("description", "")
            tool_name = call.get("name", "")
            tool_args = format_tool_args(call.get("args", {}),
                                        call.get("name", ""))
            tool = TreeNode(
                nid, title, parent_id,
                StepStatus.RUNNING, NodeType.TOOL,
                detail=call.get("id", ""),
                description=description,
                tool_name=tool_name,
                tool_args=tool_args,
            )
            self._add_node(tool)
            created_ids.append(nid)

        self._last_tool_child_ids.extend(created_ids)
        self._current_response_text = []
        logger.info("Tree: +%d tool nodes under %s (accumulated: %d)",
                     len(new_calls), parent_id, len(self._last_tool_child_ids))
        return [self._snapshot_event()]

    def handle_update(self, _data: dict, namespace: list[str]) -> list[dict[str, Any]]:
        if not self._is_tool_update(_data, namespace):
            return []

        tc_id = _data.get("tool_call_id", "")
        changed = False

        if tc_id:
            for node in self.nodes.values():
                if (node.node_type == NodeType.TOOL and
                        node.detail == tc_id and
                        node.status == StepStatus.RUNNING):
                    node.status = StepStatus.COMPLETED
                    changed = True
                    break

        if not changed:
            for node in self.nodes.values():
                if node.node_type == NodeType.TOOL and node.status == StepStatus.RUNNING:
                    node.status = StepStatus.COMPLETED
                    changed = True

        has_running = any(
            n.node_type == NodeType.TOOL and n.status == StepStatus.RUNNING
            for n in self.nodes.values()
        )
        if has_running:
            return [self._snapshot_event()]

        if not changed:
            return []

        self._flush_think()
        # Next phase is created on the next LLM invocation (round change in handle_token),
        # not here. This way each phase spans the full LLM interaction round.
        self.state = "answering"
        logger.info("Tree: all tools done, state -> answering")
        return [self._snapshot_event()]

    def update_tool_args(self, tc_id: str, tool_name: str, args: dict) -> list[dict[str, Any]]:
        """Update tool name/args for an existing tool node when streaming args arrive."""
        for node in self.nodes.values():
            if node.node_type == NodeType.TOOL and node.detail == tc_id:
                if not node.tool_args and args:
                    node.tool_args = format_tool_args(args, tool_name)
                    if tool_name:
                        node.tool_name = tool_name
                    return [self._snapshot_event()]
        return []

    def finalize(self) -> list[dict[str, Any]]:
        current_id = self._current_phase_id
        if current_id in self.nodes:
            node = self.nodes[current_id]
            if node.node_type == NodeType.PHASE:
                if node.status == StepStatus.RUNNING:
                    node.status = StepStatus.COMPLETED

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

    # ── Phase creation (LLM-driven descriptions) ──

    def _create_next_phase(self) -> str:
        parent_ids = list(dict.fromkeys(self._last_tool_child_ids))
        parent_ids = [pid for pid in parent_ids if pid in self.nodes]
        if not parent_ids:
            parent_ids = [self._current_phase_id]

        phase_count = sum(
            1 for n in self.nodes.values() if n.node_type == NodeType.PHASE
        )
        round_num = phase_count + 1
        title = f"第{round_num}轮智能分析"

        description = self._extract_phase_description() or ""

        nid = self._next_id()
        phase = TreeNode(
            nid, title, parent_ids[0],
            StepStatus.RUNNING, NodeType.PHASE,
            parent_ids=parent_ids,
            description=description,
        )
        self._add_node(phase)
        self._current_phase_id = nid
        self._last_tool_child_ids = []
        logger.info("Tree: phase '%s' (id=%s, parents=%s, desc=%r)",
                     title, nid, parent_ids, description[:60])
        return nid

    def _create_first_phase(self) -> str:
        description = self._extract_phase_description() or ""
        nid = self._next_id()
        phase = TreeNode(
            nid, "第1轮智能分析", None,
            StepStatus.RUNNING, NodeType.PHASE,
            parent_ids=[],
            description=description,
        )
        self._add_node(phase)
        self._current_phase_id = nid
        logger.info("Tree: phase '第1轮智能分析' (id=%s, desc=%r)", nid, description[:60])
        return nid

    def _extract_phase_description(self) -> str:
        """Extract phase description from LLM's think text."""
        if self.think_segments:
            text = self.think_segments[-1]
        elif self.think_buffer:
            text = "".join(self.think_buffer)
        else:
            return ""
        if text.strip():
            sentences = re.split(r'[。；\n]', text)
            for s in sentences:
                s = s.strip()
                if len(s) > 4:
                    return s[:80]
            return text.strip()[:80]
        return ""

    # ── Helpers ──

    def _snapshot_event(self) -> dict[str, Any]:
        return {
            "type": "tree_snapshot",
            "steps": [
                {
                    "id": node.id,
                    "title": node.title,
                    "parent_id": node.parent_id,
                    "parent_ids": node.parent_ids or ([node.parent_id] if node.parent_id else []),
                    "status": node.status.value,
                    "node_type": node.node_type.value,
                    "detail": node.detail,
                    "description": node.description,
                    "tool_name": node.tool_name,
                    "tool_args": node.tool_args,
                }
                for nid in self.node_order
                if nid in self.nodes
                for node in [self.nodes[nid]]
            ],
        }

    def _add_node(self, node: TreeNode) -> None:
        if node.parent_id and not node.parent_ids:
            node.parent_ids = [node.parent_id]
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
    def _is_tool_update(data: dict, namespace: list[str]) -> bool:
        if any("tools" in str(n).lower() for n in namespace):
            return True
        if not isinstance(data, dict):
            return False
        for key, value in data.items():
            if "tools" in str(key).lower():
                return True
            if isinstance(value, dict) and value.get("message_type") == "ToolMessage":
                return True
        return False

    @staticmethod
    def _tool_title(call: dict) -> str:
        """Map tool function name to Chinese display label."""
        name = call.get("name") or call.get("function", {}).get("name", "unknown")
        return TOOL_LABELS.get(name, name)



