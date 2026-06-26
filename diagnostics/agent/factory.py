from __future__ import annotations

import os
import pathlib
from collections.abc import Sequence
from typing import Any

from deepagents import create_deep_agent
from deepagents.backends import CompositeBackend, FilesystemBackend, StateBackend
from langchain.chat_models import init_chat_model

from diagnostics.agent.prompt import make_system_prompt
from diagnostics.agent.ledger import DiagnosisLedgerState
from diagnostics.agent.ledger_middleware import DiagnosisLedgerMiddleware
from diagnostics.config import Settings
from diagnostics.tools import get_agent_tools as _get_live_tools
from diagnostics.tools import get_k8s_live_tools
from diagnostics.tools.mock import get_k8s_mock_tools, get_mock_tools
from diagnostics.tools.mock.gpu import check_gpu_health, check_gpu_memory, check_gpu_utilization
from diagnostics.tools.mock.hosts import (
    check_cpu,
    check_disk,
    check_memory,
    check_network,
    check_processes,
)
from diagnostics.tools.mock.kubernetes import check_kubernetes_control_plane, check_kubernetes_nodes, check_kubernetes_pods

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
_EXPERT_RETURN_SUFFIX = (
    "\n\n**工具使用边界（严格遵守）**: "
    "`read_file`/`grep`/`glob`/`ls` 只能访问 `/agent_data/**`，"
    "禁止访问 `/proc/**`、`/sys/**`、`/var/log/**`、`/etc/**` 等被诊断主机路径。"
    "远程主机数据必须通过专用诊断工具（如 `check_network`、`get_node_diagnostics`）获取。"
    "\n\n返回格式（严格遵循）:\n"
    "- 假设验证: {confirmed|refuted|inconclusive}\n"
    "- 关键证据: 1~3条，每条标注数据来源\n"
    "- 根因判断: 如已定位根因则陈述，否则说明还需什么数据\n"
    "- 置信度: {高|中|低} + 百分比"
)


