"""Argus monitoring data — scenario-specific 1min-granularity per-subsystem timelines.

Each scenario provides a 10-min window per subsystem so the Coordinator can:
1. See the temporal relationship between anomalies across subsystems
2. Identify the onset time and duration of each symptom
3. Cross-reference concurrent metric changes to infer causality

All data functions accept a scenario ID and return formatted text.
Query parameters (name_chunk, start_time, end_time) are applied as
display-level overrides on the formatted output.
"""

from __future__ import annotations

import re
from typing import Callable


# ═══════════════════ Public per-metric query API ═══════════════════

def query_argus_cpu_metrics(
    scenario: str,
    name_chunk: str = "",
    start_time: str = "",
    end_time: str = "",
) -> str:
    handler = _CPU_DATA.get(scenario, _default)
    return _apply_query_overrides(handler(), name_chunk, start_time, end_time)


def query_argus_memory_metrics(
    scenario: str,
    name_chunk: str = "",
    start_time: str = "",
    end_time: str = "",
) -> str:
    handler = _MEM_DATA.get(scenario, _default)
    return _apply_query_overrides(handler(), name_chunk, start_time, end_time)


def query_argus_disk_metrics(
    scenario: str,
    name_chunk: str = "",
    start_time: str = "",
    end_time: str = "",
) -> str:
    handler = _DISK_DATA.get(scenario, _default)
    return _apply_query_overrides(handler(), name_chunk, start_time, end_time)


def query_argus_network_metrics(
    scenario: str,
    name_chunk: str = "",
    start_time: str = "",
    end_time: str = "",
) -> str:
    handler = _NET_DATA.get(scenario, _default)
    return _apply_query_overrides(handler(), name_chunk, start_time, end_time)


def query_argus_nodes_metrics(
    scenario: str,
    name_chunk: str = "",
    start_time: str = "",
    end_time: str = "",
) -> str:
    handler = _NODES_DATA.get(scenario, _nodes_default)
    return _apply_query_overrides(handler(), name_chunk, start_time, end_time)


def query_argus_services_metrics(
    scenario: str,
    name_chunk: str = "",
    start_time: str = "",
    end_time: str = "",
) -> str:
    handler = _SERVICES_DATA.get(scenario, _services_default)
    return _apply_query_overrides(handler(), name_chunk, start_time, end_time)


# ═══════════════════ Default (normal system) ═══════════════════

def _default() -> str:
    return "Argus: 无异常数据返回（系统正常或指标不可用）"


def _cpu_normal() -> str:
    return _fmt("CPU", "prod-web-01", "15:00",
        cols=["CPU%", "Load", "iowait%", "Top Process"],
        rows=[
            [2, 0.5, 1, "java 2.5%"],
            [3, 0.6, 1, "java 2.8%"],
            [2, 0.5, 1, "mysqld 1.5%"],
            [4, 0.7, 2, "java 3.2%"],
            [3, 0.6, 1, "java 2.5%"],
            [2, 0.5, 1, "mysqld 1.8%"],
            [3, 0.6, 2, "java 3.0%"],
            [2, 0.5, 1, "java 2.2%"],
            [2, 0.5, 1, "nginx 1.5%"],
            [3, 0.6, 1, "java 2.8%"],
        ],
        summary="✅ CPU 正常 — 无异常波动")


def _mem_normal() -> str:
    return _fmt("Memory", "prod-web-01", "15:00",
        cols=["Mem%", "Swap%", "Available(GiB)", "OOM Events"],
        rows=[
            [45, 0, 3.5, "—"],
            [45, 0, 3.5, "—"],
            [46, 0, 3.4, "—"],
            [45, 0, 3.5, "—"],
            [46, 0, 3.4, "—"],
            [45, 0, 3.5, "—"],
            [45, 0, 3.5, "—"],
            [46, 0, 3.4, "—"],
            [45, 0, 3.5, "—"],
            [45, 0, 3.5, "—"],
        ],
        summary="✅ 内存正常 — 无异常波动")


def _disk_normal() -> str:
    return _fmt("Disk", "prod-web-01", "15:00",
        cols=["Util%", "IOPS(r/s)", "IOPS(w/s)", "await(ms)"],
        rows=[
            [5, 25, 45, 1.5],
            [6, 28, 48, 1.6],
            [5, 25, 45, 1.5],
            [7, 30, 50, 1.7],
            [5, 25, 45, 1.5],
            [6, 28, 48, 1.6],
            [5, 25, 45, 1.5],
            [5, 25, 45, 1.5],
            [6, 28, 48, 1.6],
            [5, 25, 45, 1.5],
        ],
        summary="✅ 磁盘正常 — 无异常波动")


def _net_normal() -> str:
    return _fmt("Network", "prod-web-01", "15:00",
        cols=["RX(Mbps)", "TX(Mbps)", "丢包", "重传%", "ESTAB"],
        rows=[
            [120, 80, 0, 0.5, 120],
            [130, 85, 0, 0.3, 125],
            [125, 82, 0, 0.4, 122],
            [135, 88, 0, 0.5, 128],
            [128, 85, 0, 0.3, 125],
            [122, 80, 0, 0.4, 118],
            [130, 84, 0, 0.5, 125],
            [125, 82, 0, 0.3, 122],
            [128, 85, 0, 0.4, 125],
            [132, 82, 0, 0.3, 128],
        ],
        summary="✅ 网络正常 — 无异常波动")


def _k8s_normal() -> str:
    return _fmt("Kubernetes", "prod-us-east", "15:00",
        cols=["NotReady", "PodRestarts", "API Lat(ms)", "DNS Lat(ms)"],
        rows=[
            [0, 0, 5, 2],
            [0, 0, 5, 2],
            [0, 0, 6, 2],
            [0, 0, 5, 2],
            [0, 0, 5, 2],
            [0, 0, 6, 3],
            [0, 0, 5, 2],
            [0, 0, 5, 2],
            [0, 0, 5, 2],
            [0, 0, 6, 2],
        ],
        summary="✅ K8s 正常 — 无异常波动")


def _nodes_default() -> str:
    return _fmt("Nodes", "prod-us-east", "15:00",
        cols=["NotReady", "PodRestarts", "Evictions", "Pending"],
        rows=[[0, 0, 0, 0]] * 10,
        summary="✅ 节点与 Pod 正常 — 无异常波动")


def _services_default() -> str:
    return _fmt("Services", "prod-us-east", "15:00",
        cols=["API Lat(ms)", "etcd Ldr", "DB(MB)", "DNS Lat(ms)", "DNS Err"],
        rows=[[5, 1, 1200, 2, 0]] * 10,
        summary="✅ 控制面与 DNS 正常 — 无异常波动")


# ═══════════════════ Scenario: stress_cpu_and_tc_loss ═══════════════════

def _cpu_stress_tc() -> str:
    return _fmt("CPU", "prod-web-01", "15:00",
        cols=["CPU%", "Load", "iowait%", "Top Process"],
        rows=[
            [35, 1.2, 2, "java 35%"],
            [38, 1.5, 2, "java 38%"],
            [36, 1.3, 2, "java 36%"],
            [95, 8.5, 0, "stress-ng 100%"],  # ← 突变点
            [96, 8.8, 0, "stress-ng 100%"],
            [97, 9.0, 0, "stress-ng 100%"],
            [95, 8.7, 0, "stress-ng 100%"],
            [96, 8.9, 0, "stress-ng 100%"],
            [95, 8.6, 0, "stress-ng 100%"],
            [97, 8.8, 0, "stress-ng 100%"],
        ],
        summary="🔴 15:03 CPU 36→95% 突变，stress-ng 占满所有核心")


def _mem_stress_tc() -> str:
    return _fmt("Memory", "prod-web-01", "15:00",
        cols=["Mem%", "Swap%", "Available(GiB)", "OOM Events"],
        rows=[
            [55, 0, 2.8, "—"],
            [55, 0, 2.8, "—"],
            [56, 0, 2.7, "—"],
            [58, 0, 2.6, "—"],
            [60, 0, 2.4, "—"],
            [61, 0, 2.3, "—"],
            [62, 0, 2.3, "—"],
            [63, 0, 2.2, "—"],
            [64, 0, 2.1, "—"],
            [65, 0, 2.1, "—"],
        ],
        summary="⚠ 内存缓慢增长 55→65%（CPU 满载导致缓存压力），无 OOM")


def _disk_stress_tc() -> str:
    return _fmt("Disk", "prod-web-01", "15:00",
        cols=["Util%", "IOPS(r/s)", "IOPS(w/s)", "await(ms)"],
        rows=[[8, 30, 50, 1.8]] * 3 + [[15, 45, 65, 2.5]] + [[12, 38, 55, 2.0]] + [[10, 32, 48, 1.8]] * 5,
        summary="⚠ 磁盘 IO 轻微上升后回落（CPU 满载的副作用）")


def _net_stress_tc() -> str:
    return _fmt("Network", "prod-web-01", "15:00",
        cols=["RX(Mbps)", "TX(Mbps)", "丢包", "重传%", "ESTAB"],
        rows=[
            [200, 150, 0, 1.2, 520],
            [210, 155, 0, 1.5, 525],
            [195, 148, 0, 1.3, 518],
            [220, 160, 2850, 18.5, 520],  # ← 丢包+重传同时突变
            [215, 155, 3100, 19.2, 518],
            [205, 152, 2900, 18.8, 515],
            [210, 158, 3050, 19.0, 522],
            [208, 155, 2950, 18.5, 520],
            [212, 160, 3000, 19.1, 518],
            [205, 155, 2850, 18.5, 522],
        ],
        summary="🔴 15:03 丢包0→2850 + 重传1.3→18.5% 同时突变（tc 5% loss 策略）")


# ═══════════════════ Scenario: conntrack_and_oom ═══════════════════

def _cpu_conntrack_oom() -> str:
    return _fmt("CPU", "prod-us-east/worker-3", "15:00",
        cols=["CPU%", "Load", "iowait%", "Top Process"],
        rows=[
            [42, 1.8, 2, "java 42% (RSS 4.8G)"],
            [44, 2.0, 2, "java 44% (RSS 5.1G)"],
            [46, 2.5, 3, "java 46% (RSS 5.4G)"],
            [48, 3.0, 5, "java 48% (RSS 5.7G)"],
            [52, 3.5, 8, "java 52% (RSS 6.0G)"],
            [56, 3.8, 10, "java 56% (RSS 6.6G) OOMKilled!"],
            [42, 2.0, 3, "java 30% (Pod重启后)"],
            [44, 2.5, 3, "java 35%"],
            [46, 2.8, 3, "java 38%"],
            [48, 3.0, 4, "java 42%"],
        ],
        summary="🔴 15:05 Java OOMKilled (RSS 6.6G)，Java RSS 从 15:00 起持续增长")


def _mem_conntrack_oom() -> str:
    return _fmt("Memory", "prod-us-east/worker-3", "15:00",
        cols=["Mem%", "Swap%", "Available(MiB)", "OOM Events"],
        rows=[
            [70, 0, 1800, "—"],
            [73, 5, 1600, "—"],
            [76, 15, 1400, "—"],
            [79, 28, 1150, "—"],
            [83, 42, 900, "—"],
            [88, 65, 620, "⚠ 接近OOM"],
            [93, 85, 380, "🔴 Java OOMKilled!"],
            [72, 45, 1720, "Pod重启后回落"],
            [75, 50, 1550, "—"],
            [78, 55, 1350, "—"],
        ],
        summary="🔴 15:05 Java OOMKilled (Mem 93%, Swap 85%)，内存从 15:00 起持续增长")


