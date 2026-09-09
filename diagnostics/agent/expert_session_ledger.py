"""Expert session ledger (design document §9 proactive-guidance family, v3.24.0).

Per-delegation deterministic progress state for expert subagents — the
expert-scale counterpart of the Coordinator's diagnosis ledger.  Records
which diagnostic tools were called, which observability dimensions they
cover, which directions proved data-unavailable, and the budget water
level — then feeds two consumers:

- proactive guidance injection (expert_guidance_middleware): a compact
  progress block appended to the system message before each model call;
- the reactive low-information-gain gate (expert_novelty_gate, G26):
  per-tool result fingerprints for marginal-information detection.

Rationale (design document §11 v3.24.0): the raw action-observation
history is NOT usable state — large tool results are evicted from
context, attention dilutes over long delegations, and the classificatory
facts (which direction is a dead end, which calls were redundant) are
never explicit in history.  PABU (arXiv:2602.09138) shows an explicit
compact progress state beats conditioning on full history (+23.9%
success, -26.9% steps, both components indispensable in ablation); this
ledger generates that state DETERMINISTICALLY — zero LLM participation,
so no sampling noise and full auditability.  Pirolli & Card's
sensemaking loop: the ledger is the externalised evidence file; dead
ends and uncovered dimensions are the information scent guiding the
next foraging move.

Deliberate omissions (selective retention, PABU): no result CONTENT is
stored — only classification labels (yield / zero_yield) and dimension
attribution.  Raw data stays in the conversation history / evicted
files; a deterministic excerpt risks misleading truncation.
"""

from __future__ import annotations

import os
from collections import OrderedDict
from typing import Any

# ── Tool → observability-dimension map ───────────────────────────────
# The only hand-maintained semantic asset.  Unmapped tools (including
# the production port's renamed toolset) fall back to "其他" and are
# excluded from the expected-dimension set — they never block, matching
# the portability constraint that unknown names degrade gracefully.
TOOL_DIMENSION: dict[str, str] = {
    # ── Host deep tools (host-expert) ──
    "get_system_overview": "主机概览",
    "check_cpu": "CPU",
    "check_memory": "内存",
    "check_disk": "磁盘IO",
    "check_network": "网络",
    "check_processes": "进程",
    "check_conntrack": "conntrack",
    "check_dmesg": "内核日志",
    "check_gpu_health": "GPU",
    "check_gpu_memory": "GPU",
    "check_gpu_utilization": "GPU",
    # ── Host Argus metrics (host-argus-expert) ──
    "query_argus_host_overview": "主机指标概览",
    "query_argus_cpu": "CPU指标",
    "query_argus_memory": "内存指标",
    "query_argus_disk": "磁盘指标",
    "query_argus_network": "网络指标",
    # ── K8s deep tools (k8s-expert) ──
    "check_kubernetes_control_plane": "控制面",
    "check_kubernetes_nodes": "节点",
    "check_kubernetes_pods": "Pod",
    "get_namespaces": "集群概览",
    "get_cluster_overview": "集群概览",
    "get_pod_logs": "Pod日志",
    "get_pod_logs_since": "Pod日志",
    "get_pod_logs_lines": "Pod日志",
    "get_pod_previous_logs": "Pod日志",
    "describe_pod": "Pod",
    "get_pod_events": "事件",
    "get_cluster_events": "事件",
    "get_node_info": "节点",
    "get_node_conditions": "节点",
    "get_pod_resource_usage": "资源用量",
    "get_node_resource_usage": "资源用量",
    "get_system_pods": "CoreDNS/系统组件",
    "get_coredns_logs": "CoreDNS/系统组件",
    "describe_coredns": "CoreDNS/系统组件",
    "list_helm_releases": "Helm",
    "get_helm_release_history": "Helm",
    "get_helm_release_values": "Helm",
    "get_network_policies": "网络策略",
    "check_rbac_permissions": "RBAC",
    "get_pod_restart_counts": "Pod",
    "check_certificate_expiry": "证书/Webhook",
    "check_webhook_status": "证书/Webhook",
    "get_etcd_status": "etcd",
    "get_etcd_logs": "etcd",
    "check_etcd_health": "etcd",
    "get_etcd_metrics": "etcd",
    "check_service_endpoints": "Service/Ingress",
    "get_configmap": "ConfigMap",
    "list_namespace_resources": "命名空间资源",
    "get_pv_pvc_status": "存储",
    "get_ingress_status": "Service/Ingress",
    # ── K8s Argus metrics (k8s-argus-expert) ──
    "query_argus_k8s_cluster": "集群指标",
    "query_argus_k8s_node": "节点指标",
    "query_argus_k8s_workload": "工作负载指标",
    "query_argus_k8s_pod": "Pod指标",
    "query_argus_k8s_etcd": "etcd指标",
    # ── Serverless logical cluster ──
    "query_argus_serverless_cluster": "逻辑集群指标",
    "query_argus_serverless_node": "逻辑节点指标",
    "query_argus_serverless_workload": "逻辑工作负载指标",
    "query_argus_serverless_pod": "逻辑Pod指标",
    "query_argus_shared_etcd": "共享etcd指标",
    "get_serverless_deployments": "逻辑工作负载",
    "get_serverless_pods": "逻辑Pod",
    "get_serverless_pod_logs": "逻辑Pod日志",
    "get_serverless_events": "逻辑事件",
    # ── KMC physical cluster ──
    "query_argus_kmc_cluster": "KMC集群指标",
    "query_argus_kmc_node": "KMC节点指标",
    "query_argus_kmc_workload": "KMC工作负载指标",
    "query_argus_kmc_pod": "KMC Pod指标",
    "query_argus_kmc_etcd": "KMC etcd指标",
    "get_kmc_deployments": "KMC工作负载",
    "get_kmc_pods": "KMC Pod",
    "get_kmc_events": "KMC事件",
    "get_kmc_pod_logs": "KMC日志",
    "get_kmc_etcd_status": "共享etcd",
    "get_kmc_apigateway_status": "API Gateway",
    "get_kmc_group1_status": "Group1隧道",
    "get_kmc_ipam_status": "IPAM",
    "get_kmc_vpc_cni_controller_status": "VPC-CNI控制器",
    # ── SCI physical cluster ──
    "query_argus_sci_cluster": "SCI集群指标",
    "query_argus_sci_node": "SCI节点指标",
    "query_argus_sci_workload": "SCI工作负载指标",
    "query_argus_sci_pod": "SCI Pod指标",
    "query_argus_sci_etcd": "SCI etcd指标",
    "get_sci_nodes": "SCI节点",
    "get_sci_pods": "SCI Pod",
    "get_sci_pod_logs": "SCI日志",
    "get_sci_events": "SCI事件",
    "check_sci_cni_status": "SCI CNI",
    "get_sci_pod_ip": "Pod IP",
}

