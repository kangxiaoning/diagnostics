"""Host-level live diagnostic tools — execute real commands via remote shell.

These tools are placeholder implementations that map to shell scripts
in diagnostics/tools/scripts/ for actual data collection. Replace the
placeholder logic with SSH/remote execution as needed.

One-to-one correspondence: mock/hosts.py
"""

from __future__ import annotations

from langchain_core.tools import tool


@tool
def get_system_overview() -> str:
    """Get system overview: OS, kernel, uptime, load average, and scenario route info.

    Must be called FIRST to establish diagnostic baseline (Brendan Gregg USE method).
    """
    return "[LIVE] System overview — execute collect_linux_quick_check.sh via remote shell on target host."


@tool
def check_cpu() -> str:
    """Check CPU utilization, iowait, load average, and process D-states via vmstat/top.

    Use when load > CPU cores or iowait > 30%.
    """
    return "[LIVE] CPU diagnostics — execute collect_cpu_diagnostics.sh via remote shell on target host."


@tool
def check_memory() -> str:
    """Check memory usage, swap, OOM events, per-process RSS, and /proc/meminfo.

    Use when available memory < 20% or OOMKilled events detected.
    """
    return "[LIVE] Memory diagnostics — execute collect_memory_diagnostics.sh via remote shell on target host."


@tool
def check_disk() -> str:
    """Check disk IO performance: iostat utilization, await, svctm, df usage.

    Use when iowait > 30% or await > 20ms.
    """
    return "[LIVE] Disk IO diagnostics — execute collect_disk_diagnostics.sh via remote shell on target host."


@tool
def check_network() -> str:
    """Check network: interface errors/drops, TCP retransmits, connection states, routing, DNS.

    Use when retrans > 1% or CLOSE-WAIT > 10.
    """
    return "[LIVE] Network diagnostics — execute collect_network_diagnostics.sh via remote shell on target host."


@tool
def check_processes() -> str:
    """Check running processes: top consumers by CPU/memory, D-state count, OOM scores.

    Args:
        pid_or_name: optional PID or process name to focus on a specific process.
    """
    return "[LIVE] Process diagnostics — execute collect_process_diagnostics.sh via remote shell on target host."


@tool
def list_diagnostic_capabilities() -> str:
    """List all available diagnostic tools and their purposes."""
    return (
        "## Host-level diagnostic tools (live mode)\n"
        "- get_system_overview: OS/kernel/uptime/load average\n"
        "- check_cpu: CPU utilization, iowait, D-states\n"
        "- check_memory: Memory usage, swap, OOM risk\n"
        "- check_disk: Disk IO utilization, await, svctm\n"
        "- check_network: Interface errors, TCP retransmits, connections\n"
        "- check_processes: Process state analysis, OOM scores\n"
        "\n## GPU diagnostic tools (live mode)\n"
        "- check_gpu_health: Temperature, power, throttling, ECC, PCIe\n"
        "- check_gpu_memory: GPU memory usage, per-process allocation\n"
        "- check_gpu_utilization: GPU core/SM/memory bandwidth usage\n"
        "\n## Kubernetes diagnostic tools (live mode)\n"
        "- 39 kubectl-based tools for cluster/pod/node/etcd/CoreDNS/Helm diagnosis\n"
        "\n## Remote shell scripts\n"
        "Execution requires remote shell channel to target hosts. Contact admin for setup."
    )
