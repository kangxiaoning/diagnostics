from __future__ import annotations

import os
import pathlib
from collections.abc import Sequence
from typing import Any

from deepagents import create_deep_agent
from deepagents.backends import CompositeBackend, FilesystemBackend, StateBackend
from langchain.chat_models import init_chat_model

from diagnostics.agent.prompt import SYSTEM_PROMPT
from diagnostics.config import Settings
from diagnostics.tools import get_agent_tools
from diagnostics.tools.gpu_tools import check_gpu_health, check_gpu_memory, check_gpu_utilization
from diagnostics.tools.kubernetes_tools import check_kubernetes_control_plane, check_kubernetes_nodes, check_kubernetes_pods
from diagnostics.tools.network_tools import check_network
from diagnostics.tools.storage_tools import check_disk
from diagnostics.tools.system_tools import (
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
    """Define specialized diagnostic subagents following deepagents best practices.

    Each subagent has:
    - Precise description for correct routing by main agent
    - Focused tool set (never inherit all tools)
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
                "You are a CPU diagnostics expert. Check vmstat r/b columns, "
                "iowait percentage, and process D-states. Classify whether "
                "bottleneck is compute-bound, IO-wait-bound, or scheduler-bound. "
                "Return under 150 words: key findings, hypothesis, confidence (high/medium/low)."
            ),
            "tools": [check_cpu, check_processes],
        },
        {
            "name": "memory-expert",
            "description": (
                "Diagnoses memory leaks, OOM risks, and swap thrashing. "
                "Use when memory is exhausted, RSS grows unbounded, or swap usage increases."
            ),
            "system_prompt": (
                "You are a memory diagnostics expert. Check free/available ratios, "
                "swap usage trends, and per-process RSS. Identify native memory leaks "
                "(RSS >> heap limit) and swap pressure. "
                "Return under 150 words: key findings, root cause hypothesis, fix recommendation."
            ),
            "tools": [check_memory, check_processes],
        },
        {
            "name": "disk-io-expert",
            "description": (
                "Investigates disk IO saturation and storage latency. "
                "Use when iowait exceeds 30% or apps report slow disk operations."
            ),
            "system_prompt": (
                "You are a storage performance expert. Analyze %util, await, "
                "svctm, aqu-sz from iostat. High %util + high await = saturation. "
                "Determine if bottleneck is read or write bound. "
                "Return under 100 words: severity, root cause, which processes affected."
            ),
            "tools": [check_disk],
        },
        {
            "name": "network-expert",
            "description": (
                "Diagnoses TCP retransmits, packet loss, connection saturation, "
                "CLOSE-WAIT leaks, and interface errors. Use when network latency "
                "or drops are suspected."
            ),
            "system_prompt": (
                "You are a network diagnostics expert. Check ss connection state, "
                "interface errors/drops, TCP retransmit rate, and CLOSE-WAIT counts. "
                "High retransmits + Send-Q backlog = congestion. Many CLOSE-WAIT = app socket leak. "
                "Return under 150 words: key findings, root cause, recommended fix."
            ),
            "tools": [check_network, check_processes],
        },
        {
            "name": "gpu-expert",
            "description": (
                "Diagnoses GPU CUDA OOM, thermal/power throttling, utilization gaps, "
                "and ECC errors. Use for GPU-intensive workloads."
            ),
            "system_prompt": (
                "You are a GPU diagnostics expert. Check health first (temperature, "
                "throttling, ECC, PCIe), then memory (OOM, leaks), then utilization "
                "(compute vs memory bandwidth). "
                "Return under 150 words: bottleneck type, affected GPU, recommended action."
            ),
            "tools": [
                check_gpu_health,
                check_gpu_memory,
                check_gpu_utilization,
            ],
        },
        # ── Kubernetes component experts ──
        {
            "name": "k8s-control-plane-expert",
            "description": (
                "Diagnoses Kubernetes control plane issues: API Server timeouts, "
                "etcd disk IO saturation, scheduler failures, controller-manager "
                "crashes. Use when kubectl commands timeout or API Server is "
                "unreachable or returning 5xx errors."
            ),
            "system_prompt": (
                "You are a Kubernetes control plane diagnostics expert. Check "
                "API Server health, etcd latency/raft index, scheduler leader, "
                "and controller-manager status. Correlate with node conditions "
                "and pod statuses when needed. "
                "Return under 150 words: affected component, root cause, "
                "impact on cluster, recommended recovery steps."
            ),
            "tools": [check_kubernetes_control_plane, check_kubernetes_nodes, check_kubernetes_pods],
        },
        {
            "name": "k8s-workload-expert",
            "description": (
                "Diagnoses Pod lifecycle issues: OOMKilled, CrashLoopBackOff, "
                "ImagePullBackOff, resource limit misconfigurations, deployment "
                "rollout failures. Use when K8s workloads are crashing, restarting, "
                "or failing to start."
            ),
            "system_prompt": (
                "You are a Kubernetes workload diagnostics expert. Analyze pod "
                "statuses, restart counts, termination reasons (OOMKilled, Error, "
                "Completed), container resource limits vs actual usage, and "
                "deployment rollout status. Identify root cause: resource pressure, "
                "image issues, probe failures, or configuration errors. "
                "Return under 150 words: root cause, affected pods/deployments, "
                "fix (limit adjustment, image fix, or probe tuning)."
            ),
            "tools": [check_kubernetes_pods, check_kubernetes_nodes],
        },
        {
            "name": "k8s-node-expert",
            "description": (
                "Diagnoses Kubernetes node issues: NotReady status, MemoryPressure, "
                "DiskPressure, PIDPressure, kubelet failures, node evictions. "
                "Use when nodes become unhealthy, unschedulable, or show resource "
                "exhaustion."
            ),
            "system_prompt": (
                "You are a Kubernetes node diagnostics expert. Check node conditions "
                "(MemoryPressure, DiskPressure, PIDPressure, NetworkUnavailable), "
                "node status transitions, resource utilization per node, and "
                "eviction events. Correlate with system-level checks when needed. "
                "Return under 150 words: affected node(s), condition causing issue, "
                "impact on workloads, recommended fix (drain, scale, or resource adjustment)."
            ),
            "tools": [check_kubernetes_nodes, check_kubernetes_pods],
        },
    ]


def build_agent(settings: Settings | None = None, extra_tools: Sequence[Any] = ()):
    settings = settings or Settings.from_env()

    # Disable stream chunk timeout for local LLMs (LM Studio)
    # LM Studio loads models on-demand, first request may take 10-60s
    # before producing the first token.
    os.environ.setdefault("LANGCHAIN_OPENAI_STREAM_CHUNK_TIMEOUT_S", "0")

    model = init_chat_model(
        settings.model,
        model_provider="openai",
        base_url=settings.base_url,
        api_key=settings.api_key,
        temperature=settings.temperature,
    )
    return create_deep_agent(
        model=model,
        tools=get_agent_tools(extra_tools),
        system_prompt=SYSTEM_PROMPT,
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
