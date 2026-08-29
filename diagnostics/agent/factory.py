from __future__ import annotations

import os
import pathlib
from collections.abc import Sequence
from typing import Any, Literal

from pydantic import BaseModel, Field

from deepagents import (
    GeneralPurposeSubagentProfile,
    HarnessProfile,
    create_deep_agent,
    register_harness_profile,
)
from deepagents.backends import CompositeBackend, FilesystemBackend, StateBackend

from diagnostics.agent.ollama_chat import OllamaChatOpenAI
from diagnostics.agent.prompt import make_system_prompt
from diagnostics.agent.dedup_middleware import ToolDedupMiddleware
from diagnostics.agent.expert_stall_watchdog import ExpertStallWatchdogMiddleware
from diagnostics.agent.file_tool_governance import ExpertFileToolGovernanceMiddleware
from diagnostics.agent.ledger import (
    ARGUS_CLARIFICATION_MARKER,
    DiagnosisLedgerState,
)
from diagnostics.agent.ledger_middleware import DiagnosisLedgerMiddleware
from diagnostics.config import Settings
from diagnostics.tools import get_agent_tools as _get_live_tools
from diagnostics.tools import get_k8s_live_tools

# Mock tools are optional — only available in local dev environments.
# GitHub clones should set DIAGNOSTICS_MODE=production.
try:
    from diagnostics.tools.mock import (
        get_coordinator_mock_tools,
        get_host_argus_tools,
        get_k8s_argus_tools,
        get_k8s_mock_tools,
        get_kmc_argus_tools,
        get_kmc_tools,
        get_sci_argus_tools,
        get_sci_tools,
        get_serverless_argus_tools,
        get_serverless_tools,
    )
    from diagnostics.tools.mock.gpu import check_gpu_health, check_gpu_memory, check_gpu_utilization
    from diagnostics.tools.mock.hosts import (
        check_conntrack,
        check_cpu,
        check_disk,
        check_dmesg,
        check_memory,
        check_network,
        check_processes,
        get_system_overview,
    )
    from diagnostics.tools.mock.kubernetes import (
        check_kubernetes_control_plane,
        check_kubernetes_nodes,
        check_kubernetes_pods,
    )
    _MOCK_AVAILABLE = True
except ImportError:
    _MOCK_AVAILABLE = False
    # Provide no-op stubs so the module loads cleanly.
    # _build_subagents() will raise a clear error if mock mode is
    # requested but mock tools are not installed.
    def get_coordinator_mock_tools(): return []
    def get_host_argus_tools(): return []
    def get_k8s_argus_tools(): return []
    def get_k8s_mock_tools(): return []
    def get_serverless_argus_tools(): return []
    def get_serverless_tools(): return []
    def get_kmc_argus_tools(): return []
    def get_kmc_tools(): return []
    def get_sci_argus_tools(): return []
    def get_sci_tools(): return []
    check_gpu_health = check_gpu_memory = check_gpu_utilization = None
    check_conntrack = check_cpu = check_disk = check_dmesg = None
    check_memory = check_network = check_processes = None
    get_system_overview = None
    check_kubernetes_control_plane = check_kubernetes_nodes = check_kubernetes_pods = None

# ── Harness profile: disable the auto-added general-purpose subagent ──
# deepagents auto-adds a general-purpose subagent that sits outside the
# ledger / file-governance gates (it inherits every diagnostic tool plus the
# full filesystem toolset — a latent governance-bypass channel). This is a
# read-only diagnostic system, so that subagent is removed entirely.
# OllamaChatOpenAI subclasses ChatOpenAI, whose _get_ls_params reports
# ls_provider="openai", so the provider-level "openai" key resolves via
# _harness_profile_for_model's provider-only fallback — and so does every
# subagent that inherits the Coordinator's model.
#
# `delete` is hidden via the official excluded_tools mechanism: deepagents
# auto-appends a _ToolExclusionMiddleware for it on both the main agent and
# each subagent, filtering the tool from request.tools.  deepagents 0.7.x
# exposes a real destructive delete tool on non-sandbox backends, which this
# read-only system must never expose.
# `write_todos` needs no exclusion here: since deepagents 0.7.x the
# TodoListMiddleware (its write_todos tool + planning prompt) is opt-in and
# only added by the built-in Codex harness profiles, which this model never
# matches.  The previous approach only hid the tool name while the middleware
# still injected its ~2KB planning prompt on every model call; that middleware
# is no longer in the stack at all.
register_harness_profile(
    "openai",
    HarnessProfile(
        general_purpose_subagent=GeneralPurposeSubagentProfile(enabled=False),
        excluded_tools=frozenset({"delete"}),
    ),
)

PROJECT_ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
AGENT_DATA_ROOT = PROJECT_ROOT / "agent_data"

# Ensure report directory structure exists for hierarchical archiving
_REPORT_DIRS = [
    AGENT_DATA_ROOT / "reports" / "host",
    AGENT_DATA_ROOT / "reports" / "kubernetes",
]
for _d in _REPORT_DIRS:
    _d.mkdir(parents=True, exist_ok=True)

# ── Expert structured-return contracts (prompt-level JSON) ─────────────
# Expert returns follow a prompt-level JSON contract shaped by the
# schemas below: the expert's final message is a JSON object, carried by
# deepagents' last-non-empty-AIMessage extraction into the task
# ToolMessage content, then parsed by ledger_middleware (structured
# fields first, Chinese-label text patterns as the degraded path).
# (design document §8 G13 context, 2026-08-11: expert summaries truncated
# by max_tokens lost decisive evidence — compact JSON stays parseable
# even when short.)
# 2026-08-27: the framework-level response_format mechanism was removed
# (design document §11) — it forced tool_choice="required" on every
# expert model call, and "required" is the least portable field across
# OpenAI-compatible backends (vLLM supported it only since 0.8.3 via
# guided decoding; SGLang bypasses native tool-call parsers for it and
# silently falls back to plain text on parse failure), plus it
# contradicts guard receipts mandating text wrap-up (G19/dedup hard
# blocks).  The Pydantic classes remain as the SSOT for field names
# referenced by the return-contract prompts.