def _build_subagents(mode: str = "mock") -> list[dict[str, Any]]:
    """Define specialized diagnostic subagents with domain-specific skills.

    Each subagent has:
    - Precise description for correct routing by main agent
    - Focused tool set (never inherit all tools)
    - Domain skills loaded lazily (skills-first diagnosis)
    - Output format with word limit to keep context clean
    """
    k8s_tools = get_k8s_mock_tools() if mode == "mock" else get_k8s_live_tools()
    return [
        {
            "name": "cpu-expert",
            "description": (
                "Analyzes CPU utilization, load averages, iowait, and process D-states. "
                "Use when load average exceeds CPU cores or iowait exceeds 30%."
            ),
            "system_prompt": (
                "你是 CPU 诊断专家。\n"
                "诊断工作流：\n"
                "1. 优先使用 cpu-diagnosis 技能执行结构化 CPU 诊断流程。\n"
                "2. 如技能工具不足，使用 check_cpu 和 check_processes 深入分析。\n"
                "3. 判断瓶颈类型：计算密集、IO 等待或调度竞争。\n"
                "返回不超过 150 字：瓶颈类型、关键进程、修复建议。" + _EXPERT_RETURN_SUFFIX
            ),
            "tools": [check_cpu, check_processes],
            "skills": [
                "/agent_data/skills/cpu-diagnosis/",
                "/agent_data/skills/system-health-check/",
            ],
        },
        {
            "name": "memory-expert",
            "description": (
                "Diagnoses memory leaks, OOM risks, and swap thrashing. "
                "Use when memory is exhausted, RSS grows unbounded, or swap usage increases."
            ),
            "system_prompt": (
                "你是内存诊断专家。\n"
                "诊断工作流：\n"
                "1. 优先使用 memory-diagnosis 技能执行结构化内存诊断流程。\n"
                "2. 如技能工具不足，使用 check_memory 和 check_processes 深入分析。\n"
                "3. 识别原生内存泄漏（RSS >> heap limit）和 Swap 压力。\n"
                "返回不超过 150 字：关键发现、根因假设、修复建议。"
                + _EXPERT_RETURN_SUFFIX
            ),
            "tools": [check_memory, check_processes],
            "skills": [
                "/agent_data/skills/memory-diagnosis/",
                "/agent_data/skills/system-health-check/",
            ],
        },
        {
            "name": "disk-io-expert",
            "description": (
                "Investigates disk IO saturation and storage latency. "
                "Use when iowait exceeds 30% or apps report slow disk operations."
            ),
            "system_prompt": (
                "你是存储 IO 性能诊断专家。\n"
                "诊断工作流：\n"
                "1. 优先使用 disk-io-diagnosis 技能执行结构化 IO 诊断流程。\n"
                "2. 如技能工具不足，使用 check_disk 深入分析。\n"
                "3. 分析 iostat 的 %util、await、svctm、aqu-sz；高 %util + 高 await = IO 饱和。\n"
                "4. 判断瓶颈是读密集还是写密集。\n"
                "返回不超过 150 字：严重程度、根因、受影响进程。"
                + _EXPERT_RETURN_SUFFIX
            ),
            "tools": [check_disk],
            "skills": ["/agent_data/skills/disk-io-diagnosis/"],
        },
        {
            "name": "network-expert",
            "description": (
                "Diagnoses TCP retransmits, packet loss, connection saturation, "
                "CLOSE-WAIT leaks, interface errors, and kernel network issues "
                "(ARP cache, conntrack, MTU, softirq, TCP queue overflow, gRPC leaks). "
                "Use when network latency or drops are suspected."
            ),
            "system_prompt": (
                "你是网络诊断专家。\n"
                "诊断工作流：\n"
                "1. 标准 TCP/连接分析优先使用 network-diagnosis 技能。\n"
                "2. 如症状指向内核网络问题，按场景选用对应技能：\n"
                "   arp-cache-diagnosis（ARP）、conntrack-diagnosis（conntrack 表满）、\n"
                "   mtu-misconfig-diagnosis（MTU 错配）、softirq-starvation（软中断）、\n"
                "   kernel-parameter-drops（内核参数丢包）、tcp-listen-overflow（TCP 队列溢出）、\n"
                "   grpc-connection-leak（gRPC 连接泄漏）。\n"
                "3. 如技能工具不足，使用 check_network 和 check_processes 深入分析。\n"
                "返回不超过 150 字：关键发现、根因、修复建议。"
                + _EXPERT_RETURN_SUFFIX
            ),
            "tools": [check_network, check_processes],
            "skills": [
                "/agent_data/skills/network-diagnosis/",
                "/agent_data/skills/arp-cache-diagnosis/",
                "/agent_data/skills/conntrack-diagnosis/",
                "/agent_data/skills/mtu-misconfig-diagnosis/",
                "/agent_data/skills/softirq-starvation/",
                "/agent_data/skills/kernel-parameter-drops/",
                "/agent_data/skills/tcp-listen-overflow/",
                "/agent_data/skills/grpc-connection-leak/",
            ],
        },
        {
            "name": "gpu-expert",
            "description": (
                "Diagnoses GPU CUDA OOM, thermal/power throttling, utilization gaps, "
                "and ECC errors. Use for GPU-intensive workloads."
            ),
            "system_prompt": (
                "你是 GPU 诊断专家。\n"
                "诊断工作流：\n"
                "1. 优先使用 gpu-diagnosis 技能执行结构化 GPU 诊断流程。\n"
                "2. 如技能工具不足，使用 check_gpu_health、check_gpu_memory、check_gpu_utilization 深入分析。\n"
                "3. 检查顺序：健康状态（温度、限速、ECC、PCIe）→ 显存（OOM、泄漏）→ 利用率（计算 vs 带宽）。\n"
                "返回不超过 150 字：瓶颈类型、受影响 GPU、修复建议。"
                + _EXPERT_RETURN_SUFFIX
            ),
            "tools": [
                check_gpu_health,
                check_gpu_memory,
                check_gpu_utilization,
            ],
            "skills": ["/agent_data/skills/gpu-diagnosis/"],
        },
        # ── Kubernetes experts (2-tier: cluster infrastructure vs workloads) ──
        #
        # Boundary follows Kubernetes architecture:
        #   - Cluster infrastructure: control plane + nodes + cluster networking + storage.
        #     These are the "brain and limbs" — they DON'T run user Pods but must be
        #     healthy for workloads to function. Cluster-expert diagnoses WHY the
        #     cluster is degraded.
        #   - Workloads: Pods, Deployments, StatefulSets, Services, Helm releases.
        #     These ARE the user applications. Workload-expert diagnoses WHY apps
        #     are crashing, failing to start, or unreachable.
        #
        # When host issues cascade into K8s (e.g., disk IO → kubelet lease timeout →
        # Node NotReady → Pod eviction), cluster-expert uses cross-layer-diagnosis
        # to connect the dots. Workload-expert focuses on application-level root cause.
        {
            "name": "k8s-cluster-expert",
            "description": (
                "Diagnoses Kubernetes cluster infrastructure health: API Server, etcd, "
                "scheduler, controller-manager, CoreDNS, node conditions (NotReady, "
                "MemoryPressure, DiskPressure, PIDPressure), kubelet failures, node "
                "evictions, RBAC, certificates, webhooks, network policies, and PV/PVC. "
                "Use when kubectl times out, API Server returns 5xx, nodes are NotReady, "
                "or cluster-wide events indicate infrastructure degradation. "
                "For host-to-K8s cascading failures (e.g., host disk IO → kubelet "
                "heartbeat timeout → Node NotReady → Pod eviction), use cross-layer "
                "diagnosis to trace the chain."
            ),
            "system_prompt": (
                "你是 Kubernetes 集群基础设施诊断专家。\n"
                "职责范围：控制面（API Server、etcd、scheduler、controller-manager）、节点健康（kubelet、状态、压力、驱逐）、集群网络（CoreDNS、kube-proxy）、集群存储（PV/PVC）。\n\n"
                "诊断工作流：\n"
                "1. 按优先级使用技能：API/etcd/scheduler 问题用 control-plane-diagnosis，etcd 深度分析用 etcd-diagnosis，DNS 解析失败用 coredns-diagnosis。\n"
                "2. 主机级联故障（主机 IO/OOM → 节点 NotReady）使用 cross-layer-diagnosis 关联主机指标与 K8s 症状。\n"
                "3. 标准节点和集群分析使用 kubernetes-diagnosis。\n"
                "4. 技能工具不足时，使用 check_kubernetes_control_plane、check_kubernetes_nodes 和集群范围 K8s 工具。\n\n"
                "核心职责：\n"
                "- 区分集群基础设施根因与工作负载症状。\n"
                "- 节点 NotReady 时，判断是主机层（磁盘/CPU/内存/网络）还是 K8s 层（kubelet 崩溃/配置）导致。\n"
                "- 评估影响范围：受影响节点数和工作负载数。\n"
                "返回不超过 150 字：受影响基础设施组件、证据链根因、影响范围（节点/Pod 数）、恢复步骤。"
                + _EXPERT_RETURN_SUFFIX
            ),
            "tools": [
                check_kubernetes_control_plane, check_kubernetes_nodes, check_kubernetes_pods,
                *k8s_tools,
            ],
            "skills": [
                "/agent_data/skills/control-plane-diagnosis/",
                "/agent_data/skills/etcd-diagnosis/",
                "/agent_data/skills/coredns-diagnosis/",
                "/agent_data/skills/kubernetes-diagnosis/",
                "/agent_data/skills/cross-layer-diagnosis/",
                "/agent_data/skills/system-health-check/",
            ],
        },
        {
            "name": "k8s-workload-expert",
            "description": (
                "Diagnoses Kubernetes workload issues: Pod crashes (OOMKilled, "
                "CrashLoopBackOff, Error, ImagePullBackOff), Deployment/StatefulSet "
                "rollout failures, Service endpoint problems (no healthy backends, "
                "label selector mismatch), Ingress routing failures, Helm release "
                "failures, container runtime errors, and resource misconfiguration "
                "(limits too low, requests not set, probes misconfigured). "
                "Use when Pods are crashing, restarting, failing to start, or "
                "traffic is not reaching workloads. "
                "Only invoke AFTER cluster-expert confirms infrastructure is healthy, "
                "unless symptoms clearly point to application-level issues "
                "(e.g., OOMKilled with sufficient node memory, ImagePullBackOff)."
            ),
            "system_prompt": (
                "你是 Kubernetes 工作负载诊断专家。\n"
                "职责范围：Pod 生命周期、控制器（Deployment/StatefulSet/DaemonSet）、Service、Ingress、Helm 发布、容器运行时。\n\n"
                "诊断工作流：\n"
                "1. 标准 Pod/Deployment 排障优先使用 kubernetes-diagnosis 技能。\n"
                "2. 容器运行时疑似问题（镜像拉取、运行时崩溃）使用 container-runtime-diagnosis 技能。\n"
                "3. 集群专家已识别基础设施故障时，使用 cross-layer-diagnosis 评估对工作负载的影响。\n"
                "4. 技能工具不足时，使用 check_kubernetes_pods 和工作负载范围 K8s 工具。\n\n"
                "关键诊断问题：\n"
                "- 根因是应用层（配置错误、资源限制、镜像、探针）还是继承自集群基础设施？\n"
                "- OOMKilled：是 limit 过低还是内存泄漏？对比重启历史的 RSS vs limit。\n"
                "- CrashLoopBackOff：检查退出码，137=SIGKILL（OOM/探针），1=应用错误，255=镜像/运行时。\n"
                "- Service 不可达：检查 EndpointSlices、标签选择器、就绪探针。\n"
                "- Helm 升级失败：对比版本值和 Pod spec 变化。\n"
                "- 给出具体修复方案（如将 memory limit 从 512Mi 调整为 768Mi）。\n"
                "返回不超过 150 字：根因类型（应用/基础设施）、受影响资源、证据链、修复方案（含具体值）、回滚计划。"
                + _EXPERT_RETURN_SUFFIX
            ),
            "tools": [check_kubernetes_pods, check_kubernetes_nodes, *k8s_tools],
            "skills": [
                "/agent_data/skills/kubernetes-diagnosis/",
                "/agent_data/skills/container-runtime-diagnosis/",
                "/agent_data/skills/cross-layer-diagnosis/",
            ],
        },
    ]


