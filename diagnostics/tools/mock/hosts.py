"""Mock host-level diagnostic tools — CPU, memory, disk, network, processes, and system overview.

One-to-one correspondence: live/hosts.py
"""

from langchain_core.tools import tool

from diagnostics.tools.mock.data import (
    _load_avg,
    cpu_data,
    disk_data,
    memory_data,
    network_data,
    processes_data,
)
from diagnostics.tools.mock.scenarios import AVAILABLE_SCENARIOS, get_active_scenario


@tool
def get_system_overview() -> str:
    """Get basic system overview: OS version, uptime, kernel, and current load.

    Use this first to understand the environment before running detailed checks.
    """
    s = get_active_scenario()
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
    return cpu_data(get_active_scenario())


@tool
def check_memory() -> str:
    """Check memory usage, swap, and /proc/meminfo highlights."""
    return memory_data(get_active_scenario())


@tool
def check_disk() -> str:
    """Check disk IO performance, utilization, latency (iostat), and filesystem usage (df)."""
    return disk_data(get_active_scenario())


@tool
def check_network() -> str:
    """Check network connections (ss), interface stats, errors, drops, and TCP retransmits."""
    return network_data(get_active_scenario())


@tool
def check_processes() -> str:
    """List top processes with state (D/S/R) and resource consumption."""
    return processes_data(get_active_scenario())


@tool
def check_conntrack() -> str:
    """Check netfilter conntrack table: max/current entries, insert_failed, drop, early_drop counts.

    Use this to verify or refute H1: conntrack table full hypothesis.
    Returns: nf_conntrack_max, nf_conntrack_count, conntrack -S summary, dmesg conntrack errors.
    """
    s = get_active_scenario()
    if s == "conntrack_table_full":
        return """## Conntrack Table Status
nf_conntrack_max:   131072
nf_conntrack_count: 131072 (100% FULL!)
使用率: 100% — CRITICAL!

## conntrack -S 统计
entries           131072
found             8520000
new               125000
invalid           8500
insert_failed     52000   ← 新连接插入失败!
drop              48000   ← 内核丢弃数据包!
early_drop        12500   ← 提前淘汰旧连接!
error             25
search_restart    3200

## dmesg conntrack 相关
dmesg: nf_conntrack: table full, dropping packet
dmesg: nf_conntrack: table full, dropping packet (重复出现!)
dmesg: conntrack: connection tracking table full (131072/131072 entries)

## 诊断结论
🔴 conntrack表100%满 → 内核丢弃新UDP/TCP包 → DNS超时 + Pod通信失败"""
    elif s == "etcd_quota_near_full":
        return """## Conntrack Table Status
nf_conntrack_max:   131072
nf_conntrack_count: 12543 (9.6% — 正常!)
使用率: 9.6% — HEALTHY

## conntrack -S 统计
entries           12543
found             5230000
new               850
invalid           120
insert_failed     0       ← 正常!
drop              0       ← 无丢包!
early_drop        0       ← 无淘汰!
error             0
search_restart    0

## dmesg conntrack 相关
dmesg | grep conntrack: (无输出 — 无conntrack相关问题!)

## 诊断结论
✅ conntrack表远未饱和(9.6%)，内核层面无连接跟踪相关丢包 → H1 (conntrack) 可排除"""
    else:
        return """## Conntrack Table Status
nf_conntrack_max:   131072
nf_conntrack_count: 8342 (6.4% — 正常!)
使用率: 6.4% — HEALTHY

## conntrack -S 统计
entries           8342
found             4520000
new               650
invalid           80
insert_failed     0
drop              0
early_drop        0
error             0
search_restart    0

## dmesg conntrack 相关
dmesg | grep conntrack: (无输出)

## 诊断结论
✅ conntrack表正常，无连接跟踪相关问题"""


@tool
def check_dmesg() -> str:
    """Check dmesg kernel ring buffer for OOM, conntrack, IO errors, and other anomalies.

    Use this to look for kernel-level issues: nf_conntrack table full, oom-killer invocations,
    task hung warnings, filesystem errors, etc.
    """
    s = get_active_scenario()
    if s == "conntrack_table_full":
        return """## dmesg 关键信息
nf_conntrack: table full, dropping packet (大量!)
nf_conntrack: table full, dropping packet (重复!)
⚠ conntrack: connection tracking table full (131072/131072 entries)
(无 OOM Killer/IO hang/文件系统错误)
"""
    elif s == "conntrack_and_oom":
        return """## dmesg 关键信息
nf_conntrack: table full, dropping packet
nf_conntrack: table full, dropping packet
oom-killer invoked: killed process java (PID 8765) total-vm:12849152kB anon-rss:6754304kB
⚠ 双故障: conntrack table full + Java OOM Kill
"""
    elif s == "etcd_quota_near_full":
        return """## dmesg 关键信息
(无 nf_conntrack 错误)
(无 OOM Killer 调用)
(无 task hung 警告)
(无文件系统错误)
✅ dmesg 完全正常 — 内核层面无异常
"""
    elif s == "memory_leak_and_disk_full":
        return """## dmesg 关键信息
java invoked oom-killer: gfp_mask=0xcc0(GFP_KERNEL), oom_score_adj=750
Out of memory: Killed process 8765 (java) total-vm:12849152kB anon-rss:7130112kB (RSS=6.8GB)
EXT4-fs error (device sda1): ext4_journal_check_start: Detected aborted journal
ext4 journal abort on /dev/sda1 (磁盘满导致ext4日志写入失败)
⚠ 双故障: Java内存泄漏OOM Killer + 磁盘满导致ext4日志异常
"""
    else:
        return """## dmesg 关键信息
(无异常 — 无 OOM/conntrack/IO hang/文件系统错误)
"""


@tool
def list_diagnostic_capabilities() -> str:
    """List available diagnostic tools and their purposes.

    Use this when unsure which tools to call or what data each provides.
    """
    return """Available diagnostic tools:

System:  get_system_overview() | check_cpu() | check_memory() | check_processes()
Storage: check_disk()
Network: check_network() | check_conntrack()
Kernel:  check_dmesg()
GPU:     check_gpu_health() | check_gpu_memory() | check_gpu_utilization()
K8s:     check_kubernetes_pods() | check_kubernetes_nodes()

Suggested approach:
- Start with get_system_overview() to assess overall health
- Based on findings, drill into specific subsystems
- Cross-reference results: one tool's anomaly should be confirmed by another
- If Kubernetes is involved, check pods and nodes
- If GPU workloads, check health → memory → utilization
- Use check_conntrack() to verify/refute conntrack table full hypothesis
- Use check_dmesg() to check kernel-level errors (OOM, conntrack, IO)"""
