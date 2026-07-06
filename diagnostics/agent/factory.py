from __future__ import annotations

import os
import pathlib
from collections.abc import Sequence
from typing import Any

from deepagents import create_deep_agent
from deepagents.backends import CompositeBackend, FilesystemBackend, StateBackend
from langchain.chat_models import init_chat_model

from diagnostics.agent.prompt import make_system_prompt
from diagnostics.agent.dedup_middleware import ToolDedupMiddleware
from diagnostics.agent.ledger import DiagnosisLedgerState
from diagnostics.agent.ledger_middleware import DiagnosisLedgerMiddleware
from diagnostics.agent.offload_middleware import ToolOffloadMiddleware
from diagnostics.agent.param_override_middleware import ToolParamOverrideMiddleware
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
    check_gpu_health = check_gpu_memory = check_gpu_utilization = None
    check_conntrack = check_cpu = check_disk = check_dmesg = None
    check_memory = check_network = check_processes = None
    get_system_overview = None
    check_kubernetes_control_plane = check_kubernetes_nodes = check_kubernetes_pods = None

PROJECT_ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
AGENT_DATA_ROOT = PROJECT_ROOT / "agent_data"

# Ensure report directory structure exists for hierarchical archiving
_REPORT_DIRS = [
    AGENT_DATA_ROOT / "reports" / "hosts",
    AGENT_DATA_ROOT / "reports" / "kubernetes",
]
for _d in _REPORT_DIRS:
    _d.mkdir(parents=True, exist_ok=True)

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
    "- 禁止调用台账管理工具（`commit_hypotheses`、`select_path`、`record_finding`）——这些工具仅供 Coordinator 使用。\n"
    "- 避免冗余枚举：概览类工具返回批量数据后，仅对**显示异常**的资源用深度工具核查"
    "（多个资源异常则需全部核查），不对正常的节点/Pod/资源逐一调用同名查询工具。"
    "若多个工具返回同一资源的数据且结论一致，取最早返回的结果即可，无需重复验证。\n"
    "\n**返回格式（严格遵循，不超过 2000 字）**:\n"
    "- 假设验证: {confirmed|refuted|inconclusive}\n"
    "- 关键证据: 1~3条，每条标注数据来源\n"
    "- 根因判断: 如已定位根因则陈述，否则说明还需什么数据\n"
    "- 置信度: {高|中|低} + 百分比"
)

# Return format suffix for Argus time-series analysis experts.
# Argus experts do NOT perform deep diagnosis — they collect and analyse
# 1min-granularity monitoring metrics, returning structured time-series
# correlation findings so Coordinator can form well-grounded hypotheses.
_ARGUS_EXPERT_RETURN_SUFFIX = (
    "\n\n**工具使用边界（严格遵守）**:\n"
    "- 你只有 query_argus_* 监控工具，无文件工具和深度诊断工具。\n"
    "- 禁止调用台账管理工具（`commit_hypotheses`、`select_path`、`record_finding`）——这些工具仅供 Coordinator 使用。\n"
    "\n**返回格式（严格遵循，不超过 600 字）**:\n"
    "- 突变时间点: 列出指标显著变化的时间点及变化值（如 15:03 CPU 36→95%）\n"
    "- 异常排序（按严重程度）: 🔴严重 / ⚠中等 / ✅正常，每条标注具体数值\n"
    "- 并发异常: 同一时间点发生的多个异常（暗示共同根因）\n"
    "- 跨域关联: 不同子系统指标之间的时序因果推断\n"
    "- 初步判断: 基于指标关联的根因推断（一句话）\n"
    "- 置信度: {高|中|低} + 百分比"
)