def _disk_conntrack_oom() -> str:
    return _fmt("Disk", "prod-us-east/worker-3", "15:00",
        cols=["Util%", "IOPS(r/s)", "IOPS(w/s)", "await(ms)"],
        rows=[[15, 35, 65, 3.5]] * 3 + [[25, 55, 95, 5.2]] + [[30, 65, 105, 6.8]] + [[35, 75, 115, 8.2]] + [[12, 25, 45, 2.5]] * 4,
        summary="⚠ 磁盘 IO 中等升高（OOM 前 swap 操作导致），OOM 后回落")


def _net_conntrack_oom() -> str:
    return _fmt("Network", "prod-us-east/worker-3", "15:00",
        cols=["RX(Mbps)", "TX(Mbps)", "丢包", "重传%", "ESTAB"],
        rows=[
            [350, 280, 0, 2.0, 8500],
            [380, 300, 0, 2.5, 9200],
            [400, 320, 1200, 5.0, 10500],
            [420, 350, 8500, 10.0, 12000],
            [450, 380, 32000, 15.2, 131072],  # ← conntrack 满
            [480, 400, 45000, 18.5, 131072],
            [200, 150, 8000, 8.0, 8500],  # Pod重启后
            [350, 280, 500, 3.5, 9200],
            [380, 300, 2000, 5.0, 10500],
            [400, 320, 500, 5.0, 12000],
        ],
        summary="🔴 15:04 ESTAB=131072/131072, 丢包/重传飙升; 15:05 Java OOMKilled — 两个异常同时发生")


# ═══════════════════ Scenario: disk_io_and_dns ═══════════════════

def _cpu_disk_dns() -> str:
    return _fmt("CPU", "prod-us-east/worker-2", "15:00",
        cols=["CPU%", "Load", "iowait%", "Top Process"],
        rows=[
            [15, 1.5, 2, "java 12%"],
            [18, 1.8, 3, "java 15%"],
            [22, 2.5, 5, "java 18%"],
            [25, 4.9, 45, "kubelet D!"],  # ← iowait 暴增
            [25, 4.8, 45, "kubelet D!"],
            [25, 4.9, 45, "logrotate R"],
            [25, 4.8, 45, "kubelet D!"],
            [25, 4.9, 45, "java S (await IO)"],
            [25, 4.5, 42, "kubelet D!"],
            [25, 4.3, 40, "java S (await IO)"],
        ],
        summary="🔴 15:03 iowait 2→45% — logrotate 触发磁盘 IO 飙升，kubelet D 状态，java 等待 IO")


def _mem_disk_dns() -> str:
    return _fmt("Memory", "prod-us-east/worker-2", "15:00",
        cols=["Mem%", "Swap%", "Available(MiB)", "OOM Events"],
        rows=[[45, 0, 3520, "—"]] * 10,
        summary="✅ 内存正常")


def _disk_disk_dns() -> str:
    return _fmt("Disk", "prod-us-east/worker-2", "15:00",
        cols=["Util%", "IOPS(r/s)", "IOPS(w/s)", "await(ms)"],
        rows=[
            [12, 30, 55, 3.5],
            [15, 35, 60, 4.0],
            [25, 50, 80, 15.0],
            [98, 120, 480, 175.5],  # ← util 暴增
            [98, 115, 475, 172.0],
            [98, 118, 478, 178.0],
            [98, 120, 480, 175.5],
            [98, 116, 476, 174.0],
            [96, 110, 460, 165.0],
            [95, 105, 450, 158.0],
        ],
        summary="🔴 15:03 sda util 12→98.5% await 3.5→175ms — logrotate 触发写入风暴")


def _net_disk_dns() -> str:
    return _fmt("Network", "prod-us-east/worker-2", "15:00",
        cols=["RX(Mbps)", "TX(Mbps)", "丢包", "重传%", "ESTAB"],
        rows=[
            [80, 50, 0, 1.5, 480],
            [85, 52, 0, 1.5, 482],
            [90, 55, 0, 1.8, 485],
            [95, 58, 0, 2.0, 480],
            [92, 56, 0, 2.0, 478],
            [90, 55, 0, 2.0, 485],
            [88, 54, 0, 2.0, 482],
            [90, 55, 0, 2.0, 480],
            [92, 56, 0, 2.0, 478],
            [90, 55, 0, 1.8, 482],
        ],
        summary="✅ 网络接口层正常 — 丢包=0，重传正常（DNS 超时是 CoreDNS forward 配置问题）")


# ═══════════════════ Scenario: conntrack_table_full ═══════════════════

def _cpu_conntrack_only() -> str:
    return _fmt("CPU", "prod-us-east/worker-5", "15:00",
        cols=["CPU%", "Load", "iowait%", "Top Process"],
        rows=[
            [18, 1.5, 2, "kubelet 8%"],
            [22, 1.8, 2, "kubelet 8%"],
            [28, 2.0, 3, "envoy 12%"],
            [35, 2.5, 3, "envoy 15%"],
            [40, 2.8, 4, "envoy 18% (sys high)"],
            [45, 3.2, 5, "ksoftirqd 35% (sys!)"],
            [45, 3.5, 5, "ksoftirqd 38%"],
            [45, 3.8, 5, "ksoftirqd 40%"],
            [45, 4.0, 5, "ksoftirqd 38%"],
            [45, 4.2, 5, "ksoftirqd 40%"],
        ],
        summary="🔴 15:02 sys% 开始升高 — ksoftirqd 因 conntrack 操作占高 CPU")


def _net_conntrack_only() -> str:
    return _fmt("Network", "prod-us-east/worker-5", "15:00",
        cols=["RX(Mbps)", "TX(Mbps)", "丢包", "重传%", "ESTAB"],
        rows=[
            [250, 200, 0, 2.0, 45000],
            [280, 220, 0, 2.5, 52000],
            [320, 260, 500, 4.0, 65000],
            [380, 300, 2500, 8.0, 85000],
            [420, 350, 15000, 12.5, 105000],
            [450, 380, 38000, 18.0, 131072],  # ← conntrack 满
            [450, 380, 42000, 20.0, 131072],
            [450, 380, 45000, 22.0, 131072],
            [450, 380, 48000, 25.0, 131072],
            [450, 380, 50000, 28.0, 131072],
        ],
        summary="🔴 15:04 conntrack 131072/131072 满 → 丢包持续增长 → kubelet 心跳超时")


# ═══════════════════ Scenario: memory_leak_and_disk_full ═══════════════════

def _cpu_mem_leak_disk() -> str:
    return _fmt("CPU", "prod-us-east/worker-4", "15:00",
        cols=["CPU%", "Load", "iowait%", "Top Process"],
        rows=[
            [35, 1.5, 3, "java 35% (Full GC 2/min)"],
            [38, 1.8, 4, "java 38% (Full GC 3/min)"],
            [42, 2.2, 5, "java 42% (Full GC 5/min)"],
            [55, 3.5, 8, "java 55% (Full GC 8/min)"],
            [60, 4.0, 12, "java 60% (GC overhead!)"],
            [45, 3.0, 15, "java 30% (OOMKilled!)"],  # ← OOM
            [30, 1.2, 8, "java 15% (Pod重启)"],
            [38, 2.0, 10, "java 38% (Full GC 4/min)"],
            [48, 2.8, 12, "java 48% (Full GC 6/min)"],
            [52, 3.2, 15, "java 52% (Full GC 7/min)"],
        ],
        summary="🔴 15:05 Java OOMKilled (Full GC 频繁，GC overhead > 98%) — 重启后泄漏继续")


def _mem_mem_leak_disk() -> str:
    return _fmt("Memory", "prod-us-east/worker-4", "15:00",
        cols=["Mem%", "Swap%", "Available(MiB)", "OOM Events"],
        rows=[
            [62, 0, 2880, "—"],
            [66, 0, 2560, "—"],
            [72, 5, 2100, "—"],
            [78, 15, 1680, "—"],
            [85, 35, 1080, "⚠ Java RSS 5.8G"],
            [92, 65, 480, "🔴 Java OOMKilled!"],  # ← OOM
            [68, 40, 2240, "Pod重启后回落"],
            [74, 20, 1850, "—"],
            [80, 30, 1420, "—"],
            [86, 45, 920, "⚠ 泄漏继续"],
        ],
        summary="🔴 15:05 OOM — Java 内存泄漏 RSS 持续上升 2.5→5.8G，OOMKilled 后重启泄漏继续")


def _disk_mem_leak_disk() -> str:
    return _fmt("Disk", "prod-us-east/worker-4", "15:00",
        cols=["Util%", "IOPS(r/s)", "IOPS(w/s)", "await(ms)"],
        rows=[
            [78, 40, 120, 8.5],
            [80, 42, 130, 9.2],
            [82, 45, 145, 10.5],
            [85, 50, 160, 12.0],
            [88, 55, 180, 15.5],
            [90, 60, 200, 18.2],
            [92, 65, 220, 22.0],  # ← 磁盘空间压力 + 日志写入
            [93, 68, 240, 25.5],
            [95, 72, 260, 30.0],  # ← 接近磁盘满
            [96, 75, 280, 35.2],
        ],
        summary="🔴 磁盘使用率 78→96%，日志持续写入导致空间耗尽，await 升高")


def _net_mem_leak_disk() -> str:
    return _fmt("Network", "prod-us-east/worker-4", "15:00",
        cols=["RX(Mbps)", "TX(Mbps)", "丢包", "重传%", "ESTAB"],
        rows=[
            [150, 100, 0, 1.0, 320],
            [155, 105, 0, 1.2, 325],
            [148, 98, 0, 1.0, 318],
            [152, 102, 0, 1.1, 322],
            [150, 100, 0, 1.0, 320],
            [145, 95, 0, 1.0, 315],
            [140, 90, 0, 1.2, 310],
            [148, 98, 0, 1.1, 318],
            [150, 100, 0, 1.0, 320],
            [152, 102, 0, 1.1, 322],
        ],
        summary="✅ 网络层正常 — 无丢包/重传异常")


def _k8s_mem_leak_disk() -> str:
    return _fmt("Kubernetes", "prod-us-east", "15:00",
        cols=["NotReady", "PodRestarts", "API Lat(ms)", "DNS Lat(ms)"],
        rows=[
            [0, 0, 5, 2],
            [0, 0, 5, 2],
            [0, 0, 6, 3],
            [0, 0, 8, 3],
            [0, 0, 12, 5],
            [0, 3, 15, 8],  # ← OOMKilled Pod 重启
            [0, 5, 10, 5],
            [0, 2, 8, 3],
            [1, 4, 12, 6],  # ← DiskPressure → NotReady
            [1, 6, 18, 10],
        ],
        summary="🔴 15:05 Pod OOMKilled 重启 3次 → 15:08 DiskPressure 导致 worker-4 NotReady")