# Deep diagnostic experts (host-expert / k8s-expert / serverless-expert /
# kmc-expert / sci-expert): verdict + evidence + root cause.
class DeepExpertFindings(BaseModel):
    """Structured return for deep diagnostic subagents."""

    verdict: Literal["confirmed", "refuted", "inconclusive"] = Field(
        description=(
            "假设验证结论：confirmed（成立）/ refuted（不成立）/ inconclusive（证据不足）。"
            "verdict 须与证据主体方向一致：confirmed 的支持证据（key_evidence）为主体，"
            "refuted 的负证据（negative_evidence）为主体；两者矛盾时按证据主体修正 verdict"
            "（曾实证：verdict=refuted 但 key_evidence 全支持向，致假设被误证伪）"
        )
    )
    key_evidence: list[str] = Field(
        default_factory=list,
        description="关键证据 1~3 条，每条标注数据来源（工具名/日志/事件）",
    )
    negative_evidence: list[str] = Field(
        default_factory=list,
        description="负证据必报：与假设矛盾、或未找到目标对象的证据（如目标 Pod 不存在/目标组件正常），与阳性证据同等重要，不得省略",
    )
    root_cause: str = Field(
        default="",
        description=(
            "根因判断：confirmed/inconclusive 且已定位根因时陈述根因结论；"
            "refuted 时陈述假设被排除的原因及建议转向方向（此时假设本身不是根因）；"
            "证据不足时说明还需什么数据。方向须与 verdict 一致"
        ),
    )
    confidence: str = Field(
        default="",
        description="置信度：高/中/低 + 百分比（如 中 70%）",
    )


# Argus time-series analysis experts (host-argus-expert / k8s-argus-expert /
# serverless-argus-expert / kmc-argus-expert / sci-argus-expert): metrics
# only, no deep diagnosis.  The `clarification` field carries the
# ARGUS_CLARIFICATION_MARKER contract (design document §8 G2 / §9 v3.11.1)
# when required parameters are missing — the coordinator-side classifier
# matches it deterministically.
class ArgusExpertFindings(BaseModel):
    """Structured return for Argus time-series subagents."""

    clarification: str = Field(
        default="",
        description=(
            "参数缺失澄清：若因缺少必要参数（如 hostname/monitor_name）无法执行任何查询，"
            f"以 {ARGUS_CLARIFICATION_MARKER} 开头并列出所需参数；否则为空字符串。"
        ),
    )
    mutation_points: list[str] = Field(
        default_factory=list,
        description="突变时间点：指标显著变化的时间点及变化值（如 15:03 CPU 36→95%）",
    )
    anomaly_ranking: list[str] = Field(
        default_factory=list,
        description="异常排序（按严重程度）：🔴严重 / ⚠中等 逐条列出具体数值；✅正常项合并为一条汇总（如「其余 N 个节点/指标均正常、无突变」）",
    )
    negative_evidence: list[str] = Field(
        default_factory=list,
        description="负证据必报：关键指标正常/无异常必须明确报告（排除依据），与异常发现同等重要",
    )
    concurrent_anomalies: list[str] = Field(
        default_factory=list,
        description="并发异常：同一时间点发生的多个异常（暗示共同根因）",
    )
    cross_domain: list[str] = Field(
        default_factory=list,
        description="跨域关联：不同子系统指标之间的时序因果推断",
    )
    preliminary_judgment: str = Field(
        default="",
        description="初步判断：基于指标关联的根因推断（一句话）",
    )
    confidence: str = Field(
        default="",
        description="置信度：高/中/低 + 百分比（如 中 70%）",
    )


# Common return format suffix for all subagents — enables Coordinator to
# parse expert results into structured record_finding calls.
# Suffix appended to every subagent system_prompt.
# Covers: tool boundary, no-ledger-tool rule, and structured return format.
# NOTE: each subagent's own "返回不超过150字" line is removed; this suffix
# provides the canonical return format so there is no duplication.
_EXPERT_RETURN_SUFFIX = (
    "\n\n**工具使用边界（严格遵守）**:\n"
    "- `read_file`/`grep`/`glob`/`ls` 只能访问 `/agent_data/skills/**`（技能文件），"
    "禁止访问 `/agent_data/reports/**`、`/agent_data/traces/**`（历史诊断数据，与你的任务无关）。\n"
    "- 禁止访问 `/proc/**`、`/sys/**`、`/var/log/**`、`/etc/**` 等被诊断主机路径。\n"
    "- 远程主机/集群数据必须通过本 subagent 配备的专用诊断工具获取，不能用本地文件工具代替。\n"
    "- 你只负责取证与分析——假设与结论的台账登记由 Coordinator 统一完成，你只需按下方格式返回结构化结论。\n"
    "- 避免冗余枚举：概览类工具返回批量数据后，仅对**显示异常**的资源用深度工具核查"
    "（多个资源异常则需全部核查），不对正常的节点/Pod/资源逐一调用同名查询工具。"
    "若多个工具返回同一资源的数据且结论一致，取最早返回的结果即可，无需重复验证。\n"
    "- 禁止重复调用：同一工具+同一参数只调用一次——系统已自动去重，"
    "重复调用将被计数、警告直至拦截；同一指标同一时间窗口禁止按设备/参数"
    "拆分多次查询（一次调用拿全量，再从中筛选）。\n"
    "- grep 检索多个关键字时必须用一条正则一次完成（如 `OOM|panic|killed`），"
    "禁止对同一路径逐关键字多次 grep（同路径第 2 次起系统将提示，第 3 次起"
    "双倍计入读取预算）。\n"
    "\n**工具调用预算与停止条件（严格遵守）**:\n"
    "- 简单验证（单一症状/单一子系统）：≤5 次工具调用\n"
    "- 中等验证（跨子系统关联）：≤8 次工具调用\n"
    "- 复杂验证（跨层诊断）：≤12 次工具调用\n"
    "- 收益递减时立即停止：当连续 2 次工具调用未获得新证据时，直接返回已有结论\n"
    "- 关键证据已足够做出置信度判断时，立即返回结论，不要继续搜索\n"
    "\n**返回格式（信息密集，通常 300-600 字。证据充足时立即返回，不要过度展开）**:\n"
    "- 交付方式：证据收齐后，你的最终回复就是一个 JSON 对象"
    "（verdict/key_evidence/negative_evidence/root_cause/confidence），"
    "这是交付验证结论的标准方式，Coordinator 据此解析落账；"
    "回复正文以 `{` 开头、以 `}` 结束——JSON 对象本身即全部回复内容。\n"
    "- verdict: confirmed / refuted / inconclusive——verdict 须与证据主体方向一致："
    "confirmed 以 key_evidence 为主体，refuted 以 negative_evidence 为主体，矛盾时按证据主体修正 verdict\n"
    "- key_evidence: 关键证据 1~3 条，每条标注数据来源\n"
    "- negative_evidence: 负证据必报：与假设矛盾、或未找到目标对象的证据（如\"目标 Pod 不存在\"\"目标组件正常\"）"
    "必须如实列出，与阳性证据同等重要，不得省略\n"
    "- root_cause: confirmed 且已定位根因时陈述根因结论；refuted 时陈述排除原因及建议转向方向"
    "（此时假设本身不是根因）；证据不足时说明还需什么数据\n"
    "- confidence: 高/中/低 + 百分比"
)

