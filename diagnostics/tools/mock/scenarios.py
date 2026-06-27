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
    # ═══ Multi-root-cause (independent simultaneous failures) ═══
    "stress_cpu_and_tc_loss": Scenario(
        "stress_cpu_and_tc_loss", "双根因：stress进程CPU高 + tc策略丢包",
        "stress-ng占用全部CPU导致服务响应超时；同时tc qdisc netem丢包5%导致API调用间歇失败。两个故障独立但症状叠加",
    ),
    "conntrack_and_oom": Scenario(
        "conntrack_and_oom", "双根因：conntrack表满 + Java内存泄漏OOM",
        "nf_conntrack表满导致Pod网络丢包和DNS超时；同时Java应用bytedance堆外内存泄漏触发OOMKilled。网络症状和OOM症状同时出现",
    ),
    "disk_io_and_dns": Scenario(
        "disk_io_and_dns", "双根因：磁盘IO饱和 + CoreDNS配置错误",
        "日志rotate引起磁盘IO飙升导致服务写日志阻塞；同时CoreDNS forward配置指向已下线的上游DNS导致解析超时。延迟和DNS失败双重症状",
    ),
    # ═══ Additional scenarios: K8s ═══
    "coredns_cache_poison": Scenario(
        "coredns_cache_poison", "CoreDNS缓存污染→服务路由到错误IP",
        "CoreDNS缓存被错误DNS响应污染，应用解析到错误IP地址，部分请求路由到旧端点失败",
    ),
    "etcd_quorum_loss": Scenario(
        "etcd_quorum_loss", "etcd多数派丢失→API Server只读",
        "etcd集群中2/3节点磁盘延迟导致leader选举失败，API Server降级为只读模式",
    ),
    "image_pull_backoff": Scenario(
        "image_pull_backoff", "容器镜像拉取失败→Pod启动失败",
        "Docker Hub速率限制触发ImagePullBackOff，新Pod无法启动，已有Pod正常运行",
    ),
    "disk_full_var_log": Scenario(
        "disk_full_var_log", "磁盘空间耗尽→容器运行时崩溃",
        "/var/log 分区被日志写满导致容器运行时无法创建新容器，现有容器逐步失联",
    ),
    "oom_score_misconfig": Scenario(
        "oom_score_misconfig", "OOM Score配置错误→误杀kubelet",
        "kubelet的oom_score_adj配置不当在内存压力下被OOM Killer终止，节点NotReady",
    ),
    "fs_inode_exhaustion": Scenario(
        "fs_inode_exhaustion", "Inode耗尽→写操作失败",
        "小文件过多导致inode耗尽，df显示磁盘有空间但touch创建文件失败，服务日志写入异常",
    ),
    # ═══ Additional scenarios: Host ═══
    "systemd_limit_nofile": Scenario(
        "systemd_limit_nofile", "systemd文件描述符限制→连接失败",
        "systemd隐式地将LimitNOFILE从高值截断到65536，高并发服务连接池耗尽后新连接失败",
    ),
    "ebpf_probe_overhead": Scenario(
        "ebpf_probe_overhead", "eBPF监控开销→CPU飙升",
        "eBPF探针在特定内核路径触发过量事件导致CPU sys%飙升，应用响应延迟增加",
    ),
    "swap_thrashing": Scenario(
        "swap_thrashing", "Swap抖动→系统全面降速",
        "内存压力导致频繁swap换入换出，iowait高同时mem利用率高，所有进程响应严重延迟",
    ),
    # ═══ Additional scenarios: Dual root cause ═══
    "memory_leak_and_disk_full": Scenario(
        "memory_leak_and_disk_full", "双根因：内存泄漏 + 磁盘空间耗尽",
        "Java进程内存泄漏导致频繁GC，同时日志未rotate写满磁盘，应用OOMKilled且无法写日志",
    ),
    "dns_and_etcd": Scenario(
        "dns_and_etcd", "双根因：CoreDNS CrashLoop + etcd leader频繁切换",
        "CoreDNS因资源配置不足CrashLoopBackOff导致DNS解析失败，etcd leader因磁盘抖动频繁切换导致API Server慢",
    ),
    # ═══ etcd quota near full — control plane bottleneck ═══
    "etcd_quota_near_full": Scenario(
        "etcd_quota_near_full", "etcd配额接近上限→API写延迟→应用故障",
        "etcd数据库接近8GB配额上限，compaction无法释放足够空间，API Server写入延迟p99=850ms，"
        "导致api-gateway探针超时CrashLoopBackOff，CoreDNS查询service时也因etcd超时而间歇DNS失败，"
        "但所有节点和Pod资源正常，非OOM/CPU/网络问题",
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