# ═══════════════════ Scenario: memory_leak (host) ═══════════════════
_mem_memory_leak = lambda: _fmt("Memory", "prod-web-01", "15:00",
    cols=["Mem%", "Swap%", "Available(MiB)", "OOM Events"],
    rows=[
        [55, 0, 2880, "—"], [58, 0, 2560, "—"], [62, 0, 2240, "—"],
        [68, 5, 1760, "—"], [74, 15, 1280, "—"], [80, 30, 820, "⚠ Java RSS 5.2G"],
        [86, 50, 480, "⚠ Java RSS 5.8G"], [90, 70, 280, "🔴 接近OOM"],
        [92, 80, 180, "🔴 Java RSS 6.1G"], [88, 60, 380, "Swap回收后"],
    ],
    summary="🔴 15:03 Mem 55→92% + Swap 0→80% — Java进程RSS持续增长(内存泄漏)")


# ═══════════════════ Scenario: disk_bottleneck (host) ═══════════════════
_disk_disk_bottleneck = lambda: _fmt("Disk", "prod-web-01", "15:00",
    cols=["Util%", "IOPS(r/s)", "IOPS(w/s)", "await(ms)"],
    rows=[
        [25, 50, 80, 3.5], [30, 55, 90, 4.2], [45, 80, 130, 8.5],
        [78, 150, 280, 35.0], [92, 200, 380, 85.0], [98, 220, 420, 150.0],
        [98, 220, 420, 155.0], [98, 215, 415, 148.0], [95, 200, 400, 135.0],
        [90, 180, 360, 110.0],
    ],
    summary="🔴 15:03 sda util 25→98% await 3.5→150ms — 磁盘IO接近饱和")


# ═══════════════════ Scenario: network_congestion (host) ═══════════════════
_net_network_congestion = lambda: _fmt("Network", "prod-web-01", "15:00",
    cols=["RX(Mbps)", "TX(Mbps)", "丢包", "重传%", "ESTAB"],
    rows=[
        [350, 280, 0, 1.5, 5200], [380, 300, 0, 1.8, 5800],
        [420, 340, 0, 2.5, 6500], [480, 400, 500, 5.0, 7800],
        [520, 450, 2500, 8.5, 9200], [550, 480, 5000, 12.0, 10500],
        [540, 470, 4800, 11.5, 10200], [530, 460, 4500, 11.0, 9800],
        [510, 440, 3800, 9.5, 9200], [480, 400, 2500, 7.0, 8500],
    ],
    summary="🔴 15:03 带宽接近饱和 RX 350→550Mbps, 丢包0→5000, 重传1.5→12%")


# ═══════════════════ Scenario: softirq_starvation (host) ═══════════════════
_cpu_softirq = lambda: _fmt("CPU", "prod-web-01", "15:00",
    cols=["CPU%", "Load", "iowait%", "Top Process"],
    rows=[
        [25, 1.5, 2, "nginx 15%"], [28, 1.8, 2, "nginx 18%"],
        [35, 2.5, 3, "nginx 22%"], [50, 4.0, 3, "ksoftirqd 35%"],
        [60, 5.5, 4, "ksoftirqd 45%"], [65, 6.0, 4, "ksoftirqd 50%"],
        [65, 6.2, 4, "ksoftirqd 52%"], [63, 5.8, 4, "ksoftirqd 48%"],
        [60, 5.5, 3, "ksoftirqd 45%"], [58, 5.2, 3, "ksoftirqd 42%"],
    ],
    summary="🔴 15:03 ksoftirqd CPU 5→52% — 软中断处理瓶颈(单CPU绑定)")

_net_softirq = lambda: _fmt("Network", "prod-web-01", "15:00",
    cols=["RX(Mbps)", "TX(Mbps)", "丢包", "重传%", "ESTAB"],
    rows=[
        [300, 250, 0, 1.0, 4500], [320, 260, 0, 1.2, 4800],
        [350, 280, 200, 2.5, 5200], [380, 300, 1500, 5.0, 5800],
        [400, 320, 5000, 8.5, 6200], [420, 340, 8000, 12.0, 6500],
        [410, 330, 7500, 11.5, 6300], [400, 320, 6800, 10.5, 6100],
        [380, 300, 5500, 8.5, 5800], [360, 280, 4000, 6.5, 5500],
    ],
    summary="🔴 15:03 ring buffer溢出 → 丢包0→8000 — ethtool -S rx_missed_errors增长")


# ═══════════════════ Scenario: tcp_accept_overflow (host) ═══════════════════
_net_tcp_overflow = lambda: _fmt("Network", "prod-web-01", "15:00",
    cols=["RX(Mbps)", "TX(Mbps)", "丢包", "重传%", "ESTAB"],
    rows=[
        [200, 150, 0, 0.8, 12000], [210, 155, 0, 0.9, 15000],
        [220, 160, 0, 1.0, 20000], [230, 170, 0, 1.2, 28000],
        [240, 180, 0, 1.5, 35000], [250, 190, 0, 1.8, 42000],
        [255, 195, 0, 2.0, 48000], [250, 190, 0, 2.5, 52000],
        [245, 185, 0, 2.0, 45000], [240, 180, 0, 1.5, 38000],
    ],
    summary="⚠ ESTAB 12K→52K 暴增 — ss -lnt 显示 Recv-Q=Send-Q(somaxconn溢出), 接口层无丢包")


# ═══════════════════ Scenario: high_cpu_iowait (host) ═══════════════════
_cpu_high_iowait = lambda: _fmt("CPU", "prod-web-01", "15:00",
    cols=["CPU%", "Load", "iowait%", "Top Process"],
    rows=[
        [20, 0.8, 3, "java 15%"], [22, 1.0, 5, "java 18%"],
        [25, 2.5, 15, "java 20%"], [30, 5.5, 35, "java 15% (D state)"],
        [30, 8.0, 45, "mysqld D"], [32, 10.5, 50, "java D"],
        [30, 11.0, 52, "mysqld D"], [28, 9.5, 48, "java D"],
        [25, 7.0, 40, "java 15%"], [22, 4.5, 25, "java 18%"],
    ],
    summary="🔴 Load 0.8→11 但 CPU% 仅 20→32% — iowait 3→52% (大量D状态进程)")

_disk_high_iowait = lambda: _fmt("Disk", "prod-web-01", "15:00",
    cols=["Util%", "IOPS(r/s)", "IOPS(w/s)", "await(ms)"],
    rows=[
        [15, 30, 50, 2.5], [20, 40, 65, 4.0], [45, 80, 130, 15.0],
        [85, 180, 320, 65.0], [95, 220, 400, 120.0], [98, 230, 420, 180.0],
        [98, 225, 415, 175.0], [95, 210, 390, 155.0], [88, 180, 340, 100.0],
        [70, 140, 260, 55.0],
    ],
    summary="🔴 sda util 15→98% await 2.5→180ms — 磁盘IO瓶颈导致高iowait")


# ═══════════════════ Scenario: mixed_io_memory (host) ═══════════════════
_mem_mixed = lambda: _fmt("Memory", "prod-web-01", "15:00",
    cols=["Mem%", "Swap%", "Available(MiB)", "OOM Events"],
    rows=[
        [60, 0, 2560, "—"], [65, 5, 2240, "—"], [70, 12, 1920, "—"],
        [78, 25, 1440, "—"], [84, 40, 960, "⚠ 内存压力"],
        [88, 55, 640, "⚠ Swap活跃"], [90, 65, 480, "⚠ 频繁swap"],
        [92, 70, 380, "⚠ 性能劣化"], [90, 68, 420, "—"], [88, 60, 520, "—"],
    ],
    summary="🔴 Mem 60→92% + Swap 0→70% — 内存和磁盘IO同时瓶颈")

_disk_mixed = lambda: _fmt("Disk", "prod-web-01", "15:00",
    cols=["Util%", "IOPS(r/s)", "IOPS(w/s)", "await(ms)"],
    rows=[
        [30, 50, 80, 4.0], [35, 60, 100, 6.5], [50, 100, 160, 15.0],
        [75, 180, 300, 45.0], [88, 220, 380, 85.0], [92, 240, 400, 110.0],
        [95, 250, 420, 130.0], [92, 240, 400, 120.0], [88, 220, 380, 95.0],
        [85, 200, 350, 80.0],
    ],
    summary="🔴 util 30→95% await 4→130ms — swap操作加剧磁盘IO压力")


# ═══════════════════ Scenario: disk_full_var_log (host) ═══════════════════
_disk_full_varlog = lambda: _fmt("Disk", "prod-web-01", "15:00",
    cols=["Util%", "IOPS(r/s)", "IOPS(w/s)", "await(ms)"],
    rows=[
        [75, 40, 100, 5.0], [78, 42, 110, 5.5], [82, 48, 125, 6.5],
        [88, 55, 150, 8.5], [92, 60, 170, 10.5], [95, 65, 190, 13.0],
        [97, 70, 210, 16.0], [98, 72, 220, 18.5], [99, 75, 230, 22.0],
        [100, 78, 240, 28.0],
    ],
    summary="🔴 /var/log 分区 75→100% — 磁盘空间耗尽,容器运行时无法创建新容器")


# ═══════════════════ Scenario: fs_inode_exhaustion (host) ═══════════════════
_disk_inode = lambda: _fmt("Disk", "prod-web-01", "15:00",
    cols=["Util%", "IOPS(r/s)", "IOPS(w/s)", "await(ms)"],
    rows=[
        [50, 30, 60, 2.5], [50, 32, 65, 2.8], [50, 35, 70, 3.0],
        [50, 38, 75, 3.5], [50, 40, 80, 4.0], [50, 42, 85, 4.5],
        [50, 45, 90, 5.0], [50, 48, 95, 5.5], [50, 50, 100, 6.0],
        [50, 52, 105, 6.5],
    ],
    summary="⚠ 磁盘空间50%正常但inode已耗尽 — df -i 显示 IUse%=100%, touch/create失败")


# ═══════════════════ Scenario: systemd_limit_nofile (host) ═══════════════════
_net_systemd = lambda: _fmt("Network", "prod-web-01", "15:00",
    cols=["RX(Mbps)", "TX(Mbps)", "丢包", "重传%", "ESTAB"],
    rows=[
        [150, 100, 0, 0.5, 12000], [160, 110, 0, 0.6, 18000],
        [170, 120, 0, 0.7, 25000], [180, 130, 0, 0.8, 35000],
        [190, 140, 0, 1.0, 45000], [200, 150, 0, 1.2, 55000],
        [205, 155, 0, 1.5, 62000], [200, 150, 0, 2.0, 65000],
        [195, 145, 200, 3.5, 65536], [190, 140, 500, 5.0, 65536],
    ],
    summary="🔴 ESTAB 12K→65536(LimitNOFILE截断) — 新连接失败 'too many open files'")


# ═══════════════════ Scenario: ebpf_probe_overhead (host) ═══════════════════
_cpu_ebpf = lambda: _fmt("CPU", "prod-web-01", "15:00",
    cols=["CPU%", "Load", "iowait%", "Top Process"],
    rows=[
        [25, 1.2, 2, "java 20%"], [28, 1.5, 2, "java 22%"],
        [30, 1.8, 2, "java 25%"], [55, 3.5, 3, "java 15% sys=40%"],
        [70, 5.0, 3, "java 12% sys=58%"], [75, 5.5, 3, "java 10% sys=65%"],
        [78, 5.8, 4, "java 10% sys=68%"], [76, 5.5, 3, "java 10% sys=66%"],
        [72, 5.0, 3, "java 12% sys=60%"], [68, 4.5, 3, "java 15% sys=53%"],
    ],
    summary="🔴 15:03 sys% 5→68% 飙升 — eBPF探针在内核路径触发过量事件")