# Return format suffix for Argus time-series analysis experts.
# Argus experts do NOT perform deep diagnosis — they collect and analyse
# 1min-granularity monitoring metrics, returning structured time-series
# correlation findings so Coordinator can form well-grounded hypotheses.
_ARGUS_EXPERT_RETURN_SUFFIX = (
    "\n\n**工具使用边界（严格遵守）**:\n"
    "- 你只有 query_argus_* 监控工具，无文件工具和深度诊断工具。\n"
    "- 你只负责指标采集与关联分析——假设与结论的台账登记由 Coordinator 统一完成。\n"
    "- 并行查询所有相关指标（通常 2~4 次工具调用），覆盖完整后立即汇总返回，不要逐个时间点细查。\n"
    "- 区分采集目的，合理分配采集深度：\n"
    "  - 定位对象（委派描述指定的目标实体及其落点、或已观测到异常的指标）→ 完整采集其关键指标维度；\n"
    "  - 排除对象（其余节点/组件，仅用于确认「该方向正常」）→ 以确认「无异常突变」为目标，"
    "用尽量少的概览性查询覆盖关键维度即可，确认无突变即止，无需逐对象逐指标展开。\n"
    # Progressive-discovery query contract (design document §9, v3.21.1):
    # overview-first drill-down — quality-neutral by construction: mutation
    # signals and the negative-evidence obligation are unchanged, the
    # full-scan fallback clause preserves today's behaviour whenever the
    # overview lacks object-level attribution.  Motivation (2026-08-29
    # batch capture analysis): sci-argus fanned out 20+ tool calls (14
    # per-pod queries + an 11-call zero-increment rescan), host-argus 10,
    # vs 4-5 for the clean experts; round-1 delegation wall time is
    # dominated by these redundant in-flight calls.
    "- 渐进式发现（概览→下钻）：先查概览类工具（*_cluster / *_overview）获取维度级突变信号，"
    "再仅对「概览标记突变的维度」与「委派指定的目标对象」下钻做对象级归因；"
    "概览无突变信号的关键维度各查一次确认即止；"
    "概览不含对象级明细且委派未指明对象时，按完整维度扫描定位异常对象。\n"
    "- 已查过的「工具+参数」组合直接复用其返回做分析（重查只返回缓存结果、零新信息），"
    "后续查询只投向尚未覆盖的维度或对象。\n"
    # Clarification contract (design document §8 G2 / §9, v3.11.1):
    # deterministic marker the coordinator-side classifier matches —
    # contractual protocol, not keyword enumeration (§11 S1 否决理由).
    "- 参数缺失澄清标记: 若因缺少必要参数（如 hostname）无法执行任何查询，"
    f"clarification 字段必须以 {ARGUS_CLARIFICATION_MARKER} 开头并列出所需参数——"
    "系统依此标记识别缺口并转交 Coordinator 处理；禁止省略前缀，禁止臆造参数。\n"
    "\n**返回格式（信息密集，通常 200-400 字。指标覆盖完整后立即返回，不要展开分析报告）**:\n"
    "- 交付方式：指标收齐后，你的最终回复就是一个 JSON 对象"
    "（clarification/mutation_points/anomaly_ranking/negative_evidence/"
    "concurrent_anomalies/cross_domain/preliminary_judgment/confidence），"
    "这是交付时序结论的标准方式，Coordinator 据此解析分类；"
    "回复正文以 `{` 开头、以 `}` 结束——JSON 对象本身即全部回复内容。\n"
    "- mutation_points: 列出指标显著变化的时间点及变化值（如 15:03 CPU 36→95%）\n"
    "- anomaly_ranking（按严重程度）: 🔴严重 / ⚠中等 逐条列出具体数值；✅正常项合并为一条汇总"
    "（如「其余 N 个节点/指标均正常、无突变」）\n"
    "- negative_evidence: 关键指标正常/无异常必须明确报告（排除依据），与异常发现同等重要\n"
    "- concurrent_anomalies: 同一时间点发生的多个异常（暗示共同根因）\n"
    "- cross_domain: 不同子系统指标之间的时序因果推断\n"
    "- preliminary_judgment: 基于指标关联的根因推断（一句话）\n"
    "- confidence: 高/中/低 + 百分比"
)


