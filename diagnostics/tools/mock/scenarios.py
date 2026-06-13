"""Diagnostic scenarios — shared state for all tool modules."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Scenario:
    id: str
    label: str
    description: str


AVAILABLE_SCENARIOS: dict[str, Scenario] = {
    "normal": Scenario(
        "normal", "系统正常",
        "所有指标在正常范围内，无异常",
    ),
    "high_cpu_iowait": Scenario(
        "high_cpu_iowait", "CPU高负载但使用率低(IO等待)",
        "load average 很高但 CPU 利用率低，大量进程处于 iowait/D 状态，磁盘 IO 是瓶颈",
    ),
    "memory_leak": Scenario(
        "memory_leak", "内存持续增长(疑似泄漏)",
        "一个 Java 进程内存使用持续增长，swap 开始被使用，系统出现 OOM 风险",
    ),
    "disk_bottleneck": Scenario(
        "disk_bottleneck", "磁盘IO瓶颈",
        "磁盘 util 接近 100%，await 和 svctm 升高，应用响应延迟增加",
    ),
    "network_congestion": Scenario(
        "network_congestion", "网络拥塞和重传",
        "网络接口带宽接近饱和，TCP 重传率升高，部分连接出现丢包",
    ),
    "container_crash": Scenario(
        "container_crash", "容器频繁重启(OOMKilled)",
        "Kubernetes 中一个 deployment 的 Pod 频繁 OOMKilled 重启",
    ),
    "mixed_io_memory": Scenario(
        "mixed_io_memory", "混合瓶颈(IO+内存)",
        "磁盘 IO 和内存同时出现压力",
    ),
    "gpu_oom": Scenario(
        "gpu_oom", "GPU显存耗尽(CUDA OOM)",
        "深度学习训练任务 CUDA OOM，GPU 显存耗尽，进程被 kill",
    ),
    # ═══ Cross-layer: host-level issues causing K8s failures ═══
    "kubelet_disk_io_starvation": Scenario(
        "kubelet_disk_io_starvation", "节点磁盘IO饱和→Kubelet心跳超时→Node NotReady",
        "主机磁盘IO阻塞导致kubelet lease续约超时，节点被标记NotReady，Pod被驱逐",
    ),
    "node_oom_kubelet_killed": Scenario(
        "node_oom_kubelet_killed", "系统OOM Kill Kubelet→Node NotReady",
        "主机内存不足触发OOM Killer终止kubelet，节点状态Unknown，所有Pod不可达",
    ),
    "conntrack_table_full": Scenario(
        "conntrack_table_full", "内核Conntrack表满→Pod网络/DNS故障",
        "主机conntrack表溢出导致Pod间网络丢包，CoreDNS UDP查询失败，应用出现连接超时",
    ),
    "cpu_throttle_probe_failure": Scenario(
        "cpu_throttle_probe_failure", "CPU节流→Kubelet探针超时→健康Pod被Kill",
        "主机CPU资源挤占导致kubelet探针超时，正常运行的Pod被误判为不健康而重启",
    ),
    # ═══ Kernel networking: ARP / MTU / gRPC layer ═══
    "arp_cache_full": Scenario(
        "arp_cache_full", "ARP缓存溢出→节点间通信中断",
        "集群规模增长导致ARP表超限，gc_thresh不足，新Pod无法解析邻居MAC，跨节点通信丢包",
    ),
    "mtu_mismatch": Scenario(
        "mtu_mismatch", "MTU配置不一致→大包丢包",
        "CNI overlay MTU与主机网卡MTU不匹配，ICMP fragmentation-needed被拦截，大数据包静默丢弃",
    ),
    # ═══ Kernel softirq / TCP queue ═══
    "softirq_starvation": Scenario(
        "softirq_starvation", "软中断饥饿→Ring Buffer溢→网卡丢包",
        "网卡中断绑定单CPU导致softirq处理瓶颈，ring buffer溢出，ethtool -S rx_missed_errors增长",
    ),
    "tcp_accept_overflow": Scenario(
        "tcp_accept_overflow", "TCP Accept队列溢出→连接被丢弃",
        "somaxconn设置过小或应用accept太慢，ss -lnt Recv-Q=Send-Q，客户端连接间歇超时",
    ),
    # ═══ Multi-layer cascading failure ═══
    "multi_layer_cascading": Scenario(
        "multi_layer_cascading", "多层叠加故障(etcd磁盘+节点OOM+应用OOM+DNS)",
        "etcd磁盘IO饱和→API Server变慢；worker节点OOM杀kubelet→NotReady；Pod limit不足OOMKilled；CoreDNS单副本DNS间歇超时",
    ),
}

_active_scenario: str = "normal"


def set_scenario(scenario_id: str) -> None:
    global _active_scenario
    if scenario_id not in AVAILABLE_SCENARIOS:
        raise ValueError(f"Unknown scenario '{scenario_id}'")
    _active_scenario = scenario_id


def get_active_scenario() -> str:
    return _active_scenario