# ═══════════════════ Scenario: swap_thrashing (host) ═══════════════════
_cpu_swap = lambda: _fmt("CPU", "prod-web-01", "15:00",
    cols=["CPU%", "Load", "iowait%", "Top Process"],
    rows=[
        [30, 2.0, 5, "java 25%"], [35, 3.0, 10, "java 28%"],
        [40, 4.5, 18, "java 22%"], [50, 6.5, 30, "kswapd 25%"],
        [55, 8.0, 40, "kswapd 35%"], [58, 9.0, 45, "kswapd 40%"],
        [60, 9.5, 48, "java 15% kswapd 42%"], [58, 9.0, 45, "kswapd 38%"],
        [55, 8.0, 40, "java 20% kswapd 35%"], [50, 7.0, 35, "java 22%"],
    ],
    summary="🔴 Load 2→9.5 iowait 5→48% — 频繁swap导致所有进程响应严重延迟")

_mem_swap = lambda: _fmt("Memory", "prod-web-01", "15:00",
    cols=["Mem%", "Swap%", "Available(MiB)", "OOM Events"],
    rows=[
        [70, 10, 1920, "—"], [75, 20, 1600, "—"], [80, 35, 1280, "—"],
        [85, 55, 800, "⚠ 频繁swap"], [90, 75, 480, "⚠ swap抖动"],
        [93, 88, 280, "🔴 严重swap抖动"], [95, 92, 180, "🔴 所有进程D状态"],
        [94, 90, 220, "🔴 iowait=48%"], [92, 85, 350, "⚠ 持续抖动"],
        [90, 80, 480, "⚠ swap 80%"],
    ],
    summary="🔴 Mem 70→95% + Swap 10→92% — 频繁swap换入换出导致系统全面降速")

_disk_swap = lambda: _fmt("Disk", "prod-web-01", "15:00",
    cols=["Util%", "IOPS(r/s)", "IOPS(w/s)", "await(ms)"],
    rows=[
        [20, 40, 60, 3.0], [25, 50, 80, 4.5], [35, 80, 130, 8.0],
        [55, 150, 250, 25.0], [70, 200, 350, 50.0], [80, 250, 420, 80.0],
        [85, 280, 450, 95.0], [82, 260, 430, 88.0], [78, 240, 400, 75.0],
        [72, 220, 370, 65.0],
    ],
    summary="🔴 util 20→85% — swap分区频繁读写导致磁盘IO压力")


# ═══════════════════ Scenario: kubelet_disk_io_starvation (cross) ═══════════════════
_disk_kubelet_io = lambda: _fmt("Disk", "prod-us-east/worker-2", "15:00",
    cols=["Util%", "IOPS(r/s)", "IOPS(w/s)", "await(ms)"],
    rows=[
        [15, 30, 55, 3.0], [18, 35, 60, 4.0], [25, 50, 85, 8.0],
        [65, 120, 250, 45.0], [92, 200, 400, 120.0], [98, 220, 450, 250.0],
        [98, 220, 445, 280.0], [98, 218, 440, 300.0], [95, 200, 400, 200.0],
        [88, 180, 350, 150.0],
    ],
    summary="🔴 15:03 sda util 15→98% await 3→300ms — 磁盘IO饱和导致kubelet lease续约超时")

_cpu_kubelet_io = lambda: _fmt("CPU", "prod-us-east/worker-2", "15:00",
    cols=["CPU%", "Load", "iowait%", "Top Process"],
    rows=[
        [20, 1.2, 3, "java 15%"], [22, 1.5, 5, "java 18%"],
        [25, 2.5, 12, "kubelet 8%"], [28, 5.0, 35, "kubelet D!"],
        [30, 7.5, 48, "kubelet D!"], [30, 8.0, 50, "kubelet D!"],
        [28, 7.0, 45, "kubelet D!"], [25, 6.0, 40, "java D"],
        [22, 4.0, 30, "java 18%"], [20, 2.5, 15, "java 15%"],
    ],
    summary="🔴 15:03 iowait 3→50% — kubelet进入D状态,lease续约超时→Node NotReady")


# ═══════════════════ Scenario: node_oom_kubelet_killed (cross) ═══════════════════
_mem_node_oom = lambda: _fmt("Memory", "prod-us-east/worker-2", "15:00",
    cols=["Mem%", "Swap%", "Available(MiB)", "OOM Events"],
    rows=[
        [70, 0, 1920, "—"], [74, 5, 1680, "—"], [78, 12, 1440, "—"],
        [82, 25, 1120, "—"], [86, 40, 800, "⚠ 内存压力"],
        [90, 60, 480, "⚠ Java RSS 5.5G"], [94, 80, 240, "🔴 接近OOM"],
        [96, 90, 120, "🔴 OOM Kill kubelet PID 3210!"],
        [72, 50, 1680, "kubelet被杀后内存回落"], [75, 55, 1520, "—"],
    ],
    summary="🔴 15:07 OOM Killer杀kubelet(PID 3210) — Java RSS 5.5G耗尽节点内存")

_mem_oom_score = lambda: _fmt("Memory", "prod-us-east/worker-2", "15:00",
    cols=["Mem%", "Swap%", "Available(MiB)", "OOM Events"],
    rows=[
        [68, 0, 2000, "—"], [72, 5, 1750, "—"], [76, 10, 1500, "—"],
        [80, 22, 1200, "—"], [84, 38, 850, "⚠ 内存压力"],
        [88, 55, 520, "⚠ Java RSS 5.2G"], [92, 75, 280, "🔴 接近OOM"],
        [95, 88, 140, "🔴 OOM Kill kubelet PID 3210! (oom_score_adj=-500被误杀!)"],
        [70, 48, 1720, "kubelet被误杀后内存回落"], [73, 52, 1580, "—"],
    ],
    summary="🔴 15:07 OOM Killer误杀kubelet(PID 3210, oom_score_adj=-500) — 应用Pod oom_score_adj=750反而存活")

_cpu_node_oom = lambda: _fmt("CPU", "prod-us-east/worker-2", "15:00",
    cols=["CPU%", "Load", "iowait%", "Top Process"],
    rows=[
        [40, 2.0, 3, "java 38%"], [45, 2.5, 5, "java 42%"],
        [50, 3.0, 8, "java 48%"], [55, 3.5, 10, "java 52%"],
        [60, 4.0, 12, "java 55% (RSS 5.5G)"], [58, 3.8, 15, "kswapd 20%"],
        [15, 1.0, 5, "java 12% (kubelet killed!)"],
        [18, 1.2, 3, "java 15%"], [20, 1.5, 3, "java 18%"],
        [22, 1.8, 3, "java 20%"],
    ],
    summary="🔴 15:06 CPU突降 — kubelet被OOM Killer终止后节点指标不可达")

_cpu_oom_score = lambda: _fmt("CPU", "prod-us-east/worker-2", "15:00",
    cols=["CPU%", "Load", "iowait%", "Top Process"],
    rows=[
        [38, 1.8, 3, "java 35%"], [42, 2.2, 5, "java 40%"],
        [48, 2.8, 8, "java 45%"], [52, 3.2, 10, "java 50%"],
        [58, 3.8, 12, "java 52% (RSS 5.2G)"], [55, 3.5, 15, "kswapd 18%"],
        [12, 0.8, 5, "java 10% (kubelet OOM误杀!)"],
        [15, 1.0, 3, "java 12%"], [18, 1.2, 3, "java 15%"],
        [20, 1.5, 3, "java 18%"],
    ],
    summary="🔴 15:06 CPU突降 — kubelet被OOM Killer误杀(oom_score_adj=-500配置不当)，应用Pod反而存活")


# ═══════════════════ Scenario: cpu_throttle_probe_failure (cross) ═══════════════════
_cpu_throttle = lambda: _fmt("CPU", "prod-us-east/worker-1", "15:00",
    cols=["CPU%", "Load", "iowait%", "Top Process"],
    rows=[
        [30, 1.5, 2, "java 25%"], [35, 2.0, 2, "java 30%"],
        [55, 4.0, 3, "batch-job 45%"], [85, 7.5, 3, "batch-job 80%"],
        [95, 9.0, 3, "batch-job 92%"], [98, 10.0, 3, "batch-job 95%"],
        [97, 9.8, 3, "batch-job 94%"], [96, 9.5, 3, "batch-job 93%"],
        [95, 9.0, 3, "batch-job 90%"], [60, 5.0, 2, "java 30% (batch完成)"],
    ],
    summary="🔴 15:03 CPU 30→98% — batch-job抢占CPU导致kubelet探针超时,健康Pod被Kill")


# ═══════════════════ Scenario: container_crash (k8s) ═══════════════════
_k8s_container_crash = lambda: _fmt("Kubernetes", "prod-us-east", "15:00",
    cols=["NotReady", "PodRestarts", "API Lat(ms)", "DNS Lat(ms)"],
    rows=[
        [0, 0, 5, 2], [0, 0, 5, 2], [0, 0, 5, 2],
        [0, 3, 8, 3], [0, 5, 10, 4], [0, 8, 12, 5],
        [0, 12, 15, 6], [0, 10, 12, 5], [0, 8, 10, 4], [0, 5, 8, 3],
    ],
    summary="🔴 15:03 Pod重启暴增 — java-backend OOMKilled(CrashLoopBackOff), 12次重启/30min")


# ═══════════════════ Scenario: coredns_cache_poison (k8s) ═══════════════════
_k8s_coredns_poison = lambda: _fmt("Kubernetes", "prod-us-east", "15:00",
    cols=["NotReady", "PodRestarts", "API Lat(ms)", "DNS Lat(ms)"],
    rows=[
        [0, 0, 5, 2], [0, 0, 5, 2], [0, 0, 5, 2],
        [0, 0, 6, 5], [0, 0, 8, 15], [0, 0, 10, 25],
        [0, 0, 8, 18], [0, 0, 6, 10], [0, 0, 5, 5], [0, 0, 5, 3],
    ],
    summary="⚠ DNS延迟间歇升高2→25ms — CoreDNS缓存返回stale IP,无Pod重启/节点NotReady")


# ═══════════════════ Scenario: etcd_quorum_loss (k8s) ═══════════════════
_k8s_etcd_quorum = lambda: _fmt("Kubernetes", "prod-us-east", "15:00",
    cols=["NotReady", "PodRestarts", "API Lat(ms)", "DNS Lat(ms)"],
    rows=[
        [0, 0, 5, 2], [0, 0, 8, 3], [0, 0, 25, 5],
        [0, 2, 150, 20], [0, 5, 500, 80], [0, 8, 2000, 250],
        [0, 10, 5000, 500], [0, 8, 3000, 350], [0, 5, 1500, 200], [0, 3, 800, 100],
    ],
    summary="🔴 15:03 API Lat 5→5000ms — etcd quorum lost(2/3 down), API Server只读模式")


# ═══════════════════ Scenario: image_pull_backoff (k8s) ═══════════════════
_k8s_image_pull = lambda: _fmt("Kubernetes", "prod-us-east", "15:00",
    cols=["NotReady", "PodRestarts", "API Lat(ms)", "DNS Lat(ms)"],
    rows=[[0, 0, 5, 2]] * 10,
    summary="✅ K8s集群指标正常 — ImagePullBackOff仅影响新Pod,已有Pod正常运行,无重启/NotReady")