def _build_subagents(
    mode: str = "mock",
    scene_profile: Any | None = None,
    entity_type: str = "",
) -> list[dict[str, Any]]:
    """Define specialized diagnostic subagents with domain-specific skills.

    Each subagent has:
    - Precise description for correct routing by main agent
    - Focused tool set (never inherit all tools)
    - Domain skills loaded lazily (skills-first diagnosis)
    - Output format with word limit to keep context clean

    Expert filtering (design document scenario-framework §4.2):
    - When *scene_profile* is given, only the experts listed in
      ``scene_profile.experts`` are kept (scene-driven assembly).
    - Legacy fallback: when *entity_type* is ``"host"``, Kubernetes-specific
      experts (k8s-argus-expert, k8s-expert) are excluded — the target has no
      K8s cluster, so delegating those experts would waste a round.
    """
    if mode == "mock" and not _MOCK_AVAILABLE:
        raise RuntimeError(
            "Mock mode requested but mock tools are not installed. "
            "Set DIAGNOSTICS_MODE=production or install the mock package."
        )

    k8s_tools = get_k8s_mock_tools() if mode == "mock" else get_k8s_live_tools()
    argus_host_tools = get_host_argus_tools() if mode == "mock" else []
    argus_k8s_tools = get_k8s_argus_tools() if mode == "mock" else []
    subagents: list[dict[str, Any]] = [
        # ── Host Argus expert (host-level time-series analysis) ──
        #
        # Dedicated Argus metrics analyst for host-level indicators.
        # Coordinator delegates in UNDERSTAND phase to get focused time-series
        # correlation analysis without flooding its own context with raw metrics.
        {
            "name": "host-argus-expert",
            "description": (
                "查询并分析主机级 Argus 监控指标时序（CPU/内存/磁盘/网络 1min 粒度）。"
                "适用场景：需要获取主机指标时间线以识别突变点、异常严重程度排序和"
                "跨子系统时序关联时委派。"
            ),
            "system_prompt": (
                "你是 Linux 主机指标时序分析专家。\n"
                "你的职责是查询和分析 Argus 1min 粒度的分项指标时间线，"
                "识别异常突变点、跨子系统时序关联、异常严重程度排序。\n\n"
                "⚠ 关键约束：你只有 query_argus_* 工具，没有文件工具。"
                "所有必需参数必须从以下来源获取（按优先级）：\n"
                "1. 消息开头的「系统已预解析的诊断实体」区块（直接提供 hostname 参数值，优先级最高）\n"
                "2. 消息开头的「Argus 诊断上下文（工具参数契约）」JSON 区块（Serverless 场景；"
                "hostname 参数取 hosts[].host_name，诊断目标落点主机取 target_landing_hosts）\n"
                "3. Coordinator 的 task 描述\n"
                "hostname 参数必须是主机名（host_name），禁止填写节点 IP（node_name）。\n"
                "如果以上都未提供足够的参数，回复'需要 hostname 参数'——"
                "不要输出完整诊断报告，不要模拟工具调用。\n\n"
                "诊断原则：\n"
                "- 并行查询所有相关 Argus 指标（CPU/内存/磁盘/网络）\n"
                "- 识别每个子系统的突变时间点（指标显著变化的分钟）\n"
                "- 分析跨子系统时序关联——同一分钟的异常可能共享根因\n"
                "- 推断因果方向（如：磁盘IO突变先于iowait升高→磁盘是根因方向）\n"
                "- 按严重程度排序（🔴严重/⚠中等/✅正常）\n"
                + _ARGUS_EXPERT_RETURN_SUFFIX
            ),
            "tools": argus_host_tools,
        },
        # ── K8s Argus expert (cluster-level time-series analysis) ──
        #
        # Dedicated Argus metrics analyst for Kubernetes cluster indicators.
        # Dimension-partitioned tools (refactored following the serverless
        # argus expert, design document §4 专有集群 Argus 工具契约 / §11 v3.1.0):
        # cluster/node/workload/pod + etcd — 5 tools, monitor_name as leading
        # required param (resolved before agent creation, stored in param_overrides).
        # Host-level metrics (CPU/memory/disk/network) are exclusively owned
        # by host-argus-expert to avoid duplicate queries.
        {
            "name": "k8s-argus-expert",
            "description": (
                "查询并分析 Kubernetes 集群级 Argus 监控指标时序。"
                "工具 query_argus_k8s_cluster: 集群概览(API延迟/etcd/DNS/节点就绪)。"
                "工具 query_argus_k8s_node: 单节点(Ready/驱逐/kubelet)。"
                "工具 query_argus_k8s_workload: 工作负载(副本/重启/Pending)。"
                "工具 query_argus_k8s_pod: 单Pod(重启/OOM/探针)。"
                "工具 query_argus_k8s_etcd: etcd(leader/raft/存储)。"
                "适用场景：需要获取K8s集群指标时间线以识别集群异常时序、"
                "控制面稳定性与工作负载状态时委派。"
            ),
            "system_prompt": (
                "你是 Kubernetes 集群指标时序分析专家。\n"
                "你的职责是查询和分析 Argus 1min 粒度的集群指标时间线，"
                "识别集群异常时序与控制面稳定性问题。\n\n"
                "你有五个按维度分区的专用工具（首参均为 monitor_name，"
                "已由系统预解析提供）：\n"
                "- query_argus_k8s_cluster: 集群概览(API Server延迟/错误、etcd Leader/DB用量、DNS延迟/错误、NotReady节点数)\n"
                "- query_argus_k8s_node: 单节点(Ready状态/驱逐/kubelet心跳/节点CPU/内存)\n"
                "- query_argus_k8s_workload: 工作负载(期望/就绪副本、Pod重启、Pending)\n"
                "- query_argus_k8s_pod: 单Pod(容器重启/OOMKilled/探针失败/内存工作集)\n"
                "- query_argus_k8s_etcd: etcd(Leader/Leader变更/提案失败/WAL fsync/Backend Commit/DB大小)\n\n"
                "⚠ 关键约束：你只有 query_argus_* 工具，没有文件工具。"
                "所有必需参数必须从以下来源获取（按优先级）：\n"
                "1. 消息开头的「系统已预解析的诊断实体」区块（直接提供 monitor_name/cluster_name 参数值，优先级最高）\n"
                "2. Coordinator 的 task 描述\n"
                "如果两处都未提供足够的参数，回复'需要 monitor_name/cluster_name 参数'——"
                "不要输出完整诊断报告，不要模拟工具调用。\n\n"
                "诊断原则：\n"
                "- 按需并行调用维度工具（先 cluster 概览定位异常层，再 node/workload/pod/etcd 下钻）\n"
                "- 识别每个维度的突变时间点（指标显著变化的分钟）\n"
                "- 分析跨维度时序关联——同一分钟的异常可能共享根因\n"
                "- 区分控制面问题(API/etcd/DNS) vs 节点资源问题(NotReady/Evictions) vs 工作负载问题(Restarts/Pending)\n"
                "- 按严重程度排序（🔴严重/⚠中等/✅正常）；集群概览返回的异常对象清单自带 severity 与 start_ts 排序锚点时，排序与时间线以锚点为准\n"
                "- 如需节点级CPU/内存/磁盘/网络时序，告知Coordinator另行委派host-argus-expert\n"
                + _ARGUS_EXPERT_RETURN_SUFFIX
            ),
            "tools": argus_k8s_tools,
        },
        # ── Host expert (unified: CPU + memory + disk + network) ──
        #
        # Merged from 4 separate experts because production incidents rarely
        # stay within one subsystem — the same root cause cascades across
        # domains (disk IO → iowait → D-state, OOM → swap → disk IO, conntrack
        # full → softirq → CPU sys%).  A single host-expert can correlate
        # cross-domain signals and avoid the "did we delegate the right expert?"
        # coordination problem.
        {
            "name": "host-expert",
            "description": (
                "诊断 Linux 主机全栈问题：CPU（利用率/负载/iowait/D状态）、"
                "内存（泄漏/OOM/Swap）、磁盘（IO饱和/存储延迟）、"
                "网络（TCP重传/丢包/连接饱和/ARP/conntrack/MTU/软中断/TCP队列/gRPC泄漏）、"
                "GPU（CUDA OOM/温度限速/利用率异常/ECC 错误）。"
                "适用场景：主机层面任何异常——高负载、内存不足、IO慢、网络丢包、GPU 异常，"
                "或复合症状（如CPU高同时磁盘慢）。能跨子系统关联诊断。"
            ),
            "system_prompt": (
                "你是 Linux 主机全栈诊断专家，覆盖 CPU、内存、磁盘 IO、网络、GPU 五个子系统。\n\n"
                "⚠ 角色边界：你的职责是验证 Coordinator 委派的特定假设，返回结构化验证结论。\n"
                "你不需要生成完整诊断报告或修复方案——那是 Coordinator 的职责。\n"
                "你的输出将被 Coordinator 解析为 record_finding 调用的输入。\n\n"
                "诊断原则：\n"
                "- 先看全局（system-health-check），再按症状选择对应技能深入\n"
                "- 注意跨域关联——同一根因常表现跨子系统症状（如 CUDA OOM 致进程被 kill 而主机内存正常）\n\n"
                "按症状选择技能：\n"
                "- CPU 高/负载高/iowait → cpu-diagnosis\n"
                "- 内存不足/OOM/Swap → memory-diagnosis\n"
                "- 磁盘 IO 慢/高 await/util → disk-io-diagnosis\n"
                "- TCP 丢包/重传/DNS 超时 → network-diagnosis\n"
                "- conntrack 表满 → conntrack-diagnosis\n"
                "- ARP 缓存溢 → arp-cache-diagnosis\n"
                "- MTU 错配/大包丢包 → mtu-misconfig-diagnosis\n"
                "- 软中断占满 CPU0 → softirq-starvation\n"
                "- TCP Accept 队列溢 → tcp-listen-overflow\n"
                "- gRPC 连接泄漏 → grpc-connection-leak\n"
                "- 内核参数丢包 → kernel-parameter-drops\n"
                "- 跨域复合症状 → cross-layer-diagnosis\n"
                "- GPU CUDA OOM/温度限速/利用率异常/ECC 错误 → gpu-diagnosis"
                "（检查顺序：健康状态[温度/限速/ECC/PCIe] → 显存[OOM/泄漏] → 利用率）\n"
                "- 技能工具不足时，使用 check_cpu/check_memory/check_disk/check_network/"
                "check_gpu_* 直接分析\n\n"
                "关键诊断动作：\n"
                "- iowait 高 + 进程 D 状态 → 锁定磁盘 IO 根因\n"
                "- RSS 远超 heap limit → 识别堆外内存泄漏\n"
                "- TCP 重传率 > 10% 但接口无丢包 → 排查 MTU/tc/conntrack\n"
                "- DNS 超时 + conntrack table full → conntrack 导致 UDP 丢包\n"
                "- 返回验证结论（confirmed/refuted/inconclusive）+ 1~3 条关键证据，供 Coordinator 综合判断\n"
                + _EXPERT_RETURN_SUFFIX
            ),
            # GPU tools/skills are part of the unified host expert
            # (v3.9.0 — the standalone gpu-expert was removed: GPU faults
            # need cross-domain correlation with memory/processes, e.g.
            # "CUDA OOM kills the process while host memory stays normal";
            # container-scene GPU checks go through the host expert too,
            # since GPUs live on physical nodes).
            "tools": [t for t in [get_system_overview, check_cpu, check_memory, check_disk, check_network, check_processes, check_conntrack, check_dmesg, check_gpu_health, check_gpu_memory, check_gpu_utilization] if t is not None],
            "skills": [
                "/agent_data/skills/system-health-check/",
                "/agent_data/skills/cpu-diagnosis/",
                "/agent_data/skills/memory-diagnosis/",
                "/agent_data/skills/disk-io-diagnosis/",
                "/agent_data/skills/network-diagnosis/",
                "/agent_data/skills/arp-cache-diagnosis/",
                "/agent_data/skills/conntrack-diagnosis/",
                "/agent_data/skills/mtu-misconfig-diagnosis/",
                "/agent_data/skills/softirq-starvation/",
                "/agent_data/skills/kernel-parameter-drops/",
                "/agent_data/skills/tcp-listen-overflow/",
                "/agent_data/skills/grpc-connection-leak/",
                "/agent_data/skills/cross-layer-diagnosis/",
                "/agent_data/skills/gpu-diagnosis/",
            ],
        },
        # ── Kubernetes expert (unified: cluster infrastructure + workloads) ──
        #
        # Covers both cluster-level (control plane, nodes, etcd, CoreDNS, networking,
        # storage) and workload-level (Pods, Deployments, Services, Ingress, Helm,
        # containers) diagnostics.  Uses cross-layer-diagnosis to trace cascading
        # failures from host → kubelet → Node NotReady → Pod eviction.
        {
            "name": "k8s-expert",
            "description": (
                "诊断 Kubernetes 集群全栈问题：控制面（API Server/etcd/scheduler）、"
                "节点（NotReady/压力/驱逐）、网络（CoreDNS/kube-proxy/策略）、存储（PV/PVC）、"
                "工作负载（Pod生命周期/控制器/Service/Ingress/Helm/容器运行时）。"
                "适用场景：kubectl超时、节点NotReady、Pod崩溃/重启/启动失败、"
                "流量不可达、DNS超时、etcd异常、集群事件劣化。"
                "主机级联故障使用 cross-layer-diagnosis 关联主机指标与K8s症状。"
            ),
            "system_prompt": (
                "你是 Kubernetes 全栈诊断专家，覆盖集群基础设施和工作负载。\n\n"
                "⚠ 角色边界：你的职责是验证 Coordinator 委派的特定假设，返回结构化验证结论。\n"
                "你不需要生成完整诊断报告或修复方案——那是 Coordinator 的职责。\n"
                "你的输出将被 Coordinator 解析为 record_finding 调用的输入。\n\n"
                "职责范围：\n"
                "- 控制面：API Server、etcd、scheduler、controller-manager\n"
                "- 节点：kubelet、状态（NotReady/MemoryPressure/DiskPressure）、驱逐\n"
                "- 集群网络：CoreDNS、kube-proxy、网络策略\n"
                "- 集群存储：PV/PVC\n"
                "- 工作负载：Pod生命周期（OOMKilled/CrashLoopBackOff/Error/ImagePullBackOff）、"
                "控制器（Deployment/StatefulSet/DaemonSet）、Service/Ingress、Helm、容器运行时\n\n"
                "诊断工作流（按症状选择技能）：\n"
                "- API/etcd/scheduler 问题 → control-plane-diagnosis\n"
                "- etcd 深度分析 → etcd-diagnosis\n"
                "- DNS 解析失败 → coredns-diagnosis\n"
                "- 标准 Pod/Deployment 排障 → kubernetes-diagnosis\n"
                "- 容器运行时（镜像/崩溃） → container-runtime-diagnosis\n"
                "- 主机级联故障追踪 → cross-layer-diagnosis\n"
                "- 技能工具不足时，使用 check_kubernetes_* 和 K8s 工具直接分析\n\n"
                "**K8s 工具参数发现（必须先发现再操作）**:\n"
                "- `namespace` → 先调 `get_namespaces(cluster_name)` 获取可用命名空间\n"
                "- `resource_type` → 使用完整名称：`deployment` / `daemonset`（非 `deploy` / `ds`）\n"
                "- `cluster_name` → 从用户描述或集群元数据获取，不臆造\n"
                "- `pod_name` → 先从 `check_kubernetes_pods` 或 `get_pod_events` 获取\n\n"
                "关键诊断问题：\n"
                "- 区分基础设施根因与工作负载症状\n"
                "- 节点 NotReady → 判断主机层 vs K8s 层原因\n"
                "- OOMKilled → limit过低还是内存泄漏？对比 RSS vs limit\n"
                "- CrashLoopBackOff → 退出码分析：137=SIGKILL，1=应用错误，255=镜像/运行时\n"
                "- Service 不可达 → EndpointSlices、标签选择器、就绪探针\n"
                "- 评估影响范围：受影响节点数和工作负载数\n"
                "- 返回验证结论（confirmed/refuted/inconclusive）+ 1~3 条关键证据，供 Coordinator 综合判断\n"
                + _EXPERT_RETURN_SUFFIX
            ),
            "tools": [
                t for t in [
                    check_kubernetes_control_plane, check_kubernetes_nodes, check_kubernetes_pods,
                    *k8s_tools,
                ] if t is not None
            ],
            "skills": [
                "/agent_data/skills/control-plane-diagnosis/",
                "/agent_data/skills/etcd-diagnosis/",
                "/agent_data/skills/coredns-diagnosis/",
                "/agent_data/skills/kubernetes-diagnosis/",
                "/agent_data/skills/container-runtime-diagnosis/",
                "/agent_data/skills/cross-layer-diagnosis/",
                "/agent_data/skills/system-health-check/",
            ],
        },
        # ── Serverless experts (k8s-on-k8s: 逻辑集群 ↔ KMC 控制面 ↔ SCI 数据面) ──
        #
        # 场景 single_pod+serverless 的 6 个专属专家 + host 双专家（host-argus-expert/
        # host-expert 复用）。参数语义（design document §5.2）：argus 专家用 monitor_name
        # （拓扑 mapping），深度专家用物理集群 cluster_name（拓扑 mapping.kmc_cluster /
        # sci_cluster / serverless_cluster）；命名规则见本模块专家 system_prompt。
        {
            "name": "serverless-argus-expert",
            "description": (
                "查询并分析 Serverless 逻辑集群的 Argus 监控指标时序：API 延迟、Pod 稳定计数、节点就绪率。"
                "适用场景：需要逻辑集群视角指标时间线（虚拟集群 API 健康、Pod 稳定性）时委派。"
            ),
            "system_prompt": (
                "你是 Serverless 逻辑集群指标时序分析专家。\n"
                "你的职责是查询和分析 Argus 1min 粒度指标时间线，识别逻辑集群异常时序。\n\n"
                "⚠ 关键约束：你只有 query_argus_serverless_* 与 query_argus_shared_etcd 工具，没有文件工具。\n"
                "工具按监控维度分区：cluster（集群概览）/ node（节点）/ workload（工作负载）/ "
                "pod（单 Pod）/ shared_etcd（共享 etcd）。\n"
                "所有参数必须从消息开头的「Argus 诊断上下文（工具参数契约）」JSON 区块解析（优先级最高）：\n"
                "- monitor_name / cluster_name / 时间窗 ← tool_params（唯一来源）\n"
                "- 诊断目标 namespace / workload_type / workload_name / pod_name ← diagnostic_target\n"
                "- 目标物理落点 pod_name / node_name ← landings；其余资源参数 ← inventory.serverless_views（resource / physical_pods）\n"
                "- 共享 etcd 查询：inventory.etcd_views 条目（host_name 可选）\n"
                "不自造参数；若区块未提供足够参数，回复'需要 <参数名>'，不要模拟工具调用。\n\n"
                "诊断原则：\n"
                "- 先查集群概览（_cluster），异常时按维度深入（_node/_workload/_pod）\n"
                "- 共享 etcd 异常（多集群同时异常优先怀疑）用 query_argus_shared_etcd\n"
                "- 与 KMC/SCI 物理层时序关联由 Coordinator 跨层综合\n"
                "- 按严重程度排序（🔴严重/⚠中等/✅正常）\n"
                + _ARGUS_EXPERT_RETURN_SUFFIX
            ),
            "tools": get_serverless_argus_tools(),
        },
        {
            "name": "serverless-expert",
            "description": (
                "诊断 Serverless 逻辑集群资源：Deployment/Pod/Service/ConfigMap 状态与事件、控制器行为、"
                "Pod 固定 IP 注解。适用场景：逻辑资源协调异常、Pod 状态与物理不符、API 对象错误时委派。"
            ),
            "system_prompt": (
                "你是 Serverless 逻辑集群资源诊断专家（面向用户的虚拟 Kubernetes 集群）。\n\n"
                "⚠ 角色边界：你的职责是验证 Coordinator 委派的特定假设，返回结构化验证结论。\n"
                "你不需要生成完整诊断报告——那是 Coordinator 的职责。\n\n"
                "关键约束：\n"
                "- cluster_name 用逻辑集群名（拓扑 mapping.serverless_cluster）\n"
                "- 逻辑 Pod 的物理落点经「环境拓扑」区块的物理 Pod 锚点（burst-<ns>-<pod>）获取\n"
                "- 对比逻辑状态与 SCI 物理状态（VK 同步链路故障时两视图不一致）\n\n"
                "诊断原则：\n"
                "- 查 Deployment 状态/事件、Pod 生命周期（OOMKilled/CrashLoopBackOff）\n"
                "- 检查 Pod 固定 IP 注解与重建行为（固定 IP 保留）\n"
                "- 返回验证结论（confirmed/refuted/inconclusive）+ 1~3 条关键证据\n"
                + _EXPERT_RETURN_SUFFIX
            ),
            "tools": get_serverless_tools(),
            "skills": [
                "/agent_data/skills/kubernetes-diagnosis/",
            ],
        },
        {
            "name": "kmc-argus-expert",
            "description": (
                "查询并分析 KMC 物理集群的 Argus 监控指标时序：节点健康、Serverless 控制面组件"
                "（Deployment Pod）资源、KMC 自身 etcd。适用场景：需要控制面集群指标时间线时委派。"
            ),
            "system_prompt": (
                "你是 KMC 物理集群（控制面承载）指标时序分析专家。\n"
                "你的职责是查询和分析 Argus 1min 粒度指标时间线，识别控制面集群异常时序。\n\n"
                "⚠ 关键约束：你只有 query_argus_kmc_* 与 query_argus_kmc_etcd 工具，没有文件工具。\n"
                "工具按监控维度分区：cluster（集群概览）/ node（节点）/ workload（工作负载）/ "
                "pod（单 Pod）/ kmc_etcd（KMC 自身 etcd）。\n"
                "所有参数必须从消息开头的「Argus 诊断上下文（工具参数契约）」JSON 区块解析（优先级最高）：\n"
                "- monitor_name / cluster_name / 时间窗 ← tool_params（唯一来源）\n"
                "- 诊断目标及其控制面落点 ← diagnostic_target / landings\n"
                "- 其余资源参数 ← inventory.kmc_views（kmc_resource(kind/ns/name/node_name)）\n"
                "- KMC 自身 etcd 查询：inventory.kmc_views 中 kind=EtcdMember 条目（kmc-etcd-*，host_name 可选）\n"
                "不自造参数；若区块未提供足够参数，回复'需要 <参数名>'，不要模拟工具调用。\n\n"
                "诊断原则：\n"
                "- 识别 KMC 节点健康、控制面组件 CPU/内存、etcd 指标突变时间点\n"
                "- 控制面组件高负载可解释逻辑集群 API 延迟（跨层关联）\n"
                "- 按严重程度排序（🔴严重/⚠中等/✅正常）\n"
                + _ARGUS_EXPERT_RETURN_SUFFIX
            ),
            "tools": get_kmc_argus_tools(),
        },
        {
            "name": "kmc-expert",
            "description": (
                "诊断 KMC 物理集群：Serverless 控制面 Deployment/Pod（含 vpc-cni-controller、etcd-proxy sidecar）、"
                "共享 etcd（5 节点）、共享组件（API Gateway kube-system / Group1-xxx kmc-ingress / IPAM kmc-system）、"
                "KMC 自身控制面与节点。适用场景：逻辑集群 API 超时、多集群同时异常、控制面组件异常时委派。"
            ),
            "system_prompt": (
                "你是 KMC 物理集群诊断专家（承载多个 Serverless 逻辑集群的控制面）。\n\n"
                "⚠ 角色边界：你的职责是验证 Coordinator 委派的特定假设，返回结构化验证结论。\n"
                "你不需要生成完整诊断报告——那是 Coordinator 的职责。\n\n"
                "关键约束：\n"
                "- cluster_name 用物理集群名（拓扑 mapping.kmc_cluster），非逻辑集群名\n"
                "- 控制面命名空间 = <k8s_name>-<master_id>（拓扑 mapping 派生），不自造\n"
                "- 共享组件命名空间：API Gateway `kube-system`、Group1-xxx `kmc-ingress`、IPAM `kmc-system`\n\n"
                "诊断原则：\n"
                "- 控制面 Deployment/Pod（apiserver/cm/vk/vpc-cni-controller）状态与重启\n"
                "- 控制面组件异常时查 Pod 日志定位根因（崩溃/报错）→ get_kmc_pod_logs\n"
                "- 共享 etcd（5 节点，etcd_views）/ KMC 自身 etcd 健康/leader/延迟（多集群同时异常优先怀疑）→ get_kmc_etcd_status\n"
                "- 共享组件链路：API Gateway（VK→SCI）、Group1-xxx（logs/exec 隧道）、IPAM（固定 IP）\n"
                "- 返回验证结论（confirmed/refuted/inconclusive）+ 1~3 条关键证据\n"
                + _EXPERT_RETURN_SUFFIX
            ),
            "tools": get_kmc_tools(),
            "skills": [
                "/agent_data/skills/control-plane-diagnosis/",
                "/agent_data/skills/etcd-diagnosis/",
            ],
        },
        {
            "name": "sci-argus-expert",
            "description": (
                "查询并分析 SCI 物理集群的 Argus 监控指标时序：节点健康、工作负载 Pod（burst-*）"
                "资源/重启/OOM。适用场景：需要数据面集群指标时间线时委派。"
            ),
            "system_prompt": (
                "你是 SCI 物理集群（工作负载执行）指标时序分析专家。\n"
                "你的职责是查询和分析 Argus 1min 粒度指标时间线，识别数据面集群异常时序。\n\n"
                "⚠ 关键约束：你只有 query_argus_sci_* 与 query_argus_sci_etcd 工具，没有文件工具。\n"
                "工具按监控维度分区：cluster（集群概览）/ node（节点）/ workload（工作负载）/ "
                "pod（单 Pod）/ sci_etcd（SCI 自身 etcd）。\n"
                "所有参数必须从消息开头的「Argus 诊断上下文（工具参数契约）」JSON 区块解析（优先级最高）：\n"
                "- monitor_name / cluster_name / 时间窗 ← tool_params（唯一来源）\n"
                "- 诊断目标 SCI Pod ← diagnostic_target.derived_sci_pod；落点 ← landings\n"
                "- boundary.target_sci_pod_absent=true 表示目标在 SCI 物理侧缺席（可能为同步链路故障本体），按缺席事实分析关联指标\n"
                "- 其余资源参数 ← inventory.sci_views（sci_pod.name / sci_pod.node_name）\n"
                "- SCI 自身 etcd 查询：inventory.sci_views 中 kind=EtcdMember 条目（sci-etcd-*，host_name 可选）\n"
                "不自造参数；若区块未提供足够参数，回复'需要 <参数名>'，不要模拟工具调用。\n\n"
                "诊断原则：\n"
                "- 识别 SCI 节点健康、工作负载 Pod 资源/重启/OOM 突变时间点\n"
                "- 工作负载 Pod 名前缀 burst-（逻辑 Pod 的物理形态）\n"
                "- 按严重程度排序（🔴严重/⚠中等/✅正常）\n"
                + _ARGUS_EXPERT_RETURN_SUFFIX
            ),
            "tools": get_sci_argus_tools(),
        },
        {
            "name": "sci-expert",
            "description": (
                "诊断 SCI 物理集群：用户工作负载 Pod（burst-*，固定 IP）、节点/kubelet、自研 VPC-CNI"
                "（kube-system DaemonSet）、IP 分配/冲突、容器运行时。适用场景：Pod Pending/"
                "ContainerCreating、IP 冲突、网络不通、固定 IP 保留失败时委派。"
            ),
            "system_prompt": (
                "你是 SCI 物理集群诊断专家（运行 Serverless 用户工作负载的数据面集群）。\n\n"
                "⚠ 角色边界：你的职责是验证 Coordinator 委派的特定假设，返回结构化验证结论。\n"
                "你不需要生成完整诊断报告——那是 Coordinator 的职责。\n\n"
                "关键约束：\n"
                "- cluster_name 用物理集群名（拓扑 mapping.sci_cluster），非逻辑集群名\n"
                "- 工作负载命名空间 = burst-ns-<master_id>（拓扑 mapping 派生）\n"
                "- 工作负载 Pod 名 = burst-<serverless_ns>-<serverless_pod_name>（命名规则）\n"
                "- 逻辑资源关联经「环境拓扑」区块（serverless_views 的 physical_pods）\n\n"
                "诊断原则：\n"
                "- 用户 Pod 状态/重启/OOM 与固定 IP（VPC-CNI/IPAM 分配记录）\n"
                "- 节点/kubelet 与 VPC-CNI DaemonSet 状态（网络不通/IP 冲突优先怀疑）\n"
                "- 与逻辑集群状态对比（VK 同步链路故障时两视图不一致）\n"
                "- 返回验证结论（confirmed/refuted/inconclusive）+ 1~3 条关键证据\n"
                + _EXPERT_RETURN_SUFFIX
            ),
            "tools": get_sci_tools(),
            "skills": [
                "/agent_data/skills/kubernetes-diagnosis/",
                "/agent_data/skills/container-runtime-diagnosis/",
            ],
        },
    ]

    # Filter experts: scene profile takes precedence (scene-driven assembly).
    if scene_profile is not None:
        _allowed = set(scene_profile.experts)
        subagents = [s for s in subagents if s["name"] in _allowed]
    else:
        # Legacy requests (no scene framework — single-host / dedicated-pod
        # frontends) never use Serverless experts.
        _SLS_EXPERTS = {
            "serverless-argus-expert", "serverless-expert",
            "kmc-argus-expert", "kmc-expert",
            "sci-argus-expert", "sci-expert",
        }
        subagents = [s for s in subagents if s["name"] not in _SLS_EXPERTS]
        if entity_type == "host":
            _K8S_EXPERTS = {"k8s-argus-expert", "k8s-expert"}
            subagents = [s for s in subagents if s["name"] not in _K8S_EXPERTS]

    return subagents