_UNKNOWN_DIM = "其他"

# Per-store delegation bound (same as G19/G11).
_MAX_TRACKED_DELEGATIONS = 64
# Fingerprints retained per tool per delegation (G26).  The G19-ext
# total-call hard cap (20) bounds the real count; 8 covers the
# comparison window without unbounded growth.
_MAX_FINGERPRINTS_PER_TOOL = 8
# Hard cap on the rendered guidance block (design document PG4: the
# injected state must never bury the one action that matters; Anthropic
# context engineering — smallest set of high-signal tokens).
_GUIDANCE_MAX_CHARS = 400
# Conclusion contract restated at the decision point (design document
# §9, v3.26.0).  Two clauses only: the array field shape (the sole
# observed parse-failure mode) and the gap-declaration channel — the
# smallest high-signal token set that prevents the two failures that
# cost a full regeneration turn each.
_CLOSING_CONTRACT = (
    "收尾契约：调用结论工具提交——多值字段填字符串数组（每项一条短句）；"
    "未取证维度在 coverage_gaps 写明『维度名：原因』即视为已交代。"
)

# Truncation records kept per delegation (G28).  The bounded resubmit
# allows at most two truncated turns, so 4 is headroom for diagnostics
# without unbounded growth.
_MAX_TRUNCATIONS_PER_DELEGATION = 4


def _env_int(name: str, default: int) -> int:
    """Env var with safe fallback (test-only knob, not a product API)."""
    try:
        return max(1, int(os.environ.get(name, "") or default))
    except (TypeError, ValueError):
        return default


def dimension_of(tool_name: str) -> str:
    """Observability dimension covered by *tool_name*."""
    return TOOL_DIMENSION.get(tool_name, _UNKNOWN_DIM)


def expected_dimensions(tool_names: list[str]) -> set[str]:
    """Dimensions the CURRENT delegation can cover — derived from the
    tools actually bound to this expert (each expert assembles a
    different toolset, so the expectation is computed per-call from the
    live request rather than maintained as a second static map)."""
    return {dimension_of(n) for n in tool_names} - {_UNKNOWN_DIM}


