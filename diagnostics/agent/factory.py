from __future__ import annotations

import os
import pathlib
from collections.abc import Sequence
from typing import Any

from deepagents import create_deep_agent
from deepagents.backends import CompositeBackend, FilesystemBackend, StateBackend
from langchain.chat_models import init_chat_model

from diagnostics.agent.prompt import make_system_prompt
from diagnostics.config import Settings
from diagnostics.tools import get_agent_tools as _get_live_tools
from diagnostics.tools import get_k8s_live_tools
from diagnostics.tools.mock import get_mock_tools
from diagnostics.tools.mock.gpu_tools import check_gpu_health, check_gpu_memory, check_gpu_utilization
from diagnostics.tools.mock.kubernetes_tools import check_kubernetes_control_plane, check_kubernetes_nodes, check_kubernetes_pods
from diagnostics.tools.mock.network_tools import check_network
from diagnostics.tools.mock.storage_tools import check_disk
from diagnostics.tools.mock.system_tools import (
    check_cpu,
    check_memory,
    check_processes,
)

PROJECT_ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
AGENT_DATA_ROOT = PROJECT_ROOT / "agent_data"

# Ensure report directory structure exists for hierarchical archiving
_REPORT_DIRS = [
    AGENT_DATA_ROOT / "reports" / "hosts",
    AGENT_DATA_ROOT / "reports" / "kubernetes",
]
for _d in _REPORT_DIRS:
    _d.mkdir(parents=True, exist_ok=True)


def _build_subagents() -> list[dict[str, Any]]:
    """Define specialized diagnostic subagents with domain-specific skills.

    Each subagent has:
    - Precise description for correct routing by main agent
    - Focused tool set (never inherit all tools)
    - Domain skills loaded lazily (skills-first diagnosis)
    - Output format with word limit to keep context clean
    """
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
                "Return under 150 words: key findings, hypothesis, confidence (high/medium/low)."
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
            ),
            "tools": [
                check_gpu_health,
                check_gpu_memory,
                check_gpu_utilization,
            ],
            "skills": ["/agent_data/skills/gpu-diagnosis/"],
        },
        # ── Kubernetes component experts ──
        {
            "name": "k8s-control-plane-expert",
            "description": (
                "Diagnoses Kubernetes control plane issues: API Server timeouts, "
                "etcd disk IO saturation, scheduler failures, controller-manager "
                "crashes, CoreDNS resolution failures. Use when kubectl commands "
                "timeout or API Server is unreachable or returning 5xx errors."
            ),
            "system_prompt": (
                "You are a Kubernetes control plane diagnostics expert. "
                "First, use your control-plane-diagnosis skill to follow the structured workflow. "
                "If etcd is involved, use the etcd-diagnosis skill. "
                "If DNS issues are suspected, use the coredns-diagnosis skill. "
                "For host-to-K8s cascading failures, use the cross-layer-diagnosis skill. "
                "If skills are insufficient, use check_kubernetes_control_plane, "
                "check_kubernetes_nodes, and check_kubernetes_pods to dig deeper. "
                "Return under 150 words: affected component, root cause, "
                "impact on cluster, recommended recovery steps."
            ),
            "tools": [
                check_kubernetes_control_plane, check_kubernetes_nodes, check_kubernetes_pods,
                *get_k8s_live_tools(),
            ],
            "skills": [
                "/agent_data/skills/control-plane-diagnosis/",
                "/agent_data/skills/etcd-diagnosis/",
                "/agent_data/skills/coredns-diagnosis/",
                "/agent_data/skills/cross-layer-diagnosis/",
            ],
        },
        {
            "name": "k8s-workload-expert",
            "description": (
                "Diagnoses Pod lifecycle issues: OOMKilled, CrashLoopBackOff, "
                "ImagePullBackOff, resource limit misconfigurations, container "
                "runtime failures, deployment rollout failures. Use when K8s "
                "workloads are crashing, restarting, or failing to start."
            ),
            "system_prompt": (
                "You are a Kubernetes workload diagnostics expert. "
                "First, use your kubernetes-diagnosis skill for standard Pod/Deployment analysis. "
                "If container runtime issues are suspected, use the container-runtime-diagnosis skill. "
                "For host-to-Pod cascading failures, use the cross-layer-diagnosis skill. "
                "If skills are insufficient, use check_kubernetes_pods and check_kubernetes_nodes. "
                "Identify root cause: resource pressure, image issues, probe failures, "
                "or configuration errors. "
                "Return under 150 words: root cause, affected pods/deployments, "
                "fix (limit adjustment, image fix, or probe tuning)."
            ),
            "tools": [check_kubernetes_pods, check_kubernetes_nodes, *get_k8s_live_tools()],
            "skills": [
                "/agent_data/skills/kubernetes-diagnosis/",
                "/agent_data/skills/container-runtime-diagnosis/",
                "/agent_data/skills/cross-layer-diagnosis/",
            ],
        },
        {
            "name": "k8s-node-expert",
            "description": (
                "Diagnoses Kubernetes node issues: NotReady status, MemoryPressure, "
                "DiskPressure, PIDPressure, kubelet failures, node evictions. "
                "Use when nodes become unhealthy, unschedulable, or show resource exhaustion."
            ),
            "system_prompt": (
                "You are a Kubernetes node diagnostics expert. "
                "First, use your kubernetes-diagnosis skill for standard node analysis. "
                "For host-to-node cascading failures (e.g., disk IO causing kubelet heartbeat "
                "timeout), use the cross-layer-diagnosis skill. "
                "If skills are insufficient, use check_kubernetes_nodes and check_kubernetes_pods. "
                "Check node conditions (MemoryPressure, DiskPressure, PIDPressure, "
                "NetworkUnavailable), status transitions, and eviction events. "
                "Return under 150 words: affected node(s), condition causing issue, "
                "impact on workloads, recommended fix (drain, scale, or resource adjustment)."
            ),
            "tools": [check_kubernetes_nodes, check_kubernetes_pods, *get_k8s_live_tools()],
            "skills": [
                "/agent_data/skills/kubernetes-diagnosis/",
                "/agent_data/skills/cross-layer-diagnosis/",
                "/agent_data/skills/system-health-check/",
            ],
        },
    ]


def build_agent(settings: Settings | None = None, extra_tools: Sequence[Any] = (),
                system_prompt: str | None = None):
    settings = settings or Settings.from_env()

    # Disable stream chunk timeout for local LLMs (LM Studio)
    os.environ.setdefault("LANGCHAIN_OPENAI_STREAM_CHUNK_TIMEOUT_S", "0")

    model = init_chat_model(
        settings.model,
        model_provider="openai",
        base_url=settings.base_url,
        api_key=settings.api_key,
        temperature=settings.temperature,
    )

    # Choose tools based on DIAGNOSTICS_MODE: "mock" (default) or "production"
    mode = os.getenv("DIAGNOSTICS_MODE", "mock").lower()
    if mode == "production":
        tools = _get_live_tools(extra_tools)
    else:
        tools = get_mock_tools(extra_tools)

    return create_deep_agent(
        model=model,
        tools=tools,
        system_prompt=system_prompt or make_system_prompt(),
        backend=CompositeBackend(
            default=StateBackend(),
            routes={"/agent_data/": FilesystemBackend(
                root_dir=str(AGENT_DATA_ROOT), virtual_mode=True,
            )},
        ),
        memory=["/agent_data/AGENTS.md", "/agent_data/LEARNINGS.md"],
        skills=["/agent_data/skills/"],
        subagents=_build_subagents(),
    )