def build_agent(
    settings: Settings | None = None,
    extra_tools: Sequence[Any] = (),
    system_prompt: str | None = None,
    report_path: str = "",
    ledger_path: str = "",
    scene_profile: Any | None = None,
    entity_type: str = "",
    hostname: str = "",
    entity_name: str = "",
    start_time: str = "",
    end_time: str = "",
    param_overrides: dict | None = None,
    topology: dict | None = None,
    topology_unavailable: bool = False,
):
    settings = settings or Settings.from_env()

    # Disable stream chunk timeout for local LLMs (LM Studio)
    os.environ.setdefault("LANGCHAIN_OPENAI_STREAM_CHUNK_TIMEOUT_S", "0")

    # NOTE: use the OllamaChatOpenAI subclass (not plain ChatOpenAI /
    # init_chat_model) so requests carry the legacy top-level
    # "max_tokens" field — the only form ollama honors (see the class
    # docstring in ollama_chat.py for the evidence).
    model = OllamaChatOpenAI(
        model=settings.model,
        base_url=settings.base_url,
        api_key=settings.api_key,
        temperature=settings.temperature,
        max_tokens=settings.max_tokens,
        model_kwargs={"tool_choice": "auto"},
    )

    # Tool visibility: `delete` is stripped via the "openai" harness profile's
    # excluded_tools (see register_harness_profile above), which deepagents
    # applies to both the Coordinator and every subagent inheriting its model.
    # `write_todos` is already absent — deepagents 0.7.x dropped
    # TodoListMiddleware from the default stack (opt-in only), so neither the
    # tool nor its planning prompt reaches the model.

    # Choose tools based on DIAGNOSTICS_MODE: "mock" (default) or "production"
    mode = os.getenv("DIAGNOSTICS_MODE", "mock").lower()
    if mode == "production":
        tools = _get_live_tools(extra_tools)
    else:
        # Coordinator gets host+GPU tools only; K8s tools are
        # exclusive to k8s-expert.
        tools = get_coordinator_mock_tools(extra_tools)

    # Shared backend — used by FilesystemMiddleware (auto-registered by
    # create_deep_agent) and DedupMiddleware for cross-agent cache sharing.
    shared_backend = CompositeBackend(
        default=StateBackend(),
        routes={"/agent_data/": FilesystemBackend(
            root_dir=str(AGENT_DATA_ROOT), virtual_mode=True,
        )},
    )

    subagent_configs = _build_subagents(mode, scene_profile=scene_profile, entity_type=entity_type)

    # Tool dedup middleware — prevents duplicate tool calls (same name+args)
    # within a session, with circuit breaker for repeated failures and
    # cross-agent cache sharing via shared_backend.
    # Must run BEFORE ledger so cache hits bypass P1 blocking.
    dedup_middleware = ToolDedupMiddleware(backend=shared_backend)
    dedup_subagent = ToolDedupMiddleware.for_subagent(dedup_middleware)

    # Diagnosis ledger middleware — maintains hypothesis tree in agent state.
    # Coordinator instance: full features (ledger, P1 expert blocking, safety).
    ledger_middleware = DiagnosisLedgerMiddleware(
        ledger_path=ledger_path,
        report_path=report_path,
        model=model,
        backend=shared_backend,
        entity_type=entity_type,
        hostname=hostname,
        entity_name=entity_name,
        start_time=start_time,
        end_time=end_time,
        param_overrides=param_overrides,
        topology=topology,
        topology_unavailable=topology_unavailable,
        valid_subagents=[sa["name"] for sa in subagent_configs],
    )
    # Subagent instance: shares ledger state, P1 blocking disabled.
    subagent_ledger = DiagnosisLedgerMiddleware.for_subagent(ledger_middleware)

    # Expert file-tool governance — path whitelist, read-window boost,
    # exact-repeat dedup, per-delegation read budget (A1-A4).  Expert-only:
    # the Coordinator's file tools (report writing) stay untouched.
    governance_subagent = ExpertFileToolGovernanceMiddleware()

    # Zero-yield stall watchdog (design document §8 G19, v3.11.0):
    # consecutive executed-but-unproductive calls (empty/no-data/error)
    # — soft wrap-up guidance, then hard block.  Sits AFTER dedup and
    # file governance so it only observes genuinely-executed calls
    # (outer-layer rejections have their own escalation ladders).
    stall_watchdog = ExpertStallWatchdogMiddleware()

    # ── Inject shared middleware into every subagent ──
    # Subagents use subagent_ledger (P1 disabled) and dedup_subagent
    # (shared cache) instead of the Coordinator's full-featured instances.
    # Governance runs before ledger so denied calls skip ledger bookkeeping.
    _subagent_middleware = [dedup_subagent, governance_subagent, stall_watchdog, subagent_ledger]
    for sa in subagent_configs:
        sa["middleware"] = list(_subagent_middleware)

    return create_deep_agent(
        model=model,
        tools=tools,
        system_prompt=system_prompt or make_system_prompt(
            report_path, ledger_path,
            agent_names=[sa["name"] for sa in subagent_configs],
            system_prompt_ref=getattr(scene_profile, "system_prompt_ref", "")),
        backend=shared_backend,
        # NOTE: deepagents' memory= parameter is intentionally NOT used.
        # AGENTS.md is loaded and injected by DiagnosisLedgerMiddleware
        # instead — this drops the irrelevant ~5KB <memory_guidelines>
        # and places memory BEFORE the diagnosis ledger so the live
        # diagnosis state stays at the prompt tail.
        skills=["/agent_data/skills/"],
        subagents=subagent_configs,
        middleware=[dedup_middleware, ledger_middleware],
        state_schema=DiagnosisLedgerState,
    )
