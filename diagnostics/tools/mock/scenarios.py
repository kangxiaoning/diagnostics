"""Diagnostic scenarios — declarative definitions with routing metadata.

Each scenario is self-contained: id, label, description, entity_type,
a canonical user_message for testing, and keyword tuples for auto-routing.

Adding a new scenario:
  1. Add an entry to AVAILABLE_SCENARIOS below.
  2. Add mock data in data.py / argus_data.py / kubernetes.py / hosts.py.
  3. Run scripts/verify_scenario_coverage.py to confirm completeness.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Scenario:
    id: str
    label: str
    description: str
    entity_type: str = ""  # "hosts" | "kubernetes"
    user_message: str = ""  # canonical test input
    keywords: list[tuple[str, ...]] = field(default_factory=list)  # routing rules (AND within tuple)


# ═══════════════════════════════════════════════════════════════════
# All 31 scenarios — routing priority is automatic:
#   tuples with MORE keywords are checked first (more specific wins).
#   Ties are broken by insertion order.
# ═══════════════════════════════════════════════════════════════════

AVAILABLE_SCENARIOS: dict[str, Scenario] = {
    # ── Dual root cause (most specific — 3-4 keyword tuples) ──
    "stress_cpu_and_tc_loss": Scenario(
        "stress_cpu_and_tc_loss",
        "双根因：stress进程CPU高 + tc策略丢包",
        "stress-ng占用全部CPU导致服务响应超时；同时tc qdisc netem丢包5%导致API调用间歇失败",
        entity_type="host",
        user_message="prod-web-01 上 api-gateway 从 15:03 开始间歇 504，CPU 打满，业务 QPS 没涨。",
        keywords=[
            ("504", "cpu"),
            ("prod-web-01", "504"),
        ],
    ),
    "conntrack_and_oom": Scenario(
        "conntrack_and_oom",
        "双根因：conntrack表满 + Java内存泄漏OOM",
        "nf_conntrack表满导致Pod网络丢包和DNS超时；同时Java应用堆外内存泄漏触发OOMKilled",
        entity_type="kubernetes",
        user_message="prod-us-east 集群 api-gateway OOMKilled，DNS 解析间歇超时，java 进程内存持续增长。",
        keywords=[
            ("api-gateway", "oom"),
            ("api-gateway", "dns", "内存"),
        ],
    ),
    "etcd_quota_near_full": Scenario(
        "etcd_quota_near_full",
        "etcd配额接近上限→API写延迟→应用故障",
        "etcd数据库接近8GB配额上限，API Server写入延迟p99=850ms，api-gateway探针超时",
        entity_type="kubernetes",
        user_message="prod-us-east 集群 api-gateway 大量 5xx，一半 Pod 反复重启，DNS 偶发超时，半年没发布过。",
        keywords=[
            ("api-gateway", "5xx", "重启", "dns"),
            ("api-gateway", "5xx", "重启", "半年"),
            ("etcd quota",),
            ("etcd", "接近上限"),
        ],
    ),
    "disk_io_and_dns": Scenario(
        "disk_io_and_dns",
        "双根因：磁盘IO饱和 + CoreDNS配置错误",
        "日志rotate引起磁盘IO飙升；同时CoreDNS forward配置指向已下线的上游DNS",
        entity_type="kubernetes",
        user_message="prod-us-east 集群 backend-svc P99 延迟 200ms→5s，外部域名解析失败但内部 DNS 正常，磁盘 IO 偏高。",
        keywords=[
            ("backend-svc", "dns"),
            ("backend-svc", "延迟", "磁盘"),
            ("外部域名", "解析"),
        ],
    ),
    "memory_leak_and_disk_full": Scenario(
        "memory_leak_and_disk_full",
        "双根因：内存泄漏 + 磁盘空间耗尽",
        "Java进程内存泄漏导致频繁GC，同时日志未rotate写满磁盘",
        entity_type="kubernetes",
        user_message="Java 应用频繁 Full GC 后 OOMKilled，同时磁盘被日志写满，服务无法写日志也无法创建新容器。",
        keywords=[
            ("内存泄漏", "磁盘"),
            ("gc", "频繁", "磁盘", "满"),
            ("oom", "日志", "写满"),
        ],
    ),
    "dns_and_etcd": Scenario(
        "dns_and_etcd",
        "双根因：CoreDNS CrashLoop + etcd leader频繁切换",
        "CoreDNS因资源配置不足CrashLoopBackOff，etcd leader因磁盘抖动频繁切换",
        entity_type="kubernetes",
        user_message="CoreDNS 频繁 CrashLoopBackOff 导致 DNS 解析失败，同时 etcd leader 频繁切换导致 API Server 慢。",
        keywords=[
            ("coredns", "crashloop", "etcd"),
            ("dns 失败", "api server 慢"),
            ("coredns", "etcd", "切换"),
        ],
    ),
    # ── Cascading ──
    "multi_layer_cascading": Scenario(
        "multi_layer_cascading",
        "多层叠加故障(etcd磁盘+节点OOM+应用OOM+DNS)",
        "etcd磁盘IO饱和→API Server变慢；worker节点OOM杀kubelet→NotReady；Pod OOMKilled；CoreDNS单副本",
        entity_type="kubernetes",
        user_message="集群表现很乱：kubectl 慢十几秒，worker 节点 Pod 全部 Unknown，api-gateway 反复重启，DNS 间歇超时。像是多个独立问题叠加。",
        keywords=[
            ("etcd", "oom"),
            ("etcd", "worker-3"),
            ("master-1", "worker-3"),
            ("表现很乱", "独立问题"),
        ],
    ),
    # ── K8s single root cause (specific before general) ──
    "kubelet_disk_io_starvation": Scenario(
        "kubelet_disk_io_starvation",
        "节点磁盘IO饱和→Kubelet心跳超时→Node NotReady",
        "主机磁盘IO阻塞导致kubelet lease续约超时，节点被标记NotReady，Pod被驱逐",
        entity_type="kubernetes",
        user_message="一台 worker 节点突然 NotReady，上面所有 Pod 被驱逐。监控看到节点磁盘 IO 之前很高。",
        keywords=[
            ("kubelet", "disk io"),
            ("node notready", "磁盘"),
            ("pod 被驱逐", "磁盘"),
        ],
    ),
    "conntrack_table_full": Scenario(
        "conntrack_table_full",
        "内核Conntrack表满→Pod网络/DNS故障",
        "主机conntrack表溢出导致Pod间网络丢包，CoreDNS UDP查询失败",
        entity_type="kubernetes",
        user_message="prod-us-east 集群 worker-5/8 先后 NotReady，30+ Pod 被驱逐，CoreDNS 间歇超时。",
        keywords=[
            ("conntrack", "notready"),
            ("worker-5", "worker-8"),
            ("notready", "pod 被驱逐"),
            ("节点", "notready", "coredns"),
        ],
    ),
    "oom_score_misconfig": Scenario(
        "oom_score_misconfig",
        "OOM Score配置错误→误杀kubelet",
        "kubelet的oom_score_adj配置不当在内存压力下被OOM Killer终止",
        entity_type="kubernetes",
        user_message="内存压力下 kubelet 进程被 OOM Killer 终止，节点 NotReady，应用 Pod 反而幸存。oom_score 配置不当。",
        keywords=[
            ("oom_score", "配置"),
            ("oom score", "配置"),
            ("oom_score",),
            ("oom score",),
            ("误杀", "kubelet"),
        ],
    ),
    "node_oom_kubelet_killed": Scenario(
        "node_oom_kubelet_killed",
        "系统OOM Kill Kubelet→Node NotReady",
        "主机内存不足触发OOM Killer终止kubelet，节点状态Unknown",
        entity_type="kubernetes",
        user_message="集群里有个 worker 节点状态变成 Unknown，所有 Pod 不可达。主机内存之前一直很高。",
        keywords=[
            ("kubelet", "oom"),
            ("oom kill", "kubelet"),
            ("节点", "unknown", "内存"),
            ("节点", "unknown", "pod 不可达"),
        ],
    ),
    "cpu_throttle_probe_failure": Scenario(
        "cpu_throttle_probe_failure",
        "CPU节流→Kubelet探针超时→健康Pod被Kill",
        "主机CPU资源挤占导致kubelet探针超时，正常运行的Pod被误判为不健康而重启",
        entity_type="kubernetes",
        user_message="有批处理任务在跑的时候，几个一直正常的 Pod 突然重启了，日志显示探针超时，怀疑 CPU 被抢占。",
        keywords=[
            ("cpu throttl", "探针"),
            ("健康 pod", "重启"),
            ("探针超时", "cpu"),
        ],
    ),
    "arp_cache_full": Scenario(
        "arp_cache_full",
        "ARP缓存溢出→节点间通信中断",
        "集群规模增长导致ARP表超限，新Pod无法解析邻居MAC",
        entity_type="kubernetes",
        user_message="集群规模扩大后，新部署的 Pod 无法和跨节点服务通信，同节点内通信正常。怀疑 ARP 表已满导致跨节点通信中断。",
        keywords=[
            ("arp", "溢出"),
            ("arp", "表满"),
            ("跨节点通信", "中断"),
        ],
    ),
    "mtu_mismatch": Scenario(
        "mtu_mismatch",
        "MTU配置不一致→大包丢包",
        "CNI overlay MTU与主机网卡MTU不匹配，大包被静默丢弃",
        entity_type="kubernetes",
        user_message="应用上传大文件总是失败，小请求正常。ping 小包通大包不通。",
        keywords=[
            ("mtu", "不匹配"),
            ("大包", "丢包"),
            ("ping 小包通", "大包不通"),
        ],
    ),
    "container_crash": Scenario(
        "container_crash",
        "容器频繁重启(OOMKilled)",
        "Kubernetes 中一个 deployment 的 Pod 频繁 OOMKilled 重启",
        entity_type="kubernetes",
        user_message="一个 deployment 的 Pod 反复 OOMKilled 重启，limit 设了 512Mi，高峰期内存不够。",
        keywords=[
            ("容器", "频繁重启"),
            ("oomkilled", "limit"),
            ("crashloopbackoff",),
        ],
    ),
    # ── Host single root cause (specific before general) ──
    "swap_thrashing": Scenario(
        "swap_thrashing",
        "Swap抖动→系统全面降速",
        "内存压力导致频繁swap换入换出，iowait高同时mem利用率高",
        entity_type="host",
        user_message="内存压力导致频繁 swap，iowait 和 mem 使用率同时高，所有进程响应严重延迟。",
        keywords=[
            ("swap", "抖动"),
            ("swap", "频繁"),
            ("换入换出",),
        ],
    ),
    "high_cpu_iowait": Scenario(
        "high_cpu_iowait",
        "CPU高负载但使用率低(IO等待)",
        "load average 很高但 CPU 利用率低，大量进程处于 iowait/D 状态",
        entity_type="host",
        user_message="服务器负载很高但 CPU 使用率不高，top 看到很多进程 D 状态，iowait 很高。",
        keywords=[
            ("iowait", "高"),
            ("load avg", "cpu util 低"),
            ("负载高", "cpu 低"),
        ],
    ),
    "memory_leak": Scenario(
        "memory_leak",
        "内存持续增长(疑似泄漏)",
        "一个 Java 进程内存使用持续增长，swap 开始被使用，系统出现 OOM 风险",
        entity_type="host",
        user_message="服务器内存使用持续增长，swap 被大量使用，Java 进程 RSS 从 512MB 涨到 6GB。",
        keywords=[
            ("内存泄漏",),
            ("rss", "持续增长"),
            ("swap", "增加"),
            ("swap", "被大量使用"),
            ("rss", "从", "涨到"),
        ],
    ),
    "disk_bottleneck": Scenario(
        "disk_bottleneck",
        "磁盘IO瓶颈",
        "磁盘 util 接近 100%，await 和 svctm 升高",
        entity_type="host",
        user_message="服务器磁盘 IO 接近 100%，iostat 看到 await 很高，应用响应延迟明显增加。",
        keywords=[
            ("磁盘", "慢"),
            ("disk io", "瓶颈"),
            ("await", "高"),
        ],
    ),
    "network_congestion": Scenario(
        "network_congestion",
        "网络拥塞和重传",
        "网络接口带宽接近饱和，TCP 重传率升高",
        entity_type="host",
        user_message="有台机器网络带宽接近饱和，TCP 重传率攀升，部分连接出现丢包。",
        keywords=[
            ("网络拥塞",),
            ("重传", "高"),
            ("带宽", "饱和"),
        ],
    ),
    "mixed_io_memory": Scenario(
        "mixed_io_memory",
        "混合瓶颈(IO+内存)",
        "磁盘 IO 和内存同时出现压力",
        entity_type="host",
        user_message="服务器磁盘 IO 和内存同时出现压力，性能下降明显。",
        keywords=[
            ("io", "内存"),
            ("混合瓶颈",),
        ],
    ),
    "softirq_starvation": Scenario(
        "softirq_starvation",
        "软中断饥饿→Ring Buffer溢→网卡丢包",
        "网卡中断绑定单CPU导致softirq处理瓶颈，ring buffer溢出",
        entity_type="host",
        user_message="有台机器网络丢包严重，网卡没有硬件错误。top 看到 ksoftirqd CPU 异常高。",
        keywords=[
            ("软中断", "cpu"),
            ("ksoftirqd", "高"),
            ("ring buffer", "溢出"),
        ],
    ),
    "tcp_accept_overflow": Scenario(
        "tcp_accept_overflow",
        "TCP Accept队列溢出→连接被丢弃",
        "somaxconn设置过小或应用accept太慢，客户端连接间歇超时",
        entity_type="host",
        user_message="服务间歇性地报连接超时，但不是一直超时。接口无丢包，TCP 重传也正常。怀疑 TCP 间歇超时。",
        keywords=[
            ("accept 队列", "满"),
            ("tcp", "间歇超时"),
            ("somaxconn",),
        ],
    ),
    # ── GPU ──
    "gpu_oom": Scenario(
        "gpu_oom",
        "GPU显存耗尽(CUDA OOM)",
        "深度学习训练任务 CUDA OOM，GPU 显存耗尽",
        entity_type="host",
        user_message="深度学习训练报 CUDA OOM，GPU 显存占满，进程被 kill。",
        keywords=[
            ("gpu", "oom"),
            ("cuda", "out of memory"),
            ("显存", "耗尽"),
        ],
    ),
    # ── Additional K8s scenarios ──
    "coredns_cache_poison": Scenario(
        "coredns_cache_poison",
        "CoreDNS缓存污染→服务路由到错误IP",
        "CoreDNS缓存被错误DNS响应污染，应用解析到错误IP地址",
        entity_type="kubernetes",
        user_message="部分服务间歇性地连接到错误的 IP 地址，重启 Pod 后暂时恢复，过一会又出现。DNS 解析到错误 IP。",
        keywords=[
            ("coredns", "缓存"),
            ("dns", "错误 ip"),
            ("解析到错误",),
        ],
    ),
    "etcd_quorum_loss": Scenario(
        "etcd_quorum_loss",
        "etcd多数派丢失→API Server只读",
        "etcd集群中2/3节点磁盘延迟导致leader选举失败",
        entity_type="kubernetes",
        user_message="etcd 多数派丢失，API Server 变成只读模式，kubectl 无法执行创建/更新操作，get 正常。",
        keywords=[
            ("etcd", "只读"),
            ("etcd", "多数派"),
            ("api server", "降级"),
        ],
    ),
    "image_pull_backoff": Scenario(
        "image_pull_backoff",
        "容器镜像拉取失败→Pod启动失败",
        "Docker Hub速率限制触发ImagePullBackOff",
        entity_type="kubernetes",
        user_message="新部署的 Pod 全部卡在 ImagePullBackOff，但已有 Pod 正常运行。最近没有镜像变更。",
        keywords=[
            ("imagepullbackoff",),
            ("镜像拉取", "失败"),
            ("registry", "限速"),
        ],
    ),
    "disk_full_var_log": Scenario(
        "disk_full_var_log",
        "磁盘空间耗尽→容器运行时崩溃",
        "/var/log 分区被日志写满导致容器运行时无法创建新容器",
        entity_type="host",
        user_message="/var/log 分区被写满，容器运行时无法创建新容器，部分 Pod 状态异常。",
        keywords=[
            ("磁盘", "写满"),
            ("var/log", "满"),
            ("容器运行时", "崩溃"),
        ],
    ),
    "fs_inode_exhaustion": Scenario(
        "fs_inode_exhaustion",
        "Inode耗尽→写操作失败",
        "小文件过多导致inode耗尽，df显示磁盘有空间但touch创建文件失败",
        entity_type="host",
        user_message="df 显示磁盘还有 50% 空间，但任何写操作都报 'No space left on device'。可能是 inode 耗尽了。",
        keywords=[
            ("inode", "耗尽"),
            ("磁盘有空间", "创建文件失败"),
            ("小文件过多",),
        ],
    ),
    # ── Additional Host scenarios ──
    "systemd_limit_nofile": Scenario(
        "systemd_limit_nofile",
        "systemd文件描述符限制→连接失败",
        "systemd隐式地将LimitNOFILE从高值截断到65536",
        entity_type="host",
        user_message="高并发服务在连接数超过 65K 后突然出现大量 'too many open files' 错误。systemd 的文件描述符限制被截断。",
        keywords=[
            ("systemd", "nofile"),
            ("limitnofile",),
            ("文件描述符", "截断"),
        ],
    ),
    "ebpf_probe_overhead": Scenario(
        "ebpf_probe_overhead",
        "eBPF监控开销→CPU飙升",
        "eBPF探针在特定内核路径触发过量事件导致CPU sys%飙升",
        entity_type="host",
        user_message="部署 eBPF 监控工具后，服务器 CPU sys% 从 10% 飙升到 60%，应用响应延迟增加。分析是 eBPF 探针开销过大。",
        keywords=[
            ("ebpf", "高"),
            ("ebpf", "开销"),
            ("探针", "过量"),
        ],
    ),
    # ── Normal (fallback) ──
    "normal": Scenario(
        "normal",
        "系统正常",
        "所有指标在正常范围内，无异常",
        entity_type="hosts",
        user_message="系统一切正常。",
        keywords=[],
    ),
}


# ═══════════════════════════════════════════════════════════════════
# State management
# ═══════════════════════════════════════════════════════════════════

_active_scenario: str = "normal"


def set_scenario(scenario_id: str) -> None:
    global _active_scenario
    if scenario_id not in AVAILABLE_SCENARIOS:
        raise ValueError(f"Unknown scenario '{scenario_id}'")
    _active_scenario = scenario_id


def get_active_scenario() -> str:
    return _active_scenario


# ═══════════════════════════════════════════════════════════════════
# Declarative routing — replaces app.py's hand-coded keyword chains
# ═══════════════════════════════════════════════════════════════════

def match_scenario(message: str) -> str | None:
    """Match user message to a scenario using declarative keyword rules.

    Returns the scenario ID, or None if no match.
    Rules are auto-sorted by keyword count descending (most specific first).
    """
    msg = message.lower()

    # Build flat list: (keyword_count, scenario_id, keywords_tuple)
    candidates: list[tuple[int, str, tuple[str, ...]]] = []
    for sid, sc in AVAILABLE_SCENARIOS.items():
        if sid == "normal":
            continue  # skip fallback
        for kw in sc.keywords:
            candidates.append((len(kw), sid, kw))

    # Sort: more keywords = higher priority (more specific match first)
    candidates.sort(key=lambda x: -x[0])

    for _count, sid, kw in candidates:
        if all(k in msg for k in kw):
            return sid

    return None
