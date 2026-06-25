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
                "You are a CPU diagnostics expert. "
                "First, use your cpu-diagnosis skill to follow the structured CPU diagnostic workflow. "
                "If the skill's tools are insufficient, use check_cpu and check_processes to dig deeper. "
                "Classify whether bottleneck is compute-bound, IO-wait-bound, or scheduler-bound. "
                "Return under 150 words." + _EXPERT_RETURN_SUFFIX
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
                "You are a memory diagnostics expert. "
                "First, use your memory-diagnosis skill to follow the structured memory diagnostic workflow. "
                "If the skill's tools are insufficient, use check_memory and check_processes to dig deeper. "
                "Identify native memory leaks (RSS >> heap limit) and swap pressure. "
                "Return under 150 words: key findings, root cause hypothesis, fix recommendation."
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
                "You are a storage performance expert. "
                "First, use your disk-io-diagnosis skill to follow the structured IO diagnostic workflow. "
                "If the skill's tools are insufficient, use check_disk to dig deeper. "
                "Analyze %util, await, svctm, aqu-sz from iostat. High %util + high await = saturation. "
                "Determine if bottleneck is read or write bound. "
                "Return under 100 words: severity, root cause, which processes affected."
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
                "You are a network diagnostics expert. "
                "First, use your network-diagnosis skill for standard TCP/connection analysis. "
                "If symptoms suggest kernel-level issues, use the relevant kernel networking skills "
                "(arp-cache-diagnosis, conntrack-diagnosis, mtu-misconfig-diagnosis, "
                "softirq-starvation, kernel-parameter-drops, tcp-listen-overflow, grpc-connection-leak). "
                "If skills are insufficient, use check_network and check_processes to dig deeper. "
                "Return under 150 words: key findings, root cause, recommended fix."
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
                "You are a GPU diagnostics expert. "
                "First, use your gpu-diagnosis skill to follow the structured GPU diagnostic workflow. "
                "If the skill's tools are insufficient, use check_gpu_health, check_gpu_memory, "
                "and check_gpu_utilization to dig deeper. "
                "Check health first (temperature, throttling, ECC, PCIe), then memory (OOM, leaks), "
                "then utilization (compute vs memory bandwidth). "
                "Return under 150 words: bottleneck type, affected GPU, recommended action."
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
                "You are a Kubernetes cluster infrastructure diagnostics expert. "
                "Your domain covers everything below the workload layer: control plane "
                "components (API Server, etcd, scheduler, controller-manager), node "
                "health (kubelet, conditions, pressure states, evictions), cluster "
                "networking (CoreDNS, kube-proxy), and cluster storage (PV/PVC). "
                "\n\n"
                "Diagnostic workflow:\n"
                "1. First, use your skills in priority order: control-plane-diagnosis "
                "for API/etcd/scheduler issues, etcd-diagnosis for etcd deep-dive, "
                "coredns-diagnosis for DNS resolution failures.\n"
                "2. For host-to-node cascading failures (node NotReady caused by host "
                "disk IO or OOM), use cross-layer-diagnosis to connect host metrics "
                "to K8s symptoms.\n"
                "3. Use kubernetes-diagnosis for standard node and cluster analysis.\n"
                "4. Only if skills are exhausted, use raw tools: check_kubernetes_control_plane, "
                "check_kubernetes_nodes, plus cluster-scoped K8s tools.\n"
                "\n"
                "Key responsibilities:\n"
                "- Distinguish cluster-infrastructure root cause from workload symptoms.\n"
                "- When nodes are NotReady, determine whether cause is host-level "
                "(disk/CPU/memory/network) or K8s-level (kubelet crash/config).\n"
                "- Assess blast radius: how many nodes and workloads are affected.\n"
                "\n"
                "Return under 200 words: affected infrastructure component, root cause "
                "with evidence chain, impact scope (nodes/pods affected), recovery steps."
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
                "You are a Kubernetes workload diagnostics expert. "
                "Your domain covers application-layer concerns: Pod lifecycles, "
                "controllers (Deployment, StatefulSet, DaemonSet), Services, "
                "Ingress, Helm-managed releases, and container runtime behavior "
                "from the workload perspective.\n\n"
                "Diagnostic workflow:\n"
                "1. First, use kubernetes-diagnosis skill for standard Pod/Deployment "
                "troubleshooting.\n"
                "2. If container runtime is suspected (image pull, runtime crash), "
                "use container-runtime-diagnosis skill.\n"
                "3. If cluster-expert has identified infrastructure degradation, use "
                "cross-layer-diagnosis to assess how that degradation manifests at "
                "the workload layer and which workloads are impacted.\n"
                "4. Only if skills are exhausted, use raw tools: check_kubernetes_pods, "
                "plus workload-scoped K8s tools.\n\n"
                "Key diagnostic questions to answer:\n"
                "- Is the root cause application-level (misconfig, resource limit, "
                "image issue, probe tuning) or inherited from cluster infrastructure?\n"
                "- If OOMKilled: is the limit too low for actual usage, or is there "
                "a memory leak? Compare RSS vs limit across restart history.\n"
                "- If CrashLoopBackOff: examine exit code and previous logs. 137=SIGKILL "
                "(OOM/probe), 1=app error, 255=image/runtime.\n"
                "- If Service unreachable: check EndpointSlices, label selectors, "
                "and readiness probes.\n"
                "- If Helm upgrade failed: compare revision values and pod spec changes.\n"
                "- Always suggest concrete fix: limit adjustment, probe tuning, "
                "image tag pinning, or rollback.\n\n"
                "Return under 200 words: root cause type (app/infra), affected resources, "
                "evidence chain, fix with specific values (e.g., 'increase memory limit "
                "from 512Mi to 768Mi'), rollback plan if applicable."
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
