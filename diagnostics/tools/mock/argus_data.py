"""Argus monitoring data — scenario-specific 1min-granularity per-subsystem timelines.

Each scenario provides a 10-min window per subsystem so the Coordinator can:
1. See the temporal relationship between anomalies across subsystems
2. Identify the onset time and duration of each symptom
3. Cross-reference concurrent metric changes to infer causality

All data functions accept a scenario ID and return formatted text.
"""

from __future__ import annotations

from typing import Callable


# ═══════════════════ Public per-metric query API ═══════════════════

def query_argus_cpu_metrics(scenario: str) -> str:
    handler = _CPU_DATA.get(scenario, _default)
    return handler()


def query_argus_memory_metrics(scenario: str) -> str:
    handler = _MEM_DATA.get(scenario, _default)
    return handler()


def query_argus_disk_metrics(scenario: str) -> str:
    handler = _DISK_DATA.get(scenario, _default)
    return handler()


def query_argus_network_metrics(scenario: str) -> str:
    handler = _NET_DATA.get(scenario, _default)
    return handler()


def query_argus_k8s_metrics(scenario: str) -> str:
    handler = _K8S_DATA.get(scenario, _default)
    return handler()


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
            [42, 1.8, 2, "java 42% (RSS 4.2G)"],
            [45, 2.0, 2, "java 45% (RSS 4.5G)"],
            [48, 2.5, 3, "java 48% (RSS 4.8G)"],
            [50, 3.0, 5, "java 50% (RSS 5.2G)"],
            [55, 3.5, 8, "java 55% (RSS 5.8G)"],
            [58, 3.8, 10, "java 58% (RSS 6.4G) ⚠ OOMKilled!"],
            [42, 2.0, 3, "java 30% (Pod重启后)"],
            [45, 2.5, 3, "java 35%"],
            [48, 2.8, 3, "java 38%"],
            [50, 3.0, 4, "java 42%"],
        ],
        summary="🔴 15:05 Java OOMKilled (RSS 6.4GB)，sys%高因 softirq+内存回收")


def _mem_conntrack_oom() -> str:
    return _fmt("Memory", "prod-us-east/worker-3", "15:00",
        cols=["Mem%", "Swap%", "Available(MiB)", "OOM Events"],
        rows=[
            [65, 0, 2240, "—"],
            [68, 5, 1980, "—"],
            [72, 15, 1720, "—"],
            [78, 28, 1350, "—"],
            [82, 42, 1100, "—"],
            [88, 65, 720, "⚠ 接近OOM"],
            [92, 85, 480, "🔴 Java OOMKilled!"],
            [72, 45, 1720, "Pod重启后回落"],
            [75, 50, 1550, "—"],
            [78, 55, 1350, "—"],
        ],
        summary="🔴 15:05 OOM — Java RSS 6.4GB 触发 OOM Killer，Swap 0→85%")


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
        summary="🔴 15:04 conntrack 131072/131072 满 → 丢包暴增 → 15:05 OOM 后回落")


# ═══════════════════ Scenario: disk_io_and_dns ═══════════════════

def _cpu_disk_dns() -> str:
    return _fmt("CPU", "prod-us-east/worker-2", "15:00",
        cols=["CPU%", "Load", "iowait%", "Top Process"],
        rows=[
            [15, 1.5, 2, "java 12%"],
            [18, 1.8, 3, "java 15%"],
            [22, 2.5, 5, "java 18%"],
            [25, 4.9, 45, "kubelet D!"],  # ← iowait 暴增
            [25, 4.8, 45, "java D!"],
            [25, 4.9, 45, "logrotate R"],
            [25, 4.8, 45, "kubelet D!"],
            [25, 4.9, 45, "java D!"],
            [25, 4.5, 42, "kubelet D!"],
            [25, 4.3, 40, "java D!"],
        ],
        summary="🔴 15:03 iowait 2→45% — logrotate 触发磁盘 IO 飙升，kubelet/java D 状态")


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


# ═══════════════════ Data registries ═══════════════════

_CPU_DATA: dict[str, Callable[[], str]] = {
    "stress_cpu_and_tc_loss": _cpu_stress_tc,
    "conntrack_and_oom": _cpu_conntrack_oom,
    "disk_io_and_dns": _cpu_disk_dns,
    "conntrack_table_full": _cpu_conntrack_only,
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
}

