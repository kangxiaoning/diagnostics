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
import re
from collections import OrderedDict
from typing import Any

# ── Tool → observability-dimension map ───────────────────────────────
# The only hand-maintained semantic asset.  Unmapped tools (including
# the production port's renamed toolset) fall back to "其他" and are
# excluded from the expected-dimension set — they never block, matching
# the portability constraint that unknown names degrade gracefully.
TOOL_DIMENSION: dict[str, str] = {
    # ── Host deep tools (host-expert, 实际环境 get_os_*/get_gpu_* 家族) ──
    "get_os_system_overview": "主机概览",
    "get_os_base_info": "主机概览",
    "get_os_cpu_info": "CPU",
    "get_os_cpu_load": "CPU",
    "get_os_cpu_cs": "CPU",
    "get_os_cpu_ps_elf": "进程",
    "get_os_mem_5s": "内存",
    "get_os_mem_oom": "内存",
    "get_os_mem_top_process": "进程",
    "get_os_block_5s": "磁盘IO",
    "get_os_block_info": "磁盘IO",
    "get_os_net_ss_s": "网络",
    "get_os_net_sar_dev": "网络",
    "get_os_net_softnet_stat": "网络",
    "get_os_net_tc_stat": "网络",
    "get_os_net_softirqs": "网络",
    "get_os_net_ethtools_s": "网络",
    "get_os_net_ethtool": "网络",
    "get_os_net_ping": "网络",
    "get_os_net_nslookup": "网络",
    "get_os_net_conntrack": "conntrack",
    "get_os_kernel_dmesg_err": "内核日志",
    "get_os_kernel_sysctl_one": "内核参数",
    "get_os_kernel_sysctl_all": "内核参数",
    "get_os_kernel_sysctl_grep": "内核参数",
    "get_os_kernel_numa_info": "内核参数",
    "get_os_kernel_journalctl_period": "系统日志",
    "get_gpu_status_info": "GPU",
    "get_gpu_pcie_info": "GPU",
    "get_gpu_driver_info": "GPU",
    "get_gpu_mod_info": "GPU",
    "get_gpu_dmesg_info": "GPU",
    # ── Host Argus metrics (host-argus-expert, 实际环境 11 维) ──
    "get_argus_os_overview_metrics": "主机指标概览",
    "get_argus_os_cpu_metrics": "CPU指标",
    "get_argus_os_mem_metrics": "内存指标",
    "get_argus_os_disk_metrics": "磁盘指标",
    "get_argus_os_net_metrics": "网络指标",
    "get_argus_os_nas_metrics": "NAS指标",
    "get_argus_os_ping_metrics": "PING指标",
    "get_argus_os_tcp_metrics": "TCP指标",
    "get_argus_os_kernel_metrics": "内核指标",
    "get_argus_os_load_metrics": "负载指标",
    "get_argus_os_ntp_metrics": "时间同步指标",
    # ── K8s deep tools (k8s-expert 及 serverless/kmc/sci 深度专家共用) ──
    "check_k8s_control_plane": "控制面",
    "check_k8s_nodes": "节点",
    "check_k8s_pods": "Pod",
    "get_pod_node_name": "Pod",
    "get_k8s_kubelet_status": "节点",
    "get_k8s_kubelet_logs": "节点日志",
    "get_k8s_kubeproxy_logs": "网络",
    "get_k8s_xid_logs": "GPU/Xid日志",
    "get_k8s_etcd_check": "etcd",
    "get_k8s_namespaces": "集群概览",
    "get_k8s_cluster_overview": "集群概览",
    "get_k8s_api_resources": "集群概览",
    "get_k8s_api_versions": "集群概览",
    "get_k8s_pod_logs": "Pod日志",
    "get_k8s_pod_logs_since": "Pod日志",
    "get_k8s_pod_logs_head": "Pod日志",
    "get_k8s_pod_previous_logs": "Pod日志",
    "describe_k8s_resource": "Pod",
    "get_k8s_resource_summary": "Pod",
    "get_k8s_pod_events_info": "事件",
    "get_k8s_cluster_events": "事件",
    "get_k8s_node_events_info": "事件",
    "get_k8s_node_info": "节点",
    "get_k8s_node_conditions": "节点",
    "get_k8s_pod_resource_usage": "资源用量",
    "get_k8s_node_resource_usage": "资源用量",
    "get_k8s_resource_top": "资源用量",
    "get_k8s_resource_list": "命名空间资源",
    "get_k8s_resource_yaml": "资源定义",
    "get_k8s_resource_history": "变更历史",
    "get_k8s_system_pods": "CoreDNS/系统组件",
    "get_k8s_coredns_logs": "CoreDNS/系统组件",
    "describe_k8s_coredns": "CoreDNS/系统组件",
    "list_k8s_helm_releases": "Helm",
    "get_k8s_helm_release_history": "Helm",
    "get_k8s_helm_release_values": "Helm",
    "get_k8s_network_policies": "网络策略",
    "check_k8s_rbac_permissions": "RBAC",
    "get_k8s_pod_restart_counts": "Pod",
    "check_k8s_certificate_expiry": "证书/Webhook",
    "check_k8s_webhook_status": "证书/Webhook",
    "get_k8s_etcd_status": "etcd",
    "get_k8s_etcd_logs": "etcd",
    "check_k8s_etcd_health": "etcd",
    "get_k8s_etcd_metrics": "etcd",
    "check_k8s_service_endpoints": "Service/Ingress",
    "get_k8s_elb_service": "Service/Ingress",
    "get_k8s_configmap": "ConfigMap",
    "list_k8s_namespace_resources": "命名空间资源",
    "get_k8s_pv_pvc_status": "存储",
    "get_k8s_ingress_status": "Service/Ingress",
    "get_k8s_vpc_cni": "网络",
    "explain_k8s_resource": "资源定义",
    # ── K8s Argus metrics (k8s-argus-expert 及 serverless/kmc/sci argus 专家共用) ──
    "get_argus_k8s_cluster_metrics": "集群指标",
    "get_argus_k8s_node_metrics": "节点指标",
    "get_argus_k8s_workload_metrics": "工作负载指标",
    "get_argus_k8s_pod_metrics": "Pod指标",
    "get_argus_k8s_master_metrics": "Master组件指标",
    # ── Serverless 家族专属补充工具（实际环境 k8s 面无等价语义）──
    "get_argus_shared_etcd_metrics": "共享etcd指标",
    "get_k8s_apigateway_status": "API Gateway",
    "get_k8s_group1_status": "Group1隧道",
    "get_k8s_ipam_status": "IPAM",
    "get_k8s_pod_ip": "Pod IP",
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
_GUIDANCE_MAX_CHARS = 600
# Conclusion contract restated at the decision point (design document
# §9, v3.26.0).  Two clauses only: the array field shape (the sole
# observed parse-failure mode) and the gap-declaration channel — the
# smallest high-signal token set that prevents the two failures that
# cost a full regeneration turn each.
_CLOSING_CONTRACT = (
    "收尾契约：调用结论工具提交——多值字段填字符串数组（每项一条短句）；"
    "未取证维度在 coverage_gaps 按『维度名：类型｜原因』写明"
    "（类型取 数据不可用/不适用/强制收尾）即视为已交代。"
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


def expected_dimensions(tool_names: list[str],
                        focus_dims: set[str] | None = None) -> set[str]:
    """Dimensions the CURRENT delegation is expected to cover.

    Default (no focus declared): every dimension the expert's toolset can
    observe — derived from the tools actually bound to this expert (each
    expert assembles a different toolset, so the expectation is computed
    per-call from the live request rather than maintained as a second
    static map).

    Task-scoped (v3.35.0, P-1): when the Coordinator names the relevant
    dimensions in the delegation description (``相关维度：…``), the
    expectation is the NAMED SUBSET **intersected with the expert's own
    toolset dimensions** instead (portability guard: a toolset that maps
    to no known dimension — the production port's renamed tools — yields
    an empty expectation, i.e. no check, never an unsatisfiable one).
    Rationale: the toolset-wide
    expectation (k8s experts: ~20 dimensions) is unrelated to any single
    task, so a concluding expert is bounced by G27 for 12-15 dimensions it
    correctly never needed — and, worse, is motivated to over-collect
    until the G19-ext call budget forces a wrap-up, after which the same
    dimensions are still uncovered (measured 2026-09-11: G27 18 bounces,
    G19-ext 36 interventions across two log segments; the two are one
    root cause).  Task-scoped expectation keeps the completeness check
    (the named dimensions must be covered or declared) while dropping the
    unrelated ones — Requirements-after-first-edit evidence (arXiv 2609.03028):
    stating the acceptance scope BEFORE the work cuts rework.
    """
    toolset_dims = {dimension_of(n) for n in tool_names} - {_UNKNOWN_DIM}
    if focus_dims:
        return set(focus_dims) & toolset_dims
    return toolset_dims


# ── Task-scoped focus extraction (P-1, v3.35.0) ──────────────────────
# Only an EXPLICITLY MARKED line is parsed ("相关维度：Pod、事件"): free
# prose mentioning 网络/内存 must not silently re-widen the expectation.
_FOCUS_MARK_RE = re.compile(
    r"(?:相关维度|取证范围|覆盖维度|focus\s*dimensions?)\s*[：:]\s*([^\n]+)",
    re.IGNORECASE,
)
# Longest-first so "Pod日志" is consumed before "Pod", "节点指标" before "节点".
_DIM_NAMES = sorted((d for d in set(TOOL_DIMENSION.values()) if d),
                    key=len, reverse=True)


def parse_focus_dims(text: str) -> set[str]:
    """Extract Coordinator-named dimensions from a delegation description.

    Deterministic, zero-LLM: match a marked line, then longest-name-first
    match against the dimension vocabulary (consumed names are removed so
    short names cannot double-match inside long ones).
    """
    if not text:
        return set()
    found: set[str] = set()
    for m in _FOCUS_MARK_RE.finditer(text):
        line = m.group(1)
        for dim in _DIM_NAMES:
            if dim in line:
                found.add(dim)
                line = line.replace(dim, " ")
    return found


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
                # P-1 (v3.35.0): Coordinator-named focus dimensions parsed
                # from the delegation description.  None = not parsed yet;
                # empty set = parsed, nothing named (fallback to toolset).
                "focus_dims": None,
                # v3.37.6 (progress frame): the delegation's own goal plus a
                # per-call "ref" counter (tool + key args).  Together they
                # turn the block from an ACTION TALLY into a TASK-PROGRESS
                # frame — the missing piece behind repeated calls: the expert
                # could see "N calls / some dimensions", but never "how far
                # from the delegated goal am I, and which call am I about to
                # repeat".
                "goal": "",
                "ref_counts": {},            # "tool(arg/arg)" → count
            }
            self._sessions[key] = s
        self._sessions.move_to_end(key)
        while len(self._sessions) > _MAX_TRACKED_DELEGATIONS:
            self._sessions.popitem(last=False)
        return s

    # ── call recording (guidance middleware) ──

    def record_call(self, key: str, tool: str, verdict: str,
                    ref: str = "") -> None:
        """Record one genuinely-executed diagnostic call.

        *verdict* is "yield" or "zero_yield" (classified by the caller
        via the G19 zero-yield predicate — one predicate, one truth).
        *ref* (v3.37.6) is the compact "tool(key args)" label: counting
        equal refs lets the progress frame name the call the expert is
        about to repeat, instead of only reporting a total.
        """
        s = self.session(key)
        dim = dimension_of(tool)
        s["calls"].append({"tool": tool, "dim": dim, "verdict": verdict})
        if ref:
            s["ref_counts"][ref] = s["ref_counts"].get(ref, 0) + 1
        if verdict == "zero_yield":
            s["dim_streaks"][dim] = s["dim_streaks"].get(dim, 0) + 1
        else:
            s["covered"].add(dim)
            s["dim_streaks"][dim] = 0

    # ── task-scoped focus (P-1) ──

    def set_goal(self, key: str, goal: str) -> None:
        """Record the delegation goal (progress frame, v3.37.6).

        Set once from the Coordinator's instruction block — the frame must
        be the task actually delegated, never a system invention.
        """
        s = self.session(key)
        if not s["goal"]:
            s["goal"] = (goal or "").strip()[:120]

    def set_focus(self, key: str, dims: set[str]) -> None:
        """Record the Coordinator-named focus dimensions for this delegation.

        Called once per delegation (first model call) with the parse result
        of the delegation description; idempotent thereafter so a later
        turn cannot silently re-widen the expectation.
        """
        s = self.session(key)
        if s["focus_dims"] is None:
            s["focus_dims"] = set(dims)

    def expected_for(self, key: str, tool_names: list[str]) -> set[str]:
        """Expected dimensions for this delegation (focus-aware, P-1)."""
        return expected_dimensions(tool_names, self.session(key)["focus_dims"])

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

    # ── near-truncation reasoning pressure (Q4, v3.38.5) ──
    # A turn whose reasoning stream balloons far above normal (but stays
    # short of the output cap) is the leading indicator of the degeneracy
    # that ends in a length truncation — measured 2026-09-11 session
    # fe3a8604: one turn produced 40417 reasoning chars while every other
    # turn stayed ≤4095, and the stream was visibly re-deciding ("OK, let me
    # finalize ..." ×8).  Recording it per delegation lets the NEXT turn be
    # told to converge BEFORE the budget is gone (proactive guidance); the
    # post-truncation thinking-off variant remains the reactive backstop.

    def set_reasoning_chars(self, key: str, chars: int) -> None:
        self.session(key)["last_reasoning_chars"] = int(chars or 0)

    def reasoning_chars(self, key: str) -> int:
        return int(self.session(key).get("last_reasoning_chars", 0) or 0)

    # v3.39.2 (C): output-budget pressure — the CONTENT side.  A turn whose
    # content ALONE approaches the output ceiling saturates the budget with
    # reasoning_chars=0 (measured 2026-09-12, scenario 38) and the conclusion
    # then has no room.  Tracked per delegation, exactly like the reasoning
    # side, so the next turn can be told to conclude instead of emitting more
    # bulk.

    def set_output_chars(self, key: str, chars: int) -> None:
        self.session(key)["last_output_chars"] = int(chars or 0)

    def output_chars(self, key: str) -> int:
        return int(self.session(key).get("last_output_chars", 0) or 0)

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
        uncovered = self.expected_for(key, tool_names) - s["covered"]
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
        # v3.37.6 progress FRAME (replaces the former action tally): the
        # block now answers "how far from the delegated goal am I" — goal,
        # repeated calls named explicitly, covered/uncovered dimensions,
        # budget, next step.  The old "系统已去重 N 次" line is gone: the
        # per-ref counts below carry the same information in a locatable
        # form ("which call"), so the total was pure redundancy.
        lines = ["[委派进展·系统维护]"]
        if s["goal"]:
            lines.append("· 委派目标：" + s["goal"])
        repeated = {r: c for r, c in s["ref_counts"].items() if c > 1}
        if repeated:
            top = sorted(repeated.items(), key=lambda kv: (-kv[1], kv[0]))[:4]
            lines.append(
                "· 已重复调用（同参数，勿再发）："
                + "；".join(f"{r} ×{c}" for r, c in top)
                + ("…" if len(repeated) > len(top) else ""))
        if s["covered"]:
            lines.append("· 已有数据维度：" + "、".join(sorted(s["covered"])))
        uncovered = sorted(self.expected_for(key, tool_names) - s["covered"])
        if uncovered:
            lines.append("· 尚未覆盖：" + "、".join(uncovered[:8])
                         + ("…" if len(uncovered) > 8 else ""))
        dead = self.dead_ends(key)
        if dead:
            lines.append("· 连续无数据方向："
                         + "、".join(f"{d}({c}次)" for d, c in dead.items()))
        lines.append(f"· 进度：已执行 {n} 次"
                     f"（软提醒 {budget_soft} 次 / 强制收尾 {budget_hard} 次）")
        lines.append("· 下一步："
                     + self.next_action(key, self.expected_for(key, tool_names),
                                        budget_soft))
        block = "\n".join(lines)
        if len(block) > _GUIDANCE_MAX_CHARS:
            block = block[:_GUIDANCE_MAX_CHARS - 1] + "…"
        return block