# ═══════════════════ Scenario: dns_and_etcd (k8s) ═══════════════════
_k8s_dns_etcd = lambda: _fmt("Kubernetes", "prod-us-east", "15:00",
    cols=["NotReady", "PodRestarts", "API Lat(ms)", "DNS Lat(ms)"],
    rows=[
        [0, 0, 8, 5], [0, 0, 12, 8], [0, 2, 30, 50],
        [0, 5, 80, 200], [0, 8, 150, 500], [0, 10, 200, 800],
        [0, 8, 180, 600], [0, 6, 120, 400], [0, 4, 80, 200], [0, 3, 50, 100],
    ],
    summary="🔴 DNS Lat 5→800ms + Pod重启10次 — CoreDNS CrashLoopBackOff + etcd leader频繁切换")


# ═══════════════════ Scenario: multi_layer_cascading (k8s) ═══════════════════
_k8s_multi_layer = lambda: _fmt("Kubernetes", "prod-us-east", "15:00",
    cols=["NotReady", "PodRestarts", "API Lat(ms)", "DNS Lat(ms)"],
    rows=[
        [0, 0, 8, 3], [0, 0, 10, 5], [1, 2, 50, 25],
        [2, 5, 150, 80], [2, 10, 350, 200], [3, 18, 500, 500],
        [3, 20, 600, 800], [3, 18, 500, 600], [2, 15, 350, 400], [2, 12, 250, 300],
    ],
    summary="🔴 多层叠加: 3节点NotReady + Pod重启20次 + API Lat 8→600ms + DNS Lat 3→800ms")


# ═══════════════════ Scenario: arp_cache_full (k8s) ═══════════════════
_net_arp = lambda: _fmt("Network", "prod-us-east/worker-3", "15:00",
    cols=["RX(Mbps)", "TX(Mbps)", "丢包", "重传%", "ESTAB"],
    rows=[
        [200, 150, 0, 1.0, 8500], [210, 155, 0, 1.2, 8800],
        [220, 160, 50, 1.5, 9200], [230, 170, 500, 3.0, 9500],
        [240, 180, 2000, 6.0, 10000], [250, 190, 5000, 10.0, 10500],
        [245, 185, 4800, 9.5, 10200], [240, 180, 4500, 9.0, 10000],
        [235, 175, 4000, 8.0, 9800], [230, 170, 3500, 7.0, 9500],
    ],
    summary="🔴 15:04 ARP缓存溢出(gc_thresh3=1024) → 跨节点丢包0→5000, 同节点通信正常")

_k8s_arp = lambda: _fmt("Kubernetes", "prod-us-east", "15:00",
    cols=["NotReady", "PodRestarts", "API Lat(ms)", "DNS Lat(ms)"],
    rows=[
        [0, 0, 5, 2], [0, 0, 5, 2], [0, 0, 5, 2],
        [0, 0, 6, 5], [0, 0, 8, 15], [0, 0, 10, 25],
        [0, 0, 8, 20], [0, 0, 7, 15], [0, 0, 6, 10], [0, 0, 5, 5],
    ],
    summary="⚠ DNS延迟间歇升高(ARP丢包影响UDP查询), 无Pod重启/节点NotReady")


# ═══════════════════ Scenario: mtu_mismatch (k8s) ═══════════════════
_net_mtu = lambda: _fmt("Network", "prod-us-east/worker-2", "15:00",
    cols=["RX(Mbps)", "TX(Mbps)", "丢包", "重传%", "ESTAB"],
    rows=[
        [150, 100, 0, 0.8, 3200], [155, 105, 0, 0.9, 3300],
        [160, 110, 0, 1.0, 3400], [165, 115, 100, 2.5, 3500],
        [170, 120, 500, 5.0, 3600], [175, 125, 1200, 8.0, 3700],
        [172, 122, 1100, 7.5, 3650], [170, 120, 900, 6.5, 3600],
        [165, 115, 700, 5.0, 3500], [160, 110, 400, 3.5, 3400],
    ],
    summary="🔴 15:03 大包丢包(MTU不匹配) — 小请求正常,大文件上传失败,ping大包不通")

_k8s_mtu = lambda: _fmt("Kubernetes", "prod-us-east", "15:00",
    cols=["NotReady", "PodRestarts", "API Lat(ms)", "DNS Lat(ms)"],
    rows=[[0, 0, 5, 2]] * 10,
    summary="✅ K8s集群指标正常 — MTU不匹配影响大包传输,不影响K8s控制面")


# ═══════════════════ Scenario: gpu_oom (host) ═══════════════════
_cpu_gpu = lambda: _fmt("CPU", "prod-web-01", "15:00",
    cols=["CPU%", "Load", "iowait%", "Top Process"],
    rows=[
        [25, 1.5, 2, "python 20%"], [28, 1.8, 2, "python 22%"],
        [30, 2.0, 2, "python 25%"], [32, 2.2, 2, "python 28%"],
        [35, 2.5, 3, "python 30%"], [30, 2.0, 3, "python 15% (CUDA OOM!)"],
        [22, 1.2, 2, "python 18%"], [25, 1.5, 2, "python 20%"],
        [28, 1.8, 2, "python 22%"], [30, 2.0, 2, "python 25%"],
    ],
    summary="⚠ 15:05 CUDA OOM — GPU显存耗尽触发进程kill, CPU短暂回落后恢复训练")

_mem_gpu = lambda: _fmt("Memory", "prod-web-01", "15:00",
    cols=["Mem%", "Swap%", "Available(GiB)", "OOM Events"],
    rows=[
        [45, 0, 3.5, "—"], [48, 0, 3.2, "—"], [50, 0, 3.0, "—"],
        [52, 0, 2.8, "—"], [55, 0, 2.5, "—"], [50, 0, 3.0, "GPU OOM进程已kill"],
        [48, 0, 3.2, "—"], [46, 0, 3.4, "—"], [45, 0, 3.5, "—"], [45, 0, 3.5, "—"],
    ],
    summary="✅ 主机内存正常 — CUDA OOM是GPU显存问题,不影响主机内存")


_CPU_DATA: dict[str, Callable[[], str]] = {
    "stress_cpu_and_tc_loss": _cpu_stress_tc,
    "softirq_starvation": _cpu_softirq,
    "tcp_accept_overflow": _cpu_normal,
    "high_cpu_iowait": _cpu_high_iowait,
    "mixed_io_memory": _cpu_normal,
    "disk_full_var_log": _cpu_normal,
    "fs_inode_exhaustion": _cpu_normal,
    "systemd_limit_nofile": _cpu_normal,
    "ebpf_probe_overhead": _cpu_ebpf,
    "swap_thrashing": _cpu_swap,
    "kubelet_disk_io_starvation": _cpu_kubelet_io,
    "node_oom_kubelet_killed": _cpu_node_oom,
    "oom_score_misconfig": _cpu_oom_score,
    "cpu_throttle_probe_failure": _cpu_throttle,
    "memory_leak": _cpu_normal,
    "disk_bottleneck": _cpu_normal,
    "network_congestion": _cpu_normal,
    "gpu_oom": _cpu_gpu,
    "arp_cache_full": _cpu_normal,
    "mtu_mismatch": _cpu_normal,
    "conntrack_and_oom": _cpu_conntrack_oom,
    "disk_io_and_dns": _cpu_disk_dns,
    "conntrack_table_full": _cpu_conntrack_only,
    "memory_leak_and_disk_full": _cpu_mem_leak_disk,
    "etcd_quota_near_full": lambda: _fmt("CPU", "prod-us-east/master-1", "18:00",
        cols=["CPU%", "Load", "iowait%", "Top Process"],
        rows=[
            [20, 0.8, 2, "etcd 5%"],
            [22, 0.9, 3, "etcd 6%"],
            [25, 1.0, 5, "etcd 8% (compaction)"],
            [25, 1.1, 5, "etcd 8%"],
            [28, 1.2, 6, "etcd 10%"],
            [28, 1.2, 6, "etcd 10%"],
            [30, 1.3, 7, "etcd 12%"],
            [30, 1.3, 7, "etcd 12%"],
            [28, 1.2, 6, "etcd 10%"],
            [28, 1.2, 6, "etcd 10%"],
        ],
        summary="⚠ CPU 正常（20-30%），etcd compaction 轻度增加iowait（5-7%），无CPU瓶颈"),
    "multi_layer_cascading": lambda: _fmt("CPU", "prod-us-east/master-1+worker-3", "15:00",
        cols=["CPU%", "Load", "iowait%", "Top Process"],
        rows=[
            [15, 0.6, 3, "etcd 4%"],
            [18, 0.8, 5, "etcd 6%"],
            [25, 1.5, 18, "etcd 15% (D-state)"],
            [35, 2.8, 32, "etcd 22% (D-state)"],
            [40, 4.2, 45, "etcd 30% (D-state)"],
            [38, 5.5, 48, "java 35% (worker-3 OOM reclaim)"],
            [42, 6.0, 50, "etcd 28% + kswapd 12%"],
            [40, 5.8, 48, "etcd 25% + kswapd 10%"],
            [35, 4.5, 40, "etcd 20%"],
            [30, 3.2, 30, "etcd 15%"],
        ],
        summary="🔴 master-1 iowait 3→50%(etcd D-state IO hang) + worker-3 sys%飙升(OOM页面回收)"),
    "dns_and_etcd": _cpu_normal,
    # ── K8s scenarios: host resources normal (root cause is cluster-level) ──
    "container_crash": _cpu_normal,
    "coredns_cache_poison": _cpu_normal,
    "image_pull_backoff": _cpu_normal,
    # ── etcd_quorum_loss: disk delay causes iowait on etcd nodes ──
    "etcd_quorum_loss": lambda: _fmt("CPU", "prod-us-east/etcd-node", "15:00",
        cols=["CPU%", "Load", "iowait%", "Top Process"],
        rows=[
            [15, 0.8, 3, "etcd 5%"],
            [18, 1.0, 5, "etcd 8%"],
            [25, 1.8, 22, "etcd 18% (D-state)"],
            [32, 2.5, 35, "etcd 25% (D-state)"],
            [28, 2.2, 30, "etcd 20% (D-state)"],
            [22, 1.8, 22, "etcd 15%"],
            [18, 1.2, 10, "etcd 10%"],
            [15, 0.8, 5, "etcd 6%"],
            [15, 0.8, 3, "etcd 5%"],
            [15, 0.8, 3, "etcd 5%"],
        ],
        summary="⚠ etcd节点 iowait 3→35% — 磁盘延迟致etcd D-state阻塞，leader选举失败"),
}