_MEM_DATA: dict[str, Callable[[], str]] = {
    "stress_cpu_and_tc_loss": _mem_stress_tc,
    "conntrack_and_oom": _mem_conntrack_oom,
    "disk_io_and_dns": _mem_disk_dns,
    "etcd_quota_near_full": lambda: _fmt("Memory", "prod-us-east/master-1", "18:00",
        cols=["Mem%", "Swap%", "Available(GiB)", "OOM Events"],
        rows=[[50, 0, 3.0, "—"]] * 10,
        summary="✅ 内存正常 — 无 OOM 风险"),
}

_DISK_DATA: dict[str, Callable[[], str]] = {
    "stress_cpu_and_tc_loss": _disk_stress_tc,
    "conntrack_and_oom": _disk_conntrack_oom,
    "disk_io_and_dns": _disk_disk_dns,
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
}

_NET_DATA: dict[str, Callable[[], str]] = {
    "stress_cpu_and_tc_loss": _net_stress_tc,
    "conntrack_and_oom": _net_conntrack_oom,
    "disk_io_and_dns": _net_disk_dns,
    "conntrack_table_full": _net_conntrack_only,
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
}

_K8S_DATA: dict[str, Callable[[], str]] = {
    "conntrack_and_oom": lambda: _fmt("Kubernetes", "prod-us-east", "15:00",
        cols=["NotReady", "PodRestarts", "API Lat(ms)", "DNS Lat(ms)"],
        rows=[
            [0, 0, 5, 2], [0, 0, 5, 2],
            [0, 0, 6, 3],
            [0, 0, 5, 5],
            [0, 0, 5, 25],  # ← DNS 延迟开始升高（conntrack 满导致 UDP 丢包）
            [0, 8, 8, 120], # ← Pod 重启 + DNS 超时
            [0, 12, 10, 250],
            [0, 8, 5, 80],
            [0, 5, 5, 30],
            [0, 3, 5, 10],
        ],
        summary="🔴 15:05 Pod 重启 8 次（OOMKilled）+ DNS 延迟暴增（conntrack UDP 丢包）→ 15:06 OOM 后重启回落"),
    "conntrack_table_full": lambda: _fmt("Kubernetes", "prod-us-east", "15:00",
        cols=["NotReady", "PodRestarts", "API Lat(ms)", "DNS Lat(ms)"],
        rows=[
            [0, 0, 5, 2], [0, 0, 5, 2],
            [0, 0, 6, 2],
            [0, 0, 5, 2],
            [2, 0, 8, 25],  # ← 2 nodes NotReady + DNS latency spike
            [2, 0, 10, 120],
            [2, 5, 12, 250],
            [2, 30, 15, 500],
            [2, 30, 18, 800],
            [2, 30, 20, 1200],
        ],
        summary="🔴 15:04 worker-5/8 NotReady → Pod 驱逐开始 → DNS 延迟暴增"),
    "etcd_quota_near_full": lambda: _fmt("Kubernetes", "prod-us-east", "18:00",
        cols=["NotReady", "PodRestarts", "API Lat(ms)", "DNS Lat(ms)"],
        rows=[
            [0, 0, 8, 5],
            [0, 0, 12, 8],
            [0, 0, 45, 15],    # ← API Server 写延迟开始升高（etcd compaction）
            [0, 0, 150, 80],   # ← DNS 延迟开始升高（CoreDNS 查 etcd 变慢）
            [0, 0, 350, 250],  # ← API Server 写入排队
            [0, 2, 580, 500],  # ← Pod 重启开始（探针超时）
            [0, 5, 650, 800],  # ← DNS 严重超时
            [0, 8, 850, 1200], # ← 峰值: API p99=850ms, DNS p99=1200ms
            [0, 5, 720, 900],
            [0, 3, 480, 600],
        ],
        summary="🔴 18:02 API Server 延迟8→850ms（etcd slow）→ DNS 随即恶化 → 18:05 Pod 重启8次（探针超时）"),
}


# ═══════════════════ Formatting ═══════════════════

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