def build_agent(
    settings: Settings | None = None,
    extra_tools: Sequence[Any] = (),
    system_prompt: str | None = None,
    report_path: str = "",
    ledger_path: str = "",
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
        model_kwargs={"tool_choice": "auto"},
    )

    # Choose tools based on DIAGNOSTICS_MODE: "mock" (default) or "production"
    mode = os.getenv("DIAGNOSTICS_MODE", "mock").lower()
    if mode == "production":
        tools = _get_live_tools(extra_tools)
    else:
        tools = get_mock_tools(extra_tools)

    # Diagnosis ledger middleware — maintains hypothesis tree in agent state
    ledger_middleware = DiagnosisLedgerMiddleware(
        ledger_path=ledger_path,
        report_path=report_path,
    )

    return create_deep_agent(
        model=model,
        tools=tools,
        system_prompt=system_prompt or make_system_prompt(report_path, ledger_path),
        backend=CompositeBackend(
            default=StateBackend(),
            routes={"/agent_data/": FilesystemBackend(
                root_dir=str(AGENT_DATA_ROOT), virtual_mode=True,
            )},
        ),
        memory=["/agent_data/AGENTS.md", "/agent_data/LEARNINGS.md"],
        skills=["/agent_data/skills/"],
        subagents=_build_subagents(mode),
        middleware=[ledger_middleware],
        state_schema=DiagnosisLedgerState,
    )