_MEM_DATA: dict[str, Callable[[], str]] = {
    "stress_cpu_and_tc_loss": _mem_stress_tc,
    "memory_leak": _mem_memory_leak,
    "disk_bottleneck": _mem_normal,
    "network_congestion": _mem_normal,
    "softirq_starvation": _mem_normal,
    "tcp_accept_overflow": _mem_normal,
    "high_cpu_iowait": _mem_normal,
    "mixed_io_memory": _mem_mixed,
    "disk_full_var_log": _mem_normal,
    "fs_inode_exhaustion": _mem_normal,
    "systemd_limit_nofile": _mem_normal,
    "ebpf_probe_overhead": _mem_normal,
    "swap_thrashing": _mem_swap,
    "kubelet_disk_io_starvation": _mem_normal,
    "node_oom_kubelet_killed": _mem_node_oom,
    "oom_score_misconfig": _mem_oom_score,
    "cpu_throttle_probe_failure": _mem_normal,
    "gpu_oom": _mem_gpu,
    "arp_cache_full": _mem_normal,
    "mtu_mismatch": _mem_normal,
    "conntrack_and_oom": _mem_conntrack_oom,
    "disk_io_and_dns": _mem_disk_dns,
    "memory_leak_and_disk_full": _mem_mem_leak_disk,
    "etcd_quota_near_full": lambda: _fmt("Memory", "prod-us-east/master-1", "18:00",
        cols=["Mem%", "Swap%", "Available(GiB)", "OOM Events"],
        rows=[[50, 0, 3.0, "—"]] * 10,
        summary="✅ 内存正常 — 无 OOM 风险"),
    "multi_layer_cascading": lambda: _fmt("Memory", "prod-us-east/worker-3", "15:00",
        cols=["Mem%", "Swap%", "Available(GiB)", "OOM Events"],
        rows=[
            [72, 5, 1.8, "—"],
            [75, 8, 1.5, "—"],
            [80, 15, 1.1, "—"],
            [85, 25, 0.8, "—"],
            [90, 40, 0.5, "—"],
            [95, 60, 0.2, "OOM Kill: kubelet(pid=3210)"],
            [92, 55, 0.4, "—"],
            [88, 45, 0.6, "—"],
            [85, 35, 0.9, "—"],
            [82, 28, 1.2, "—"],
        ],
        summary="🔴 worker-3 内存枯竭 72→95% + Swap 5→60% → 15:05 OOM Kill kubelet(pid=3210, RSS=6.2GB java)"),
    "dns_and_etcd": _mem_normal,
    # ── K8s scenarios: host memory normal ──
    "container_crash": _mem_normal,
    "coredns_cache_poison": _mem_normal,
    "etcd_quorum_loss": _mem_normal,
    "image_pull_backoff": _mem_normal,
    "conntrack_table_full": _mem_normal,
}

_DISK_DATA: dict[str, Callable[[], str]] = {
    "stress_cpu_and_tc_loss": _disk_stress_tc,
    "disk_bottleneck": _disk_disk_bottleneck,
    "memory_leak": _disk_normal,
    "network_congestion": _disk_normal,
    "softirq_starvation": _disk_normal,
    "tcp_accept_overflow": _disk_normal,
    "high_cpu_iowait": _disk_high_iowait,
    "mixed_io_memory": _disk_mixed,
    "disk_full_var_log": _disk_full_varlog,
    "fs_inode_exhaustion": _disk_inode,
    "systemd_limit_nofile": _disk_normal,
    "ebpf_probe_overhead": _disk_normal,
    "swap_thrashing": _disk_swap,
    "kubelet_disk_io_starvation": _disk_kubelet_io,
    "node_oom_kubelet_killed": _disk_normal,
    "oom_score_misconfig": _disk_normal,
    "cpu_throttle_probe_failure": _disk_normal,
    "gpu_oom": _disk_normal,
    "arp_cache_full": _disk_normal,
    "mtu_mismatch": _disk_normal,
    "conntrack_and_oom": _disk_conntrack_oom,
    "disk_io_and_dns": _disk_disk_dns,
    "memory_leak_and_disk_full": _disk_mem_leak_disk,
    "etcd_quota_near_full": lambda: _fmt("Disk", "prod-us-east/master-1", "18:00",
        cols=["Util%", "IOPS(r/s)", "IOPS(w/s)", "await(ms)"],
        rows=[
            [30, 50, 100, 8.5],
            [32, 55, 110, 9.2],
            [35, 60, 120, 10.0],
            [45, 80, 150, 22.5],  # ← compaction 开始，IO升高
            [45, 80, 150, 22.5],
            [45, 80, 150, 22.5],
            [45, 80, 150, 22.5],
            [42, 75, 140, 20.0],
            [40, 70, 130, 18.0],
            [38, 65, 120, 15.0],
        ],
        summary="⚠ 磁盘 IO util 30→45% — etcd compaction 导致IO中等升高但远未饱和"),
    "dns_and_etcd": lambda: _fmt("Disk", "prod-us-east/master-1", "15:00",
        cols=["Util%", "IOPS(r/s)", "IOPS(w/s)", "await(ms)"],
        rows=[
            [20, 40, 80, 5.0],
            [22, 42, 85, 6.0],
            [35, 55, 110, 28.0],   # ← 磁盘间歇不稳定
            [50, 70, 140, 65.0],   # ← await 飙升→ etcd backend_commit 380ms
            [25, 45, 90, 8.0],     # ← 回落
            [48, 68, 135, 55.0],   # ← 再次飙升
            [30, 50, 100, 12.0],   # ← 回落
            [55, 75, 145, 80.0],   # ← 第三次飙升→ leader election
            [32, 52, 105, 15.0],
            [45, 65, 130, 45.0],   # ← 尾部仍不稳定
        ],
        summary="⚠ master-1 磁盘间歇不稳定 — await 5→80ms 反复波动 → etcd leader频繁切换(p99 backend_commit=380ms)"),
    "multi_layer_cascading": lambda: _fmt("Disk", "prod-us-east/master-1/sda", "15:00",
        cols=["Util%", "IOPS(r/s)", "IOPS(w/s)", "await(ms)"],
        rows=[
            [25, 40, 80, 5.0],
            [30, 45, 90, 8.0],
            [55, 60, 120, 35.0],
            [78, 80, 150, 85.0],
            [92, 90, 160, 145.0],
            [99, 95, 165, 185.0],
            [99.8, 92, 160, 185.0],
            [99.5, 88, 155, 180.0],
            [98, 85, 150, 170.0],
            [95, 80, 140, 150.0],
        ],
        summary="🔴 master-1 sda %util 25→99.8% + await 5→185ms — etcd 数据盘 IO hang (D-state进程堆积)"),
    # ── K8s scenarios: host disk normal ──
    "container_crash": _disk_normal,
    "coredns_cache_poison": _disk_normal,
    "image_pull_backoff": _disk_normal,
    "conntrack_table_full": _disk_normal,
    # ── etcd_quorum_loss: disk delay on etcd data disk ──
    "etcd_quorum_loss": lambda: _fmt("Disk", "prod-us-east/etcd-node", "15:00",
        cols=["Util%", "IOPS(r/s)", "IOPS(w/s)", "await(ms)"],
        rows=[
            [25, 40, 80, 5.0],
            [30, 45, 90, 8.0],
            [55, 60, 120, 38.0],
            [82, 80, 150, 95.0],
            [75, 70, 140, 85.0],
            [48, 55, 100, 25.0],
            [32, 45, 85, 10.0],
            [28, 42, 82, 8.0],
            [25, 40, 80, 5.0],
            [25, 40, 80, 5.0],
        ],
        summary="🔴 etcd数据盘 await 5→95ms + util 25→82% — 磁盘延迟致etcd多数派丢失"),
}

_NET_DATA: dict[str, Callable[[], str]] = {
    "stress_cpu_and_tc_loss": _net_stress_tc,
    "network_congestion": _net_network_congestion,
    "memory_leak": _net_normal,
    "disk_bottleneck": _net_normal,
    "softirq_starvation": _net_softirq,
    # ── K8s scenarios: host network normal ──
    "container_crash": _net_normal,
    "coredns_cache_poison": _net_normal,
    "etcd_quorum_loss": _net_normal,
    "image_pull_backoff": _net_normal,
    "tcp_accept_overflow": _net_tcp_overflow,
    "high_cpu_iowait": _net_normal,
    "mixed_io_memory": _net_normal,
    "disk_full_var_log": _net_normal,
    "fs_inode_exhaustion": _net_normal,
    "systemd_limit_nofile": _net_systemd,
    "ebpf_probe_overhead": _net_normal,
    "swap_thrashing": _net_normal,
    "kubelet_disk_io_starvation": _net_normal,
    "node_oom_kubelet_killed": _net_normal,
    "oom_score_misconfig": _net_normal,
    "cpu_throttle_probe_failure": _net_normal,
    "gpu_oom": _net_normal,
    "arp_cache_full": _net_arp,
    "mtu_mismatch": _net_mtu,
    "conntrack_and_oom": _net_conntrack_oom,
    "disk_io_and_dns": _net_disk_dns,
    "conntrack_table_full": _net_conntrack_only,
    "memory_leak_and_disk_full": _net_mem_leak_disk,
    "etcd_quota_near_full": lambda: _fmt("Network", "prod-us-east/worker-1", "18:00",
        cols=["RX(Mbps)", "TX(Mbps)", "丢包", "重传%", "ESTAB"],
        rows=[
            [120, 80, 0, 0.5, 350],
            [125, 82, 0, 0.5, 352],
            [130, 85, 0, 0.6, 355],
            [128, 84, 0, 0.5, 350],
            [132, 86, 0, 0.6, 358],
            [130, 85, 0, 0.5, 355],
            [125, 82, 0, 0.5, 352],
            [128, 84, 0, 0.6, 350],
            [130, 85, 0, 0.5, 355],
            [128, 84, 0, 0.5, 352],
        ],
        summary="✅ 网络接口层正常 — 丢包=0，重传正常（DNS超时不来自网络层）"),
    "multi_layer_cascading": lambda: _fmt("Network", "prod-us-east/worker-3", "15:00",
        cols=["RX(Mbps)", "TX(Mbps)", "丢包", "重传%", "ESTAB"],
        rows=[
            [150, 100, 0, 0.5, 420],
            [155, 102, 0, 0.5, 425],
            [148, 98, 0, 0.6, 418],
            [152, 100, 0, 0.5, 422],
            [150, 99, 0, 0.5, 420],
            [155, 102, 0, 0.6, 428],
            [150, 100, 0, 0.5, 422],
            [148, 98, 0, 0.5, 418],
            [152, 100, 0, 0.5, 420],
            [150, 99, 0, 0.5, 422],
        ],
        summary="✅ 网络接口层正常 — 丢包=0，重传正常（DNS间歇超时不来自网络层，是CoreDNS受etcd延迟影响）"),
    "dns_and_etcd": _net_normal,
}


# ═══════════════════ NODES DATA (NotReady / PodRestarts / Evictions / Pending) ═══════════════════

