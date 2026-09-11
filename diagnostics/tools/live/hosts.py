"""Host-level live diagnostic tools — production-aligned surface (get_os_* family).

These tools are placeholder implementations that map to shell scripts
in diagnostics/tools/scripts/ for actual data collection. Replace the
placeholder logic with SSH/remote execution as needed.

One-to-one correspondence: mock/hosts.py
"""

from __future__ import annotations

from langchain_core.tools import tool


def _live(what: str) -> str:
    return f"[LIVE] {what} — execute collection script via remote shell on target host."


# ══════════════ System base ══════════════

@tool
def get_os_base_info() -> str:
    """获取主机基本信息：OS 版本、内核、运行时长、当前负载。"""
    return _live("host base info (hostnamectl / uptime / load average)")


# ══════════════ Kernel ══════════════

@tool
def get_os_kernel_sysctl_one(param: str) -> str:
    """获取单个内核参数值（sysctl <param>）。"""
    return _live(f"sysctl {param}")


@tool
def get_os_kernel_sysctl_all() -> str:
    """获取所有内核参数值（sysctl -a）。"""
    return _live("sysctl -a")


@tool
def get_os_kernel_sysctl_grep(pattern: str = "") -> str:
    """按条件过滤内核参数（sysctl -a | grep）。"""
    return _live(f"sysctl -a | grep '{pattern}'")


@tool
def get_os_kernel_dmesg_err() -> str:
    """获取内核dmesg日志：OOM、conntrack、IO 错误等内核级异常。"""
    return _live("dmesg error-level extraction")


@tool
def get_os_kernel_numa_info() -> str:
    """获取NUMA节点信息：拓扑、内存分布、miss 率。"""
    return _live("numactl -H / numastat")


@tool
def get_os_kernel_journalctl_period(start_time: str = "", end_time: str = "") -> str:
    """获取指定时间段内的系统日志（journalctl --since --until）。"""
    return _live(f"journalctl --since '{start_time}' --until '{end_time}' -p err")


# ══════════════ Memory ══════════════

@tool
def get_os_mem_5s() -> str:
    """获取实时内存采样：使用率、swap、/proc/meminfo 亮点。"""
    return _live("memory sampling (free / /proc/meminfo)")


@tool
def get_os_mem_top_process() -> str:
    """获取内存占用Top20进程。"""
    return _live("ps aux --sort=-rss | head -21")


@tool
def get_os_mem_oom() -> str:
    """检查OOM Killer事件（dmesg/journal 中的 oom-killer 记录）。"""
    return _live("dmesg | grep -i 'oom|killed process'")


# ══════════════ CPU ══════════════

@tool
def get_os_cpu_info() -> str:
    """获取主机CPU信息：利用率、per-core、iowait、vmstat。"""
    return _live("CPU diagnostics (vmstat / mpstat)")


@tool
def get_os_cpu_load() -> str:
    """查询系统负载及CPU使用情况（1/5/15 分钟负载）。"""
    return _live("load average + CPU usage (uptime / sar -q)")


@tool
def get_os_cpu_cs() -> str:
    """获取上下文切换相关信息（vmstat cs 列、非自愿切换）。"""
    return _live("context switch stats (vmstat / pidstat -w)")


@tool
def get_os_cpu_ps_elf() -> str:
    """获取进程列表及CPU/内存占用信息（ps -elf + top 形态）。"""
    return _live("process list (ps -elf / top -b -n1)")


# ══════════════ Block / disk ══════════════

@tool
def get_os_block_info() -> str:
    """获取磁盘块设备及文件系统信息（lsblk + df 形态）。"""
    return _live("block devices & filesystem usage (lsblk / df -h)")


@tool
def get_os_block_5s() -> str:
    """采集iostat数据以分析磁盘I/O：利用率、await、svctm。"""
    return _live("iostat -x sampling")


# ══════════════ Network ══════════════

@tool
def get_os_net_ethtools_s(nic: str = "") -> str:
    """获取指定网卡数据包状态（ethtool -S：错误/丢弃计数器）。"""
    return _live(f"ethtool -S {nic or '<primary nic>'}")


@tool
def get_os_net_softnet_stat() -> str:
    """获取软网络统计信息（/proc/net/softnet_stat）。"""
    return _live("cat /proc/net/softnet_stat")


@tool
def get_os_net_tc_stat() -> str:
    """获取连接流量控制状态（tc -s qdisc）。"""
    return _live("tc -s qdisc show")


@tool
def get_os_net_sar_dev(nic: str = "", start_time: str = "", end_time: str = "") -> str:
    """获取网卡流量信息（sar -n DEV）。"""
    return _live(f"sar -n DEV (nic={nic or 'all'})")


@tool
def get_os_net_softirqs() -> str:
    """获取网络软中断统计。"""
    return _live("softirq/interrupt stats (/proc/net/softnet_stat, /proc/interrupts)")


@tool
def get_os_net_ss_s() -> str:
    """查询Socket摘要信息（ss -s：连接状态分布、TCP 重传、队列）。"""
    return _live("socket summary (ss -s / ss -ti)")


@tool
def get_os_net_ethtool(nic: str = "") -> str:
    """获取网卡驱动及硬件信息（ethtool -i）。"""
    return _live(f"ethtool -i {nic or '<primary nic>'}")


@tool
def get_os_net_ping(target: str = "") -> str:
    """测试网络连通性及延迟（ping：丢包/RTT）。"""
    return _live(f"ping -c 5 {target or '<target>'}")


@tool
def get_os_net_nslookup(target: str = "") -> str:
    """执行DNS域名解析查询。"""
    return _live(f"nslookup {target or '<target>'}")

@tool
def get_os_net_conntrack() -> str:
    """[LIVE 占位] 获取netfilter连接跟踪表状态：容量、insert_failed、drop/early_drop 计数。

    移植说明：实现体请对接实际环境的对应接口/脚本；签名与 mock 严格一致。
    """
    return "[LIVE] get_os_net_conntrack — implement against the actual environment backend (signature mirrors mock)."


@tool
def get_os_system_overview() -> str:
    """[LIVE 占位] Get basic system overview: OS version, uptime, kernel, and current load.

    移植说明：实现体请对接实际环境的对应接口/脚本；签名与 mock 严格一致。
    """
    return "[LIVE] get_os_system_overview — implement against the actual environment backend (signature mirrors mock)."