class ExpertSessionLedger:
    """Per-delegation progress store, shared by the guidance middleware
    and the novelty gate (bookkeep once, consume everywhere)."""

    def __init__(self) -> None:
        self._sessions: OrderedDict[str, dict[str, Any]] = OrderedDict()

    def session(self, key: str) -> dict[str, Any]:
        s = self._sessions.get(key)
        if s is None:
            s = {
                "calls": [],            # [{tool, dim, verdict}]
                "covered": set(),       # dims with ≥1 yielding call
                "dim_streaks": {},      # dim → consecutive zero-yield count
                "novelty_streak": 0,    # G26 consecutive low-gain count
                "fps": {},              # G26: tool → [(struct_digest, tokens)]
                "tools": [],            # last tool names seen (model request)
                "conclusion_hinted": False,  # G27 one-time bounce latch
                "pending_guidance": "",      # G27 guidance awaiting injection
                # G28: output-length truncations observed for this
                # delegation.  A truncated turn never produces a
                # structured conclusion — the ToolMessage the Coordinator
                # receives is a fallback text, not evidence.
                "truncations": [],           # [{out, reasoning, duration_s, channels}]
                "truncation_reported": False,  # degraded path surfaced once
            }
            self._sessions[key] = s
        self._sessions.move_to_end(key)
        while len(self._sessions) > _MAX_TRACKED_DELEGATIONS:
            self._sessions.popitem(last=False)
        return s

    # ── call recording (guidance middleware) ──

    def record_call(self, key: str, tool: str, verdict: str) -> None:
        """Record one genuinely-executed diagnostic call.

        *verdict* is "yield" or "zero_yield" (classified by the caller
        via the G19 zero-yield predicate — one predicate, one truth).
        """
        s = self.session(key)
        dim = dimension_of(tool)
        s["calls"].append({"tool": tool, "dim": dim, "verdict": verdict})
        if verdict == "zero_yield":
            s["dim_streaks"][dim] = s["dim_streaks"].get(dim, 0) + 1
        else:
            s["covered"].add(dim)
            s["dim_streaks"][dim] = 0

    # ── novelty-gate state (G26) ──

    def novelty_streak(self, key: str) -> int:
        return self.session(key)["novelty_streak"]

    def bump_novelty(self, key: str) -> int:
        s = self.session(key)
        s["novelty_streak"] += 1
        return s["novelty_streak"]

    def reset_novelty(self, key: str) -> None:
        self.session(key)["novelty_streak"] = 0

    def fingerprints(self, key: str, tool: str,
                     entity: str = "") -> list[tuple[bytes, frozenset]]:
        """Fingerprints already seen for THIS tool + observability object.

        Bucketed by entity (design document §8 G26, v3.30.0): a fresh
        object must not inherit another object's similarity verdict.
        """
        return self.session(key)["fps"].get(tool, {}).get(entity, [])

    def add_fingerprint(self, key: str, tool: str, entity: str,
                        struct_hash: bytes, tokens: frozenset) -> None:
        fps = self.session(key)["fps"].setdefault(tool, {}).setdefault(entity, [])
        fps.append((struct_hash, tokens))
        del fps[:-_MAX_FINGERPRINTS_PER_TOOL]

    # ── conclusion-checkpoint state (G27) ──

    def set_tools(self, key: str, tool_names: list[str]) -> None:
        self.session(key)["tools"] = list(tool_names)

    def trailing_zero_yield_streak(self, key: str) -> int:
        """Consecutive zero-yield calls at the TAIL of the call list —
        the ledger-side mirror of G19's hard-block condition, so the
        conclusion checkpoint never contradicts an active hard block."""
        n = 0
        for c in reversed(self.session(key)["calls"]):
            if c["verdict"] == "zero_yield":
                n += 1
            else:
                break
        return n

    def conclusion_hinted(self, key: str) -> bool:
        return self.session(key)["conclusion_hinted"]

    def mark_conclusion_hinted(self, key: str, guidance: str) -> None:
        s = self.session(key)
        s["conclusion_hinted"] = True
        s["pending_guidance"] = guidance

    def pop_pending_guidance(self, key: str) -> str:
        s = self.session(key)
        g = s["pending_guidance"]
        s["pending_guidance"] = ""
        return g

    # ── output-truncation state (G28) ──
    # Bookkept per delegation key: the middleware instance is a factory
    # singleton shared by every expert, so a global counter would cross
    # attribute one delegation's truncation to another (the parallel
    # delegation case that G19/G26 already guard against).

    def record_truncation(self, key: str, info: dict[str, Any]) -> int:
        """Record one length-truncated model turn; returns the count."""
        s = self.session(key)
        s["truncations"].append(info)
        del s["truncations"][:-_MAX_TRUNCATIONS_PER_DELEGATION]
        return len(s["truncations"])

    def truncation_count(self, key: str) -> int:
        return len(self.session(key)["truncations"])

    def truncations(self, key: str) -> list[dict[str, Any]]:
        return list(self.session(key)["truncations"])

    def truncation_reported(self, key: str) -> bool:
        return self.session(key)["truncation_reported"]

    def mark_truncation_reported(self, key: str) -> None:
        self.session(key)["truncation_reported"] = True

    # ── derived views ──

    def dead_ends(self, key: str) -> dict[str, int]:
        """Dimensions with ≥2 consecutive zero-yield calls (the
        direction is most likely data-unavailable — G19's per-streak
        semantics lifted to dimension granularity)."""
        return {d: c for d, c in self.session(key)["dim_streaks"].items()
                if c >= 2}

    def next_action(self, key: str, expected: set[str],
                    budget_soft: int) -> str:
        """Deterministic next-action policy (ordered — first match wins;
        the expert-scale mirror of the Coordinator's exit model).

        1. budget过半 → wrap-up盘点;
        2. active dead end → switch dimension;
        3. uncovered dimensions remain → name them;
        4. otherwise → conclude when confidence allows.
        """
        s = self.session(key)
        n = len(s["calls"])
        dead = self.dead_ends(key)
        if n >= budget_soft:
            return ("预算过半——盘点已采集证据：足够置信则调用结论工具收尾；"
                    "仍有明确缺口只补最关键的 1-2 项")
        if dead:
            first = next(iter(dead))
            return f"{first} 方向连续无数据——该方向大概率不可用，转向其他维度取证"
        uncovered = expected - s["covered"]
        if uncovered:
            return ("未覆盖维度：" + "、".join(sorted(uncovered))
                    + "——优先投向与任务相关的未覆盖维度；与任务无关或数据不可用"
                    "则在结论 coverage_gaps 中声明后收尾")
        return "各方向均有数据——证据足够置信判断时调用结论工具收尾，不要继续搜索"

    def closing_reminder(self, key: str, tool_names: list[str]) -> str | None:
        """Decision-point restatement of the conclusion contract.

        The progress block is appended to the system message, i.e. it sits
        at the far end of the context from the token being generated, with
        every tool result in between — precisely the region a transformer
        uses least (Liu et al., TACL 2023, "Lost in the Middle").  The
        contract therefore gets a second, minimal copy next to the
        decision point, carrying only what the concluding turn must get
        right: the array field shape and the gap-declaration channel.

        Returns None before the first executed call (selective injection,
        same discipline as the progress block).
        """
        s = self._sessions.get(key)
        if s is None or not s["calls"]:
            return None
        uncovered = expected_dimensions(tool_names) - s["covered"]
        if uncovered:
            return ("未覆盖维度：" + "、".join(sorted(uncovered))
                    + "——相关维度补采 1-2 次；" + _CLOSING_CONTRACT)
        return _CLOSING_CONTRACT

    def render(self, key: str, tool_names: list[str], dedup_hits: int,
               budget_soft: int, budget_hard: int) -> str:
        """Render the compact progress block for prompt injection.

        Returns "" when nothing has been recorded yet (selective
        injection — silent on the first model call).  Hard-capped at
        _GUIDANCE_MAX_CHARS (PG4).
        """
        s = self.session(key)
        n = len(s["calls"])
        if n == 0:
            return ""
        lines = [
            f"[取证进展·系统维护] 已执行 {n} 次取证调用"
            f"（{budget_soft} 次提醒 / {budget_hard} 次强制收尾）"
        ]
        if s["covered"]:
            lines.append("· 已有数据维度：" + "、".join(sorted(s["covered"])))
        dead = self.dead_ends(key)
        if dead:
            lines.append("· 连续无数据方向："
                         + "、".join(f"{d}({c}次)" for d, c in dead.items()))
        if dedup_hits:
            lines.append(f"· 系统已去重 {dedup_hits} 次重复调用"
                         "——相同参数重查不会获得新数据")
        lines.append("· 下一步："
                     + self.next_action(key, expected_dimensions(tool_names),
                                        budget_soft))
        block = "\n".join(lines)
        if len(block) > _GUIDANCE_MAX_CHARS:
            block = block[:_GUIDANCE_MAX_CHARS - 1] + "…"
        return block