_NODES_DATA: dict[str, Callable[[], str]] = {
    "memory_leak_and_disk_full": lambda: _fmt("Nodes", "prod-us-east", "15:00",
        cols=["NotReady", "PodRestarts", "Evictions", "Pending"],
        rows=[
            [0, 0, 0, 0], [0, 0, 0, 0], [0, 0, 0, 0],
            [0, 0, 0, 0], [0, 0, 0, 0],
            [0, 3, 0, 0],  # ← OOMKilled Pod 重启
            [0, 5, 0, 0], [0, 2, 0, 0],
            [1, 4, 2, 0],  # ← DiskPressure → NotReady + Evictions
            [1, 6, 3, 0],
        ],
        summary="🔴 15:05 Pod OOMKilled 重启 3次 → 15:08 DiskPressure 导致 worker-4 NotReady + 2次驱逐"),
    "container_crash": lambda: _fmt("Nodes", "prod-us-east", "15:00",
        cols=["NotReady", "PodRestarts", "Evictions", "Pending"],
        rows=[
            [0, 0, 0, 0], [0, 0, 0, 0], [0, 0, 0, 0],
            [0, 3, 0, 0], [0, 5, 0, 0], [0, 8, 0, 0],
            [0, 12, 0, 0], [0, 10, 0, 0], [0, 8, 0, 0], [0, 5, 0, 0],
        ],
        summary="🔴 15:03 Pod重启暴增 — java-backend OOMKilled(CrashLoopBackOff), 12次重启/30min, 无节点异常"),
    "coredns_cache_poison": _nodes_default,
    "etcd_quorum_loss": lambda: _fmt("Nodes", "prod-us-east", "15:00",
        cols=["NotReady", "PodRestarts", "Evictions", "Pending"],
        rows=[
            [0, 0, 0, 0], [0, 0, 0, 0], [0, 0, 0, 0],
            [0, 2, 0, 1], [0, 5, 0, 3], [0, 8, 2, 5],
            [0, 10, 3, 8], [0, 8, 2, 5], [0, 5, 1, 3], [0, 3, 0, 1],
        ],
        summary="🔴 15:03 Pod重启暴增(API Server只读模式无法调度) + Pending Pod堆积(Deployment无法创建)"),
    "image_pull_backoff": lambda: _fmt("Nodes", "prod-us-east", "15:00",
        cols=["NotReady", "PodRestarts", "Evictions", "Pending"],
        rows=[[0, 0, 0, 2]] * 10,
        summary="⚠ 2个 Pod Pending(ImagePullBackOff) — 仅影响新 Pod, 无重启/NotReady"),
    "dns_and_etcd": lambda: _fmt("Nodes", "prod-us-east", "15:00",
        cols=["NotReady", "PodRestarts", "Evictions", "Pending"],
        rows=[
            [0, 0, 0, 0], [0, 0, 0, 0], [0, 2, 0, 0],
            [0, 5, 0, 0], [0, 8, 0, 0], [0, 10, 0, 0],
            [0, 8, 0, 0], [0, 6, 0, 0], [0, 4, 0, 0], [0, 3, 0, 0],
        ],
        summary="🔴 Pod重启10次 — CoreDNS CrashLoopBackOff, 无节点NotReady"),
    "multi_layer_cascading": lambda: _fmt("Nodes", "prod-us-east", "15:00",
        cols=["NotReady", "PodRestarts", "Evictions", "Pending"],
        rows=[
            [0, 0, 0, 0], [0, 0, 0, 0], [1, 2, 1, 0],
            [2, 5, 3, 1], [2, 10, 5, 2], [3, 18, 8, 3],
            [3, 20, 10, 5], [3, 18, 8, 3], [2, 15, 5, 2], [2, 12, 3, 1],
        ],
        summary="🔴 3节点NotReady + Pod重启20次 + 驱逐10次 — 多层叠加, master-1 IO饱和 + worker-3 OOM"),
    "arp_cache_full": _nodes_default,
    "mtu_mismatch": _nodes_default,
    "kubelet_disk_io_starvation": lambda: _fmt("Nodes", "prod-us-east/worker-2", "15:00",
        cols=["NotReady", "PodRestarts", "Evictions", "Pending"],
        rows=[
            [0, 0, 0, 0], [0, 0, 0, 0], [0, 0, 0, 0],
            [1, 0, 0, 0], [1, 0, 1, 0], [1, 5, 3, 0],
            [1, 10, 5, 0], [1, 8, 3, 0], [1, 5, 2, 0], [1, 3, 1, 0],
        ],
        summary="🔴 15:03 worker-2 NotReady(kubelet lease超时) + 驱逐5次, Pod重启10次"),
    "node_oom_kubelet_killed": lambda: _fmt("Nodes", "prod-us-east/worker-2", "15:00",
        cols=["NotReady", "PodRestarts", "Evictions", "Pending"],
        rows=[
            [0, 0, 0, 0], [0, 0, 0, 0], [0, 0, 0, 0],
            [0, 0, 0, 0], [0, 0, 0, 0], [0, 0, 0, 0],
            [1, 0, 0, 8], [1, 0, 0, 12], [1, 0, 0, 10], [1, 0, 0, 6],
        ],
        summary="🔴 15:06 worker-2 NotReady(kubelet被OOM Kill) + 12个Pod Pending(节点不可调度)"),
    "oom_score_misconfig": lambda: _fmt("Nodes", "prod-us-east/worker-2", "15:00",
        cols=["NotReady", "PodRestarts", "Evictions", "Pending"],
        rows=[
            [0, 0, 0, 0], [0, 0, 0, 0], [0, 0, 0, 0],
            [0, 0, 0, 0], [0, 0, 0, 0], [0, 0, 0, 0],
            [1, 0, 0, 5], [1, 0, 0, 8], [1, 0, 0, 6], [1, 0, 0, 4],
        ],
        summary="🔴 15:06 worker-2 NotReady(kubelet被误杀, oom_score_adj配置错误), 应用Pod反而存活"),
    "cpu_throttle_probe_failure": lambda: _fmt("Nodes", "prod-us-east/worker-1", "15:00",
        cols=["NotReady", "PodRestarts", "Evictions", "Pending"],
        rows=[
            [0, 0, 0, 0], [0, 0, 0, 0], [0, 0, 0, 0],
            [0, 2, 0, 0], [0, 5, 0, 0], [0, 8, 0, 0],
            [0, 10, 0, 0], [0, 8, 0, 0], [0, 5, 0, 0], [0, 3, 0, 0],
        ],
        summary="🔴 15:03 Pod重启暴增(CPU节流→探针超时→健康Pod被Kill) — 非OOM/Crash"),
    "disk_io_and_dns": lambda: _fmt("Nodes", "prod-us-east", "15:00",
        cols=["NotReady", "PodRestarts", "Evictions", "Pending"],
        rows=[[0, 0, 0, 0]] * 10,
        summary="✅ 节点和Pod状态正常 — DNS超时不影响Pod生命周期"),
    "conntrack_and_oom": lambda: _fmt("Nodes", "prod-us-east", "15:00",
        cols=["NotReady", "PodRestarts", "Evictions", "Pending"],
        rows=[
            [0, 0, 0, 0], [0, 0, 0, 0], [0, 0, 0, 0],
            [0, 0, 0, 0], [0, 0, 0, 0],
            [0, 8, 0, 0], [0, 12, 0, 0], [0, 8, 0, 0], [0, 5, 0, 0], [0, 3, 0, 0],
        ],
        summary="🔴 15:05 Pod重启 8次(OOMKilled), 无节点NotReady/驱逐"),
    "conntrack_table_full": lambda: _fmt("Nodes", "prod-us-east", "15:00",
        cols=["NotReady", "PodRestarts", "Evictions", "Pending"],
        rows=[
            [0, 0, 0, 0], [0, 0, 0, 0], [0, 0, 0, 0], [0, 0, 0, 0],
            [2, 0, 3, 0], [2, 0, 5, 0], [2, 5, 8, 0],
            [2, 30, 15, 0], [2, 30, 20, 0], [2, 30, 22, 0],
        ],
        summary="🔴 15:04 worker-5/8 NotReady → 驱逐3→22次 → Pod重启30次(跨节点重建)"),
    "etcd_quota_near_full": lambda: _fmt("Nodes", "prod-us-east", "18:00",
        cols=["NotReady", "PodRestarts", "Evictions", "Pending"],
        rows=[
            [0, 0, 0, 0], [0, 0, 0, 0], [0, 0, 0, 0],
            [0, 0, 0, 0], [0, 0, 0, 1],
            [0, 2, 0, 3], [0, 5, 0, 5], [0, 8, 0, 8],
            [0, 5, 0, 5], [0, 3, 0, 3],
        ],
        summary="🔴 18:05 Pod重启8次(探针超时) + Pending Pod堆积(etcd慢导致调度延迟)"),
}


# ═══════════════════ SERVICES DATA (API Lat / etcd Leader / DB Size / DNS Lat / DNS Err) ═══════════════════

