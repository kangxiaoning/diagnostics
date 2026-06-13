"""System overview, CPU, memory, and process diagnostic tools."""

from langchain_core.tools import tool

from diagnostics.tools.data import (
    cpu_data, memory_data, processes_data, _load_avg,
)
from diagnostics.tools.scenarios import AVAILABLE_SCENARIOS, _active_scenario


@tool
def get_system_overview() -> str:
    """Get basic system overview: OS version, uptime, kernel, and current load.

    Use this first to understand the environment before running detailed checks.
    """
    s = _active_scenario
    sc = AVAILABLE_SCENARIOS[s]
    normal = s == "normal"
    return f"""OS: Ubuntu 22.04.4 LTS (Jammy Jellyfish)
Kernel: Linux 5.15.0-91-generic x86_64
Uptime: 45 days 12:34:56
System load (1/5/15 min): {_load_avg(s)}
Scenario: {sc.label}

Quick assessment: {'System appears stable — no obvious anomalies from overview data.' if normal else 'System overview shows indicators that warrant further investigation.'}"""


@tool
def check_cpu() -> str:
    """Check CPU utilization, per-core stats, iowait, vmstat, and process top."""
    return cpu_data(_active_scenario)


@tool
def check_memory() -> str:
    """Check memory usage, swap, and /proc/meminfo highlights."""
    return memory_data(_active_scenario)


@tool
def check_processes() -> str:
    """List top processes with state (D/S/R) and resource consumption."""
    return processes_data(_active_scenario)


@tool
def list_diagnostic_capabilities() -> str:
    """List available diagnostic tools and their purposes.

    Use this when unsure which tools to call or what data each provides.
    """
    return """Available diagnostic tools:

System:  get_system_overview() | check_cpu() | check_memory() | check_processes()
Storage: check_disk()
Network: check_network()
GPU:     check_gpu_health() | check_gpu_memory() | check_gpu_utilization()
K8s:     check_kubernetes_pods() | check_kubernetes_nodes()

Suggested approach:
- Start with get_system_overview() to assess overall health
- Based on findings, drill into specific subsystems
- Cross-reference results: one tool's anomaly should be confirmed by another
- If Kubernetes is involved, check pods and nodes
- If GPU workloads, check health → memory → utilization"""