def _build_subagents(mode: str = "mock", entity_type: str = "") -> list[dict[str, Any]]:
    """Define specialized diagnostic subagents with domain-specific skills.

    Each subagent has:
    - Precise description for correct routing by main agent
    - Focused tool set (never inherit all tools)
    - Domain skills loaded lazily (skills-first diagnosis)
    - Output format with word limit to keep context clean

    When *entity_type* is ``"host"``, Kubernetes-specific experts
    (k8s-argus-expert, k8s-expert) are excluded — the target has no
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
        # Has two specialised tools: query_argus_nodes (node health + Pod
        # stability) and query_argus_services (API Server + etcd + DNS).
        # Host-level metrics (CPU/memory/disk/network) are exclusively owned
        # by host-argus-expert to avoid duplicate queries.
        {
            "name": "k8s-argus-expert",
            "description": (
                "查询并分析 Kubernetes 集群级 Argus 监控指标时序。"
                "工具 query_argus_nodes: 节点状态(NotReady)/Pod重启/驱逐/Pending。"
                "工具 query_argus_services: API延迟/etcd leader/etcd存储/DNS延迟/DNS错误。"
                "适用场景：需要获取K8s集群指标时间线以识别集群异常时序、"
                "控制面稳定性与工作负载状态时委派。"
            ),
            "system_prompt": (
                "你是 Kubernetes 集群指标时序分析专家。\n"
                "你的职责是查询和分析 Argus 1min 粒度的集群指标时间线，"
                "识别集群异常时序与控制面稳定性问题。\n\n"
                "你有两个专用工具：\n"
                "- query_argus_nodes: 节点健康(NotReady) + Pod稳定性(Restarts/Evictions/Pending)\n"
                "- query_argus_services: 控制面(API延迟/etcd leader切换/etcd存储) + DNS(延迟/错误数)\n\n"
                "诊断原则：\n"
                "- 并行调用两个工具，分别获取基础设施层和控制面层的时序数据\n"
                "- 识别每个子系统的突变时间点（指标显著变化的分钟）\n"
                "- 分析跨层时序关联——同一分钟的异常可能共享根因\n"
                "- 区分控制面问题(API/etcd/DNS) vs 节点资源问题(NotReady/Evictions) vs 工作负载问题(Restarts/Pending)\n"
                "- 按严重程度排序（🔴严重/⚠中等/✅正常）\n"
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
                "网络（TCP重传/丢包/连接饱和/ARP/conntrack/MTU/软中断/TCP队列/gRPC泄漏）。"
                "适用场景：主机层面任何异常——高负载、内存不足、IO慢、网络丢包，"
                "或复合症状（如CPU高同时磁盘慢）。能跨子系统关联诊断。"
            ),
            "system_prompt": (
                "你是 Linux 主机全栈诊断专家，覆盖 CPU、内存、磁盘 IO、网络四个子系统。\n\n"
                "诊断原则：\n"
                "- 先看全局（system-health-check），再按症状选择对应技能深入\n"
                "- 注意跨域关联——同一根因常表现跨子系统症状\n\n"
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
                "- 技能工具不足时，使用 check_cpu/check_memory/check_disk/check_network 直接分析\n\n"
                "关键诊断动作：\n"
                "- iowait 高 + 进程 D 状态 → 锁定磁盘 IO 根因\n"
                "- RSS 远超 heap limit → 识别堆外内存泄漏\n"
                "- TCP 重传率 > 10% 但接口无丢包 → 排查 MTU/tc/conntrack\n"
                "- DNS 超时 + conntrack table full → conntrack 导致 UDP 丢包\n"
                "- 给出具体修复方案（命令+参数值）\n"
                + _EXPERT_RETURN_SUFFIX
            ),
            "tools": [t for t in [get_system_overview, check_cpu, check_memory, check_disk, check_network, check_processes, check_conntrack, check_dmesg] if t is not None],
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
            ],
        },
        {
            "name": "gpu-expert",
            "description": (
                "诊断 GPU CUDA OOM、温度/功率限速、利用率异常和 ECC 错误。"
                "适用场景：GPU 密集型工作负载出现异常时。"
            ),
            "system_prompt": (
                "你是 GPU 诊断专家。\n"
                "诊断工作流：\n"
                "1. 优先使用 gpu-diagnosis 技能执行结构化 GPU 诊断流程。\n"
                "2. 如技能工具不足，使用 check_gpu_health、check_gpu_memory、check_gpu_utilization 深入分析。\n"
                "3. 检查顺序：健康状态（温度、限速、ECC、PCIe）→ 显存（OOM、泄漏）→ 利用率（计算 vs 带宽）。\n"
                + _EXPERT_RETURN_SUFFIX
            ),
            "tools": [
                t for t in [
                    check_gpu_health,
                    check_gpu_memory,
                    check_gpu_utilization,
                ] if t is not None
            ],
            "skills": ["/agent_data/skills/gpu-diagnosis/"],
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
                "- 给出具体修复方案（如 memory limit 512Mi→768Mi）\n"
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
    ]

    # Filter out K8s-specific experts when entity_type is "host"
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
    entity_type: str = "",
    param_overrides: dict | None = None,
):
    settings = settings or Settings.from_env()

    # Disable stream chunk timeout for local LLMs (LM Studio)
    os.environ.setdefault("LANGCHAIN_OPENAI_STREAM_CHUNK_TIMEOUT_S", "0")

    model = init_chat_model(
        settings.model,
        model_provider="openai",
        base_url=settings.base_url,
        api_key=settings.api_key,
        temperature=settings.temperature,
        max_tokens=settings.max_tokens,
        model_kwargs={"tool_choice": "auto"},
    )

    # Choose tools based on DIAGNOSTICS_MODE: "mock" (default) or "production"
    mode = os.getenv("DIAGNOSTICS_MODE", "mock").lower()
    if mode == "production":
        tools = _get_live_tools(extra_tools)
    else:
        # Coordinator gets host+GPU tools only; K8s tools are
        # exclusive to k8s-expert.
        tools = get_coordinator_mock_tools(extra_tools)

    # Shared backend — used by both FilesystemMiddleware (auto-registered by
    # create_deep_agent) and ToolOffloadMiddleware so that offloaded files
    # are accessible via built-in read_file / grep / ls tools.
    shared_backend = CompositeBackend(
        default=StateBackend(),
        routes={"/agent_data/": FilesystemBackend(
            root_dir=str(AGENT_DATA_ROOT), virtual_mode=True,
        )},
    )

    # Collect ALL tools (Coordinator + subagents) so the param override
    # middleware has signatures for every tool in the system, including
    # K8s tools that subagents use.
    subagent_configs = _build_subagents(mode, entity_type=entity_type)
    all_tools = list(tools)
    for sa in subagent_configs:
        for t in sa.get("tools", []):
            if t not in all_tools:
                all_tools.append(t)

    # Parameter override middleware — full replacement of tool call arguments
    # with frontend-provided values.  All matching params are unconditionally
    # overwritten, regardless of what the LLM generates.
    # Must run BEFORE dedup middleware so dedup sees corrected args.
    param_middleware = ToolParamOverrideMiddleware(
        config=param_overrides, tools=all_tools,
    ) if param_overrides else None

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
        backend=shared_backend,  # shared with FilesystemMiddleware + OffloadMiddleware
    )
    # Subagent instance: shares ledger state, P1 blocking disabled.
    subagent_ledger = DiagnosisLedgerMiddleware.for_subagent(ledger_middleware)

    # Tool offload middleware — compresses oversized tool results by writing
    # full content to the backend and replacing with a head+tail preview.
    # The LLM can then use read_file/grep to retrieve specific information.
    # hint_keywords provides per-tool search suggestions (starting points,
    # not exhaustive — the LLM is free to explore other patterns).
    #
    # Pipe-isolated: runs in the `wrap_model_call` pipeline (not
    # `awrap_tool_call`).  Tool-execution middlewares (ledger, dedup)
    # always see the original, unmodified tool result.
    # Ordering relative to those middlewares is irrelevant.
    offload_middleware = ToolOffloadMiddleware(
        tool_names={
            # Host diagnostic tools (may return large outputs)
            "check_cpu", "check_memory", "check_disk", "check_network",
            "check_processes", "check_conntrack", "check_dmesg",
            # K8s diagnostic tools
            "check_kubernetes_pods", "check_kubernetes_nodes",
            "check_kubernetes_control_plane",
            # GPU tools
            "check_gpu_health", "check_gpu_memory", "check_gpu_utilization",
            # Argus monitoring tools (time-series data can be very large)
            "query_argus_cpu", "query_argus_memory",
            "query_argus_disk", "query_argus_network",
            "query_argus_nodes", "query_argus_services",
            "query_argus_gpu",
            "query_argus_cpu_metrics", "query_argus_memory_metrics",
            "query_argus_disk_metrics", "query_argus_network_metrics",
            "query_argus_pod_logs",
        },
        token_limit=8000,
        backend=shared_backend,
        hint_keywords={
            # Host tools: common fault indicators
            "check_cpu": ["D-state", "iowait", "ksoftirqd", "kworker", "steal"],
            "check_memory": ["OOM", "oom_kill", "Swap", "dirty_ratio", "slab"],
            "check_disk": ["await", "util", "iowait", "D-state", "IO_SCHED"],
            "check_network": ["retrans", "drops", "conntrack", "softirq", "mtu"],
            "check_processes": ["OOM", "zombie", "D-state", "RSS", "oom_score"],
            "check_conntrack": ["table_full", "nf_conntrack_max", "drop", "insert_failed"],
            "check_dmesg": ["OOM", "oom_kill", "segfault", "BUG", "Call Trace", "error"],
            # K8s tools: common failure patterns
            "check_kubernetes_pods": ["CrashLoopBackOff", "OOMKilled", "Error", "ImagePullBackOff", "Evicted", "Pending"],
            "check_kubernetes_nodes": ["NotReady", "MemoryPressure", "DiskPressure", "PIDPressure", "NetworkUnavailable"],
            "check_kubernetes_control_plane": ["etcd", "leader", "timeout", "latency", "certificate"],
            # GPU tools
            "check_gpu_health": ["XID", "ECC", "throttle", "temperature", "power_limit"],
            "check_gpu_memory": ["OOM", "leak", "fragmentation", "BAR1"],
            "check_gpu_utilization": ["stall", "SM", "memory_bandwidth", "pcie"],
            # Argus monitoring tools: anomaly patterns
            "query_argus_cpu": ["spike", "iowait", "steal", "softirq"],
            "query_argus_memory": ["OOM", "swap", "RSS", "available"],
            "query_argus_disk": ["await", "util", "latency", "IO"],
            "query_argus_network": ["retransmit", "drops", "DNS", "timeout"],
            "query_argus_nodes": ["NotReady", "restart", "eviction", "pending"],
            "query_argus_services": ["etcd", "leader", "DNS", "latency", "error"],
            "query_argus_pod_logs": ["OOMKilled", "CrashLoop", "OutOfMemory", "exit code", "FATAL"],
        },
    )

    # ── Inject all shared middleware into every subagent ──
    # Subagents use subagent_ledger (P1 disabled) and dedup_subagent
    # (shared cache) instead of the Coordinator's full-featured instances.
    _subagent_middleware = [
        m for m in [param_middleware, dedup_subagent, subagent_ledger, offload_middleware]
        if m is not None
    ]
    if _subagent_middleware:
        for sa in subagent_configs:
            sa["middleware"] = list(_subagent_middleware)

    return create_deep_agent(
        model=model,
        tools=tools,
        system_prompt=system_prompt or make_system_prompt(report_path, ledger_path),
        backend=shared_backend,
        memory=["/agent_data/AGENTS.md", "/agent_data/LEARNINGS.md"],
        skills=["/agent_data/skills/"],
        subagents=subagent_configs,
        middleware=[m for m in [param_middleware, dedup_middleware, ledger_middleware, offload_middleware] if m is not None],
        state_schema=DiagnosisLedgerState,
    )