_SERVICES_DATA: dict[str, Callable[[], str]] = {
    "memory_leak_and_disk_full": lambda: _fmt("Services", "prod-us-east", "15:00",
        cols=["API Lat(ms)", "etcd Ldr", "DB(MB)", "DNS Lat(ms)", "DNS Err"],
        rows=[
            [5, 1, 1200, 2, 0], [5, 1, 1200, 2, 0], [6, 1, 1200, 3, 0],
            [8, 1, 1200, 3, 0], [12, 1, 1200, 5, 0],
            [15, 1, 1200, 8, 0], [10, 1, 1200, 5, 0], [8, 1, 1200, 3, 0],
            [12, 1, 1200, 6, 0], [18, 1, 1200, 10, 0],
        ],
        summary="⚠ API Lat 5→18ms + DNS Lat 2→10ms — 轻度升高(节点驱逐增加API请求), etcd稳定"),
    "container_crash": lambda: _fmt("Services", "prod-us-east", "15:00",
        cols=["API Lat(ms)", "etcd Ldr", "DB(MB)", "DNS Lat(ms)", "DNS Err"],
        rows=[
            [5, 1, 1200, 2, 0], [5, 1, 1200, 2, 0], [5, 1, 1200, 2, 0],
            [8, 1, 1200, 3, 0], [10, 1, 1200, 4, 0], [12, 1, 1200, 5, 0],
            [15, 1, 1200, 6, 0], [12, 1, 1200, 5, 0], [10, 1, 1200, 4, 0], [8, 1, 1200, 3, 0],
        ],
        summary="⚠ API Lat 5→15ms(重启Pod重建请求增加), etcd/DNS 正常"),
    "coredns_cache_poison": lambda: _fmt("Services", "prod-us-east", "15:00",
        cols=["API Lat(ms)", "etcd Ldr", "DB(MB)", "DNS Lat(ms)", "DNS Err"],
        rows=[
            [5, 1, 1200, 2, 0], [5, 1, 1200, 2, 0], [5, 1, 1200, 2, 0],
            [6, 1, 1200, 5, 2], [8, 1, 1200, 15, 8], [10, 1, 1200, 25, 15],
            [8, 1, 1200, 18, 10], [6, 1, 1200, 10, 5], [5, 1, 1200, 5, 2], [5, 1, 1200, 3, 0],
        ],
        summary="🔴 DNS Lat 2→25ms + DNS Err 0→15 — CoreDNS缓存污染返回stale IP, API Server正常"),
    "etcd_quorum_loss": lambda: _fmt("Services", "prod-us-east", "15:00",
        cols=["API Lat(ms)", "etcd Ldr", "DB(MB)", "DNS Lat(ms)", "DNS Err"],
        rows=[
            [5, 1, 1200, 2, 0], [8, 0, 1200, 3, 0], [25, 0, 1200, 5, 0],
            [150, 0, 1200, 20, 5], [500, 0, 1200, 80, 20],
            [2000, 0, 1200, 250, 50], [5000, 0, 1200, 500, 80],
            [3000, 0, 1200, 350, 50], [1500, 0, 1200, 200, 30], [800, 0, 1200, 100, 15],
        ],
        summary="🔴 15:03 etcd leader=0(quorum lost, 2/3 down) → API Lat 5→5000ms(只读模式) + DNS级联恶化"),
    "image_pull_backoff": _services_default,
    "dns_and_etcd": lambda: _fmt("Services", "prod-us-east", "15:00",
        cols=["API Lat(ms)", "etcd Ldr", "DB(MB)", "DNS Lat(ms)", "DNS Err"],
        rows=[
            [8, 1, 1200, 5, 0], [12, 1, 1200, 8, 2], [30, 0, 1200, 50, 10],
            [80, 0, 1200, 200, 30], [150, 1, 1200, 500, 60], [200, 0, 1200, 800, 80],
            [180, 1, 1200, 600, 50], [120, 0, 1200, 400, 30], [80, 1, 1200, 200, 15], [50, 1, 1200, 100, 5],
        ],
        summary="🔴 etcd leader频繁切换(0↔1) + API Lat 8→200ms + DNS Lat 5→800ms + DNS Err 0→80"),
    "multi_layer_cascading": lambda: _fmt("Services", "prod-us-east", "15:00",
        cols=["API Lat(ms)", "etcd Ldr", "DB(MB)", "DNS Lat(ms)", "DNS Err"],
        rows=[
            [8, 1, 1200, 3, 0], [10, 1, 1200, 5, 0], [50, 1, 1200, 25, 5],
            [150, 1, 1205, 80, 15], [350, 1, 1210, 200, 30], [500, 0, 1220, 500, 60],
            [600, 1, 1230, 800, 80], [500, 1, 1225, 600, 50], [350, 1, 1215, 400, 30], [250, 1, 1210, 300, 15],
        ],
        summary="🔴 API Lat 8→600ms + etcd leader切换 + DB增长1230MB + DNS Lat 3→800ms + DNS Err 0→80"),
    "arp_cache_full": lambda: _fmt("Services", "prod-us-east", "15:00",
        cols=["API Lat(ms)", "etcd Ldr", "DB(MB)", "DNS Lat(ms)", "DNS Err"],
        rows=[
            [5, 1, 1200, 2, 0], [5, 1, 1200, 2, 0], [5, 1, 1200, 2, 0],
            [6, 1, 1200, 5, 1], [8, 1, 1200, 15, 3], [10, 1, 1200, 25, 5],
            [8, 1, 1200, 20, 4], [7, 1, 1200, 15, 3], [6, 1, 1200, 10, 2], [5, 1, 1200, 5, 1],
        ],
        summary="⚠ DNS Lat 2→25ms + DNS Err 0→5(ARP丢包影响UDP查询), API Server/etcd正常"),
    "mtu_mismatch": _services_default,
    "kubelet_disk_io_starvation": lambda: _fmt("Services", "prod-us-east", "15:00",
        cols=["API Lat(ms)", "etcd Ldr", "DB(MB)", "DNS Lat(ms)", "DNS Err"],
        rows=[
            [5, 1, 1200, 2, 0], [5, 1, 1200, 2, 0], [5, 1, 1200, 2, 0],
            [8, 1, 1200, 3, 0], [12, 1, 1200, 5, 0], [15, 1, 1200, 8, 0],
            [12, 1, 1200, 5, 0], [10, 1, 1200, 4, 0], [8, 1, 1200, 3, 0], [6, 1, 1200, 2, 0],
        ],
        summary="⚠ API Lat 5→15ms(节点NotReady增加心跳请求), etcd/DNS正常"),
    "node_oom_kubelet_killed": lambda: _fmt("Services", "prod-us-east", "15:00",
        cols=["API Lat(ms)", "etcd Ldr", "DB(MB)", "DNS Lat(ms)", "DNS Err"],
        rows=[
            [5, 1, 1200, 2, 0], [5, 1, 1200, 2, 0], [5, 1, 1200, 2, 0],
            [6, 1, 1200, 2, 0], [6, 1, 1200, 3, 0], [8, 1, 1200, 3, 0],
            [15, 1, 1200, 8, 0], [12, 1, 1200, 5, 0], [10, 1, 1200, 4, 0], [8, 1, 1200, 3, 0],
        ],
        summary="⚠ API Lat 5→15ms(kubelet被杀后心跳中断), etcd/DNS正常"),
    "oom_score_misconfig": lambda: _fmt("Services", "prod-us-east", "15:00",
        cols=["API Lat(ms)", "etcd Ldr", "DB(MB)", "DNS Lat(ms)", "DNS Err"],
        rows=[
            [5, 1, 1200, 2, 0], [5, 1, 1200, 2, 0], [5, 1, 1200, 2, 0],
            [6, 1, 1200, 2, 0], [6, 1, 1200, 3, 0], [8, 1, 1200, 3, 0],
            [15, 1, 1200, 8, 0], [12, 1, 1200, 5, 0], [10, 1, 1200, 4, 0], [8, 1, 1200, 3, 0],
        ],
        summary="⚠ API Lat 5→15ms(kubelet被误杀), etcd/DNS正常"),
    "cpu_throttle_probe_failure": lambda: _fmt("Services", "prod-us-east", "15:00",
        cols=["API Lat(ms)", "etcd Ldr", "DB(MB)", "DNS Lat(ms)", "DNS Err"],
        rows=[
            [5, 1, 1200, 2, 0], [5, 1, 1200, 2, 0], [5, 1, 1200, 2, 0],
            [8, 1, 1200, 3, 0], [10, 1, 1200, 4, 0], [12, 1, 1200, 5, 0],
            [10, 1, 1200, 4, 0], [8, 1, 1200, 3, 0], [6, 1, 1200, 3, 0], [5, 1, 1200, 2, 0],
        ],
        summary="⚠ API Lat 5→12ms(Pod重启增加API请求), etcd/DNS正常"),
    "disk_io_and_dns": lambda: _fmt("Services", "prod-us-east", "15:00",
        cols=["API Lat(ms)", "etcd Ldr", "DB(MB)", "DNS Lat(ms)", "DNS Err"],
        rows=[
            [5, 1, 1200, 2, 0], [5, 1, 1200, 2, 0], [5, 1, 1200, 3, 0],
            [8, 1, 1200, 5000, 120], [6, 1, 1200, 5000, 150],
            [5, 1, 1200, 5000, 180], [6, 1, 1200, 4800, 160],
            [5, 1, 1200, 5000, 200], [5, 1, 1200, 4500, 150], [5, 1, 1200, 3000, 80],
        ],
        summary="🔴 DNS Lat 2→5000ms + DNS Err 0→200(CoreDNS forward指向已下线DNS), API Server/etcd正常"),
    "conntrack_and_oom": lambda: _fmt("Services", "prod-us-east", "15:00",
        cols=["API Lat(ms)", "etcd Ldr", "DB(MB)", "DNS Lat(ms)", "DNS Err"],
        rows=[
            [5, 1, 1200, 2, 0], [5, 1, 1200, 2, 0], [6, 1, 1200, 3, 0],
            [5, 1, 1200, 5, 0], [5, 1, 1200, 25, 5],
            [8, 1, 1200, 120, 20], [10, 1, 1200, 250, 40],
            [5, 1, 1200, 80, 15], [5, 1, 1200, 30, 5], [5, 1, 1200, 10, 2],
        ],
        summary="🔴 DNS Lat 2→250ms + DNS Err 0→40(conntrack导致UDP丢包), API Server/etcd正常"),
    "conntrack_table_full": lambda: _fmt("Services", "prod-us-east", "15:00",
        cols=["API Lat(ms)", "etcd Ldr", "DB(MB)", "DNS Lat(ms)", "DNS Err"],
        rows=[
            [5, 1, 1200, 2, 0], [5, 1, 1200, 2, 0], [6, 1, 1200, 2, 0], [5, 1, 1200, 2, 0],
            [8, 1, 1200, 25, 5], [10, 1, 1200, 120, 20], [12, 1, 1200, 250, 40],
            [15, 1, 1200, 500, 60], [18, 1, 1200, 800, 80], [20, 1, 1200, 1200, 100],
        ],
        summary="🔴 DNS Lat 2→1200ms + DNS Err 0→100(conntrack table full), API Lat轻度升高5→20ms"),
    "etcd_quota_near_full": lambda: _fmt("Services", "prod-us-east", "18:00",
        cols=["API Lat(ms)", "etcd Ldr", "DB(MB)", "DNS Lat(ms)", "DNS Err"],
        rows=[
            [8, 1, 7000, 5, 0],
            [12, 1, 7020, 8, 0],
            [45, 1, 7050, 15, 3],    # ← API Server 写延迟开始升高（etcd compaction）
            [150, 1, 7080, 80, 15],  # ← DNS 延迟开始升高（CoreDNS 查 etcd 变慢）
            [350, 1, 7100, 250, 30], # ← API Server 写入排队
            [580, 1, 7120, 500, 50], # ← Pod 重启开始（探针超时）
            [650, 1, 7140, 800, 80], # ← DNS 严重超时
            [850, 1, 7150, 1200, 100], # ← 峰值: DB Size 7.15GB 接近 8GB quota
            [720, 1, 7145, 900, 80],
            [480, 1, 7140, 600, 50],
        ],
        summary="🔴 etcd DB 7000→7150MB(逼近8GB NOSPACE) + API Lat 8→850ms + DNS Lat 5→1200ms + DNS Err 0→100"),
}


# ═══════════════════ Formatting ═══════════════════

_HHMM_RE = re.compile(r'(\d{1,2}:\d{2})')
_ISO_HHMM_RE = re.compile(r'T(\d{1,2}:\d{2})')


def _extract_hhmm(time_str: str) -> str:
    """Extract HH:MM from various time formats (ISO, plain HH:MM, etc.)."""
    if not time_str:
        return ""
    m = _ISO_HHMM_RE.search(time_str)
    if m:
        return m.group(1)
    m = _HHMM_RE.search(time_str)
    return m.group(1) if m else time_str


def _apply_query_overrides(
    text: str,
    name_chunk: str,
    start_time: str,
    end_time: str,
) -> str:
    """Apply query parameter overrides to formatted _fmt output.

    - name_chunk: replaces the endpoint display in the header
    - start_time / end_time: replaces the time range display
    """
    if not name_chunk and not start_time and not end_time:
        return text

    lines = text.split('\n', 2)
    if len(lines) < 3:
        return text

    # Override endpoint display in header: "## Argus CPU — prod-web-01"
    if name_chunk:
        lines[0] = re.sub(r' — .+$', f' — {name_chunk}', lines[0])

    # Override time range display
    start_hhmm = _extract_hhmm(start_time)
    end_hhmm = _extract_hhmm(end_time)
    if start_hhmm or end_hhmm:
        if start_hhmm and end_hhmm:
            new_range = f"**时间范围**: {start_hhmm} ~ {end_hhmm}  **粒度**: 1min"
        elif start_hhmm:
            new_range = f"**时间范围**: {start_hhmm} ~  默认窗口  **粒度**: 1min"
        else:
            new_range = f"**时间范围**: 默认 ~ {end_hhmm}  **粒度**: 1min"
        lines[1] = re.sub(
            r'\*\*时间范围\*\*:.*?\*\*粒度\*\*:\s*1min',
            new_range,
            lines[1],
        )

    return '\n'.join(lines)

def _fmt(
    subsystem: str,
    host: str,
    start_min: str,
    *,
    cols: list[str],
    rows: list[list],
    summary: str,
) -> str:
    hdr = f"## Argus {subsystem} — {host}\n"
    hdr += f"**时间范围**: {start_min} ~ {start_min[:-2]}{(int(start_min[-2:]) + len(rows) - 1):02d}  **粒度**: 1min\n\n"
    hdr += "| 时间 | " + " | ".join(cols) + " |\n"
    hdr += "|------|" + "|".join(["------" for _ in cols]) + "|\n"
    for i, row in enumerate(rows):
        mm = f"{(int(start_min[-2:]) + i):02d}"
        vals = " | ".join(str(v) for v in row)
        hdr += f"| {mm} | {vals} |\n"
    hdr += f"\n### 摘要\n{summary}"
    return hdr
