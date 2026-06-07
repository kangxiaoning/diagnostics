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
from diagnostics.tools.kubernetes_tools import check_kubernetes_nodes, check_kubernetes_pods
from diagnostics.tools.network_tools import check_network
from diagnostics.tools.storage_tools import check_disk
from diagnostics.tools.system_tools import (
    check_cpu,
    check_memory,
    check_processes,
)

PROJECT_ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
AGENT_DATA_ROOT = PROJECT_ROOT / "agent_data"


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
        {
            "name": "kubernetes-expert",
            "description": (
                "Diagnoses pod crashes, OOMKilled restarts, node pressure, and "
                "resource limit misconfigurations. Use when K8s workloads are unstable."
            ),
            "system_prompt": (
                "You are a Kubernetes diagnostics expert. Analyze pod statuses, "
                "restart reasons (OOMKilled, CrashLoopBackOff), and node conditions "
                "(MemoryPressure, DiskPressure, PIDPressure). Compare memory limits "
                "against actual usage. "
                "Return under 150 words: root cause, affected resources, "
                "fix (limit adjustment, scaling, or scheduler config)."
            ),
            "tools": [check_kubernetes_pods, check_kubernetes_nodes],
        },
        # ── Report Writer (synthesis, not data collection) ──
        {
            "name": "report-writer",
            "description": (
                "Writes the final diagnostic report synthesizing all findings from "
                "other experts. Use ONLY after all diagnostic data has been collected "
                "and analyzed. Reads LEARNINGS.md for historical context."
            ),
            "system_prompt": (
                "You are a diagnostic report specialist. You do NOT run diagnostics "
                "yourself. Your job is to take diagnostic findings provided to you "
                "and write a professional, actionable report.\n\n"
                "Report structure (use Chinese):\n"
                "## 诊断报告\n"
                "### 故障概述\n(1-2 sentences summarizing the problem)\n"
                "### 诊断过程\n(Briefly list the diagnostic steps taken and key tools used)\n"
                "### 根因分析\n(What caused the issue, with evidence chain)\n"
                "### 影响范围\n(What services/nodes/pods are affected)\n"
                "### 修复建议\n(Specific, actionable steps ordered by priority)\n\n"
                "Rules:\n"
                "- Write in Chinese, professional tone\n"
                "- Only include findings supported by the collected evidence\n"
                "- Never speculate beyond the data\n"
                "- Keep the report concise — under 400 words total\n"
                "- If relevant, read /agent_data/LEARNINGS.md to find similar past cases"
            ),
            "tools": [],  # No diagnostic tools — synthesis only
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
