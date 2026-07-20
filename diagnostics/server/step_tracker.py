from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

logger = logging.getLogger(__name__)


def format_tool_args(args: dict, tool_name: str = "") -> str:
    """Format tool arguments for display."""
    if not args:
        return ""
    parts = []
    for k, v in args.items():
        sv = str(v).strip()
        if not sv or sv == "{}":
            continue
        parts.append(f"{k}={sv}")
    return ", ".join(parts) if parts else ""


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
    "write_file": "写入文件",
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
    "get_namespaces": "列出命名空间",
    "get_coredns_logs": "查看CoreDNS日志",
    "describe_coredns": "查看CoreDNS详情",
    "list_helm_releases": "列出Helm部署",
    "get_helm_release_history": "查看Helm部署历史",
    "get_helm_release_values": "查看Helm部署参数",
    "get_node_conditions": "查看节点状态概览",
    "get_network_policies": "查看网络策略",
    "check_rbac_permissions": "查看RBAC权限",
    "get_pod_restart_counts": "查看Pod重启计数",
    "check_certificate_expiry": "检查证书过期",
    "check_webhook_status": "检查Webhook配置",
    "get_etcd_status": "查看etcd状态",
    "get_etcd_logs": "查看etcd日志",
    "check_etcd_health": "检查etcd健康",
    "get_etcd_metrics": "查看etcd指标",
    # Ledger management tools
    "commit_hypotheses": "提交诊断假设",
    "select_path": "选择诊断路径",
    "record_finding": "记录验证结论",
    "backtrack": "回溯假设路径",
    # Summarization
    "compact_conversation": "压缩对话历史",
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
    dedup: bool = False  # tool call was deduplicated (cached preview served)


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
        """Feed a text token for think/answer buffer. Phase creation is driven
        by handle_round_start (round-start events from LangGraph)."""
        if self.state == "done":
            return []

        self._current_response_text.append(text)
        if self.state in ("thinking", "executing"):
            self.think_buffer.append(text)
            return [{"type": "think_token", "text": text}]
        if self.state == "answering":
            self.answer_buffer.append(text)
            return [{"type": "token", "text": text}]
        return []

    def handle_round_start(self, round_num: int) -> list[dict[str, Any]]:
        """Create a new phase for an upcoming LLM round.
        Called when LangGraph enters the model node for round 2+."""
        if self.state == "done":
            return []
        if round_num <= self._current_round:
            return []
        self._current_round = round_num
        self._create_next_phase()
        self.state = "thinking"
        logger.info("Tree: round %d started, phase created", round_num)
        return [self._snapshot_event()]

    def handle_tool_call(self, tool_calls: list[dict]) -> list[dict[str, Any]]:
        new_calls = self._filter_new(tool_calls)
        if not new_calls:
            return []

        if self.state in ("thinking", "answering"):
            self.state = "executing"
            self._flush_think()

        parent_id = self._current_phase_id

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
        logger.info("Tree: +%d tool nodes under %s (accumulated: %d) created_ids=%s",
                     len(new_calls), parent_id, len(self._last_tool_child_ids), created_ids)
        delta = self._delta_event(created_ids)
        logger.debug("Tree: delta event added=%s",
                     [s.get("id") for s in delta.get("added", [])])
        return [delta]

    def handle_update(self, _data: dict, namespace: list[str]) -> list[dict[str, Any]]:
        if not self._is_tool_update(_data, namespace):
            return []

        tc_id = _data.get("tool_call_id", "")
        changed_ids: list[str] = []

        if tc_id:
            for node in self.nodes.values():
                if (node.node_type == NodeType.TOOL and
                        node.detail == tc_id and
                        node.status == StepStatus.RUNNING):
                    node.status = StepStatus.COMPLETED
                    if _data.get("dedup"):
                        node.dedup = True
                    changed_ids.append(node.id)
                    break

        if not changed_ids:
            for node in self.nodes.values():
                if node.node_type == NodeType.TOOL and node.status == StepStatus.RUNNING:
                    node.status = StepStatus.COMPLETED
                    changed_ids.append(node.id)
                    break

        if not changed_ids:
            return []

        has_running = any(
            n.node_type == NodeType.TOOL and n.status == StepStatus.RUNNING
            for n in self.nodes.values()
        )
        if has_running:
            return [self._delta_event(updated=changed_ids)]

        self._flush_think()
        # Mark current phase completed. Next phase is created by
        # handle_round_start when LangGraph enters the next model node.
        if self._current_phase_id and self._current_phase_id in self.nodes:
            node = self.nodes[self._current_phase_id]
            if node.node_type == NodeType.PHASE and node.status == StepStatus.RUNNING:
                node.status = StepStatus.COMPLETED
                changed_ids.append(node.id)
        self.state = "thinking"
        logger.info("Tree: all tools done, phase completed")
        return [self._delta_event(updated=changed_ids)]

    def update_tool_args(self, tc_id: str, tool_name: str, args: dict) -> list[dict[str, Any]]:
        """Update tool name/args for an existing tool node when streaming args arrive."""
        for node in self.nodes.values():
            if node.node_type == NodeType.TOOL and node.detail == tc_id:
                changed = False
                if args:
                    formatted = format_tool_args(args, tool_name)
                    if formatted and formatted != node.tool_args:
                        node.tool_args = formatted
                        changed = True
                    if tool_name:
                        node.tool_name = tool_name
                    # For task delegations: show expert name + instruction snippet
                    if tool_name == "task" and args.get("subagent_type"):
                        sub = args["subagent_type"]
                        desc = args.get("description", "")
                        first_line = desc.split("\n")[0].strip()[:60] if desc else ""
                        new_desc = None
                        if first_line and (first_line.startswith("请") or first_line.startswith("用户")):
                            new_desc = f"委派给 {sub}: {first_line}"
                        elif not node.description:
                            new_desc = f"委派给 {sub}"
                        if new_desc and new_desc != node.description:
                            node.description = new_desc
                            changed = True
                if changed:
                    return [self._delta_event(updated=[node.id])]
        return []

    def finalize(self) -> list[dict[str, Any]]:
        # Mark all running nodes completed
        for node in self.nodes.values():
            if node.status == StepStatus.RUNNING:
                node.status = StepStatus.COMPLETED

        # If the last phase has no tool children (text-only wrap-up round),
        # rename it to "诊断完成" instead of appending a redundant node.
        current_id = self._current_phase_id
        if current_id and current_id in self.nodes:
            last = self.nodes[current_id]
            if last.node_type == NodeType.PHASE:
                has_children = any(
                    n.node_type == NodeType.TOOL and
                    (n.parent_id == current_id or (n.parent_ids and current_id in n.parent_ids))
                    for n in self.nodes.values()
                )
                if not has_children:
                    last.title = "诊断完成"
                    self._flush_think()
                    fallback: list[dict[str, Any]] = []
                    if not self.answer_buffer:
                        fb = "".join(self.think_segments)
                        if fb.strip():
                            fallback.append({"type": "token", "text": fb})
                    self.state = "done"
                    return fallback + [self._snapshot_event()]

        # Create "诊断完成" node — connected to last round's tools (or last phase)
        parent_ids = list(dict.fromkeys(self._last_tool_child_ids))
        parent_ids = [pid for pid in parent_ids if pid in self.nodes]
        if not parent_ids and self._current_phase_id:
            parent_ids = [self._current_phase_id]

        nid = self._next_id()
        done = TreeNode(
            nid, "诊断完成", parent_ids[0] if parent_ids else None,
            StepStatus.COMPLETED, NodeType.PHASE,
            parent_ids=parent_ids,
        )
        self._add_node(done)
        self._current_phase_id = nid

        self._flush_think()

        fallback: list[dict[str, Any]] = []
        if not self.answer_buffer:
            fb = "".join(self.think_segments)
            if fb.strip():
                fallback.append({"type": "token", "text": fb})

        self.state = "done"
        return fallback + [self._snapshot_event()]

    # ── Phase creation ──

    def _create_next_phase(self) -> str:
        # Mark previous phase as completed before creating the new one.
        # This ensures there is always exactly one RUNNING Phase on the tree.
        prev_id = self._current_phase_id
        if prev_id and prev_id in self.nodes:
            prev = self.nodes[prev_id]
            if prev.node_type == NodeType.PHASE and prev.status == StepStatus.RUNNING:
                prev.status = StepStatus.COMPLETED

        parent_ids = list(dict.fromkeys(self._last_tool_child_ids))
        parent_ids = [pid for pid in parent_ids if pid in self.nodes]
        if not parent_ids:
            parent_ids = [prev_id]

        phase_count = sum(
            1 for n in self.nodes.values() if n.node_type == NodeType.PHASE
        )
        round_num = phase_count + 1
        title = f"第{round_num}轮智能分析"

        nid = self._next_id()
        phase = TreeNode(
            nid, title, parent_ids[0],
            StepStatus.RUNNING, NodeType.PHASE,
            parent_ids=parent_ids,
        )
        self._add_node(phase)
        self._current_phase_id = nid
        self._last_tool_child_ids = []
        logger.info("Tree: phase '%s' (id=%s, parents=%s)", title, nid, parent_ids)
        return nid

    def _create_first_phase(self) -> str:
        nid = self._next_id()
        phase = TreeNode(
            nid, "第1轮智能分析", None,
            StepStatus.RUNNING, NodeType.PHASE,
            parent_ids=[],
        )
        self._add_node(phase)
        self._current_phase_id = nid
        logger.info("Tree: phase '第1轮智能分析' (id=%s)", nid)
        return nid

    # ── Helpers ──

    def _node_to_dict(self, node: TreeNode) -> dict[str, Any]:
        return {
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
            "dedup": node.dedup,
        }

    def _snapshot_event(self) -> dict[str, Any]:
        """Full tree snapshot — used for structural changes (new phase, finalize)."""
        return {
            "type": "tree_snapshot",
            "steps": [
                self._node_to_dict(self.nodes[nid])
                for nid in self.node_order
                if nid in self.nodes
            ],
        }

    def _delta_event(
        self,
        added: list[str] | None = None,
        updated: list[str] | None = None,
        removed: list[str] | None = None,
    ) -> dict[str, Any]:
        """Incremental tree delta — only changed nodes.

        Frontend merges delta into existing graph state.
        Significantly reduces SSE payload vs full snapshot.
        """
        result: dict[str, Any] = {"type": "tree_delta"}
        if added:
            result["added"] = [
                self._node_to_dict(self.nodes[nid])
                for nid in added if nid in self.nodes
            ]
        if updated:
            result["updated"] = [
                {"id": nid, "status": self.nodes[nid].status.value,
                 "detail": self.nodes[nid].detail,
                 "tool_args": self.nodes[nid].tool_args,
                 "description": self.nodes[nid].description,
                 "title": self.nodes[nid].title}
                for nid in updated if nid in self.nodes
            ]
        if removed:
            result["removed"] = removed
        return result

    def remove_tool_node(self, tc_id: str) -> list[dict[str, Any]]:
        """Remove a tool node by its tool_call_id (stored in node.detail).

        Returns a tree_delta with the node in the removed list so the
        frontend can delete its DOM element and associated edges.
        """
        for nid, node in list(self.nodes.items()):
            if node.node_type == NodeType.TOOL and node.detail == tc_id:
                del self.nodes[nid]
                if nid in self.node_order:
                    self.node_order.remove(nid)
                return [self._delta_event(removed=[nid])]
        return []

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



