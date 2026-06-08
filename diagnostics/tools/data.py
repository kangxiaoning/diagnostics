"""Mock diagnostic data — organized by scenario for consistent cross-tool results."""

from __future__ import annotations


def _load_avg(scenario: str) -> str:
    loads = {
        "normal": "0.52, 0.48, 0.55",
        "high_cpu_iowait": "5.23, 4.85, 3.92",
        "memory_leak": "2.51, 2.78, 3.12",
        "disk_bottleneck": "4.52, 5.15, 6.08",
        "network_congestion": "1.48, 1.82, 2.05",
        "container_crash": "3.18, 2.75, 2.52",
        "mixed_io_memory": "3.85, 4.52, 5.18",
        "gpu_oom": "1.50, 1.80, 2.10",
        "kubelet_disk_io_starvation": "6.38, 4.55, 3.20",
        "node_oom_kubelet_killed": "8.22, 7.16, 4.85",
        "conntrack_table_full": "2.85, 2.60, 2.40",
        "cpu_throttle_probe_failure": "5.12, 4.88, 4.50",
        "arp_cache_full": "1.85, 1.70, 1.55",
        "mtu_mismatch": "1.20, 1.15, 1.10",
        "softirq_starvation": "2.85, 2.60, 2.35",
        "tcp_accept_overflow": "1.50, 1.40, 1.35",
        "multi_layer_cascading": "5.22, 4.35, 3.80",
    }
    return loads.get(scenario, "0.5, 0.6, 0.7")


# ════════════════ CPU ════════════════

def cpu_data(scenario: str) -> str:
    base = """Architecture: x86_64  CPU(s): 8  Model: Intel(R) Xeon(R) Platinum 8375C CPU @ 2.90GHz
NUMA node0 CPU(s): 0-3  NUMA node1 CPU(s): 4-7"""
    if scenario == "high_cpu_iowait":
        return base + """
top: PID 1234 app D 2.7% 10.7% java / PID 2345 app D 1.8% 6.5% mysqld
vmstat 1 3: r=10 b=15 wa=58 us=8 sy=5 id=5
CPU util: ~15% user, ~55% iowait, load average: 5.2/4.8/3.9"""
    if scenario == "memory_leak":
        return base + """
top: PID 5678 app S 95.3% 78.5% java / PID 2345 app S 3.1% 6.5% mysqld
vmstat 1 3: r=2 b=2 wa=4 us=80 sy=6 id=8
CPU util: ~85% user, ~6% sys, load average: 2.5/2.8/3.1"""
    if scenario == "disk_bottleneck":
        return base + """
vmstat 1 3: r=2 b=16 wa=52 us=14 sy=9 id=0
CPU util: ~15% user, ~10% sys, ~50% iowait, load average: 4.5/5.2/6.1"""
    if scenario == "network_congestion":
        return base + """
vmstat 1 3: r=3 b=0 wa=5 us=25 sy=35 id=35
CPU util: ~25% user, ~35% sys (high softirq), load average: 1.5/1.8/2.0"""
    if scenario == "container_crash":
        return base + """
top: PID 1234 root S 55.0% 13.1% containerd / PID 2345 root S 25.0% 6.5% kubelet
CPU util: ~60% user, ~22% sys, load average: 3.2/2.8/2.5"""
    if scenario == "gpu_oom":
        return base + """
top: PID 1234 ml S 95.0% 5.2% python3 train.py / PID 2345 ml R 2.5% 1.0% dataloader
CPU util: ~55% user, ~5% sys, load average: 1.5/1.8/2.1"""
    if scenario == "kubelet_disk_io_starvation":
        return base + """
vmstat 1 3: r=4 b=18 wa=62 us=11 sy=8 id=0
CPU util: ~11% user, ~8% sys, ~62% iowait, load average: 6.4/4.6/3.2
说明: CPU低但iowait极高—磁盘IO阻塞导致CPU空等待"""
    if scenario == "node_oom_kubelet_killed":
        return base + """
vmstat 1 3: r=2 b=6 wa=12 us=38 sy=35 id=0
CPU util: ~38% user, ~35% sys (kubelet/containerd high), load average: 8.2/7.2/4.9
说明: sys%高因OOM频繁触发页面回收，系统守护进程CPU被抢占"""
    if scenario == "conntrack_table_full":
        return base + """
vmstat 1 3: r=3 b=0 wa=2 us=18 sy=45 id=32
CPU util: ~18% user, ~45% sys (high softirq from networking), load average: 2.9/2.6/2.4
说明: sys%高因softirq处理网络中断和conntrack操作"""
    if scenario == "cpu_throttle_probe_failure":
        return base + """
vmstat 1 3: r=8 b=0 wa=5 us=68 sy=18 id=0
CPU util: ~68% user, ~18% sys, ~5% iowait, load average: 5.1/4.9/4.5
说明: user%高但CPU核数有限—批处理任务抢占kubelet CPU时间片"""
    if scenario == "mixed_io_memory":
        return base + """
top: PID 5678 app D 45.2% 45.8% mysql / PID 1234 app S 18.5% 26.2% java
vmstat 1 3: r=2 b=7 wa=32 us=30 sy=8 id=20
CPU util: ~32% user, ~8% sys, ~30% iowait, load average: 3.8/4.5/5.2"""
    if scenario == "softirq_starvation":
        return base + """
vmstat 1 3: r=2 b=0 wa=2 us=12 sy=55 id=25
CPU util: ~12% user, ~55% sys (高sys%—软中断ksoftirqd占满CPU0!)
/proc/softirqs: CPU0 NET_RX=48523000 CPU1 NET_RX=152 (严重不均衡!)
ksoftirqd/0 CPU%: 92% (CPU0被软中断占满)
说明: NIC中断全部绑定CPU0→softirq单核瓶颈→ring buffer溢出"""
    if scenario == "tcp_accept_overflow":
        return base + """
vmstat 1 3: r=2 b=0 wa=3 us=35 sy=12 id=48
CPU util: ~35% user, ~12% sys, load average: 1.5/1.4/1.4
说明: CPU正常，无资源瓶颈—问题在应用层TCP accept队列"""
    if scenario == "multi_layer_cascading":
        return base + """
vmstat 1 3 (master-1): r=3 b=12 wa=58 us=8 sy=12 id=0 (iowait极高—etcd磁盘IO阻塞!)
vmstat 1 3 (worker-3): r=2 b=6 wa=15 us=38 sy=35 id=0 (sys%高因OOM频繁触发页面回收)
top: PID 5678 etcd D 3.5% 8.2% etcd (D状态! IO阻塞) / PID 1234 root S 2.0% 5.0% kubelet
说明: master-1上etcd进程D状态+iowait58%→etcd磁盘IO是API Server慢的根因
      worker-3上sys%35%+OOM页面回收→主机内存耗尽迹象"""
    return base + """
vmstat 1 3: r=1 b=0 wa=2 us=10 sy=5 id=83
CPU util: ~10% user, ~5% sys, ~2% iowait, load average: 0.5/0.6/0.7"""


# ════════════════ Memory ════════════════

def memory_data(scenario: str) -> str:
    base = """Mem: 7.6Gi total, 2.1Gi used, 3.5Gi free, 2.0Gi buff/cache, 5.0Gi available
Swap: 2.0Gi total, 0B used, 2.0Gi free"""
    if scenario == "memory_leak":
        return """Mem: 7.6Gi total, 7.0Gi used, 120Mi free, 512Mi buff/cache, 280Mi available
Swap: 2.0Gi total, 1.5Gi used, 512Mi free
/proc/meminfo: AnonPages=6815744kB (异常高, 疑似内存泄漏)"""
    if scenario == "mixed_io_memory":
        return """Mem: 7.6Gi total, 5.8Gi used, 450Mi free, 1.3Gi buff/cache, 1.1Gi available
Swap: 2.0Gi total, 512Mi used, 1.5Gi free
/proc/meminfo: MemAvailable=1179648kB (偏低), Dirty=393216kB (脏页较多)"""
    if scenario == "container_crash":
        return """Mem: 7.6Gi total, 6.2Gi used, 280Mi free, 1.1Gi buff/cache, 900Mi available
Swap: 2.0Gi total, 256Mi used, 1.7Gi free"""
    if scenario == "gpu_oom":
        return """Mem: 15Gi total, 8.5Gi used, 4.0Gi free, 2.5Gi buff/cache, 5.5Gi available
Swap: 4.0Gi total, 0B used, 4.0Gi free"""
    if scenario == "node_oom_kubelet_killed":
        return """Mem: 7.6Gi total, 7.3Gi used, 80Mi free, 220Mi buff/cache, 190Mi available (极度不足!)
Swap: 2.0Gi total, 1.85Gi used, 150Mi free (swap近乎耗尽)
/proc/meminfo: Committed_AS=12850176kB (overcommit严重)
dmesg: oom-killer invoked, killed process kubelet (PID 3210)
dmesg: java invoked oom-killer, process consumed RSS 5.8GB"""
    if scenario == "multi_layer_cascading":
        return """Mem: 7.6Gi total, 6.8Gi used, 180Mi free, 350Mi buff/cache, 320Mi available (偏低)
Swap: 2.0Gi total, 1.2Gi used, 820Mi free
/proc/meminfo (worker-3): Committed_AS=13500224kB (overcommit严重!)
dmesg (worker-3): oom-killer invoked, killed process kubelet (PID 3210) total-vm:4194304kB anon-rss:245760kB
dmesg (worker-3): java (PID 8765) invoked oom-killer, process consumed RSS 6.2GB (主机Java进程!)
dmesg (worker-3): Out of memory: Killed process 3210 (kubelet)
说明: worker-3主机Java进程(RSS=6.2GB)触发系统OOM→OOM Killer误杀kubelet→节点失联
      —注意区别于节点MemoryPressure(此处kubelet被杀后条件变为Unknown而非True)"""
    return base


# ════════════════ Disk ════════════════

def disk_data(scenario: str) -> str:
    base = """iostat: sda r/s=25 w/s=45 await=1.5ms svctm=0.8ms %util=5.6
df: /dev/sda1 50G 18G 30G 38% /  /dev/sdb1 100G 45G 55G 45% /data"""
    if scenario in ("high_cpu_iowait", "disk_bottleneck"):
        return """iostat: sda r/s=120 w/s=250 await=45.2ms svctm=18.5ms %util=98.5 (饱和!)
sdb r/s=180 w/s=320 await=52.3ms svctm=20.1ms %util=99.2 (饱和!)
avg-cpu: iowait=55.30%
df: /dev/sda1 50G 38G 10G 78% /  /dev/sdb1 100G 85G 15G 85% /data"""
    if scenario == "gpu_oom":
        return base + """
df: /dev/nvme0n1 200G 180G 10G 95% /data (训练数据集所在盘接近满)"""
    if scenario == "kubelet_disk_io_starvation":
        return """iostat: sda r/s=310 w/s=480 await=88.5ms svctm=35.2ms %util=99.8 (严重饱和!)
sdb r/s=220 w/s=390 await=72.1ms svctm=28.6ms %util=98.9 (严重饱和!)
avg-cpu: iowait=62.10%
df: /dev/sda1 50G 35G 13G 73% /  /dev/sdb1 100G 82G 18G 82% /data
说明: iostat util接近100%、await极高—磁盘IO严重阻塞，可能影响kubelet lease写入"""
    if scenario == "node_oom_kubelet_killed":
        return """iostat: sda r/s=85 w/s=180 await=28.5ms svctm=8.2ms %util=62.0
avg-cpu: iowait=12.30%
df: /dev/sda1 50G 42G 6G 88% /  /dev/sdb1 100G 90G 10G 90% /data
说明: 磁盘IO中等偏高（swap换入换出导致），但非主要瓶颈"""
    if scenario == "multi_layer_cascading":
        return """iostat (master-1 - etcd节点): sda r/s=280 w/s=520 await=185.5ms svctm=42.3ms %util=99.8 (严重饱和!)
sdb r/s=45 w/s=80 await=12.5ms svctm=3.2ms %util=15.0
avg-cpu (master-1): iowait=58.20%
df: /dev/sda1 50G 44G 4G 92% /  /dev/sdb1 100G 60G 40G 60% /var/lib/etcd (etcd数据目录所在盘!)
说明: master-1上sda(etcd数据目录所在盘)util 99.8%+await 185ms→etcd fsync严重延迟
      iostat (worker-3): sda r/s=65 w/s=150 await=22.5ms svctm=6.8ms %util=48.0
df (worker-3): /dev/sda1 50G 38G 10G 78% /"""
    return base


# ════════════════ Network ════════════════

def network_data(scenario: str) -> str:
    base = """ss: ESTAB 3 connections on :8080, :3306. No queue backlog. No errors.
eth0: RX 8.3GiB / TX 11.6GiB, errors/dropped=0"""
    if scenario == "network_congestion":
        return """ss: ESTAB 2 on :8080 with Send-Q 524288/393216 (堆积!), CLOSE-WAIT 1, TIME-WAIT 2
eth0: RX 32.2GiB / TX 42.5GiB, RX dropped=1538, TX dropped=2048 (丢包!)
TCP retransmits: 15.3% (异常高). Bandwidth: RX 850Mbps / TX 920Mbps (接近1Gbps饱和)"""
    if scenario == "conntrack_table_full":
        return """ss: ESTAB 8562 connections total across all pods (高连接数!)
eth0: RX 22.5GiB / TX 30.2GiB, TX dropped=3840
TCP retransmits: 8.2% (偏高)
dmesg: nf_conntrack: table full, dropping packet (dmesg报连接跟踪表溢出!)
dmesg: conntrack: connection tracking table full (131072/131072 entries)
说明: 内核conntrack表已满，导致新连接被丢弃→Pod DNS查询(UDP)间歇性失败"""
    if scenario == "arp_cache_full":
        return """ss: ESTAB 480 connections across cluster. No queue backlog.
eth0: RX 18.5GiB / TX 22.3GiB, TX dropped=0
TCP retransmits: 2.1%
dmesg: neighbour: arp_cache: neighbor table overflow! (ARP表溢出!)
dmesg: neighbour: ndisc_cache: neighbor table overflow!
ip neigh: total 1024 entries, gc_thresh1=128 gc_thresh2=512 gc_thresh3=1024 (已达上限!)
nstat: ArpFailed 3456 (ARP解析失败计数持续增长)
说明: ARP缓存gc_thresh3=1024已达上限，新节点/Pod加入集群时无法完成ARP解析→跨节点通信丢包"""
    if scenario == "mtu_mismatch":
        return """ss: ESTAB 12 connections, no queue backlog. No errors on interface.
eth0: RX 12.0GiB / TX 15.5GiB, TX dropped=0
TCP retransmits: 18.5% (异常高! 但接口层面无丢包)
Bandwidth: RX 85Mbps / TX 110Mbps (远未饱和)
ping test: `ping -M do -s 1472 10.0.1.5` → OK
ping test: `ping -M do -s 1473 10.0.1.5` → FAIL (ICMP frag needed but DF set)
Calico MTU: 1440 (检测到CNI MTU异常偏小)
Host eth0 MTU: 1500, Pod eth0 MTU: 1500 (主机与Pod MTU不一致!)
说明: overlay网络MTU与物理网卡MTU不匹配，ICMP fragmentation-needed被iptables拦截→大包被静默丢弃，只有小包能通"""
    if scenario == "softirq_starvation":
        return """ss: ESTAB 1200 connections, no Send-Q backlog.
eth0: RX 48.5GiB / TX 22.3GiB, RX dropped=125000 (大量丢包!)
TCP retransmits: 12.8% (异常高)
ethtool -S eth0: rx_missed_errors=98500 (硬件丢包!), rx_fifo_errors=320
/proc/softirqs: NET_RX CPU0=48532000 CPU1=152 CPU2=98 CPU3=84 (中断不均衡!)
/proc/net/softnet_stat: col3(dropped)=4800, col11(squeezed)=320
dmesg: net_ratelimit: 1200 callbacks suppressed (极高包速率)
说明: NIC ring buffer溢出→rx_missed_errors增长，中断全部绑定CPU0→softirq单核瓶颈"""
    if scenario == "tcp_accept_overflow":
        return """ss: ESTAB 850 connections, no Send-Q backlog.
eth0: RX 35.2GiB / TX 42.0GiB, dropped=0 (接口无丢包!)
TCP retransmits: 2.5% (正常)
nstat: ListenOverflows=5120 (TCP accept队列溢出!), TCPReqQFullDrop=0
ss -lnt: :8080 Recv-Q=128 Send-Q=128 (accept队列满! 已达somaxconn上限)
说明: 接口无硬件丢包，TCP重传正常—丢包发生在listen层，accept队列满导致新连接被静默丢弃"""
    if scenario == "multi_layer_cascading":
        return """ss: ESTAB 350 connections across cluster, no Send-Q backlog.
eth0: RX 15.5GiB / TX 22.3GiB, dropped=0 (接口层正常)
TCP retransmits: 3.2% (正常范围)
DNS测试 (从api-gateway pod): nslookup order-service.default.svc.cluster.local → timeout 5s (间歇失败!)
DNS测试 (从api-gateway pod): nslookup kubernetes.default.svc.cluster.local → OK (cluster DNS可达但慢, 延迟800ms)
CoreDNS副本数: 仅1/3 Running (worker-3上的2个CoreDNS副本因节点NotReady失联!)
说明: 接口层无丢包无重传—DNS失败是因为CoreDNS只剩1个副本，查询负载集中导致间歇超时
      注意: 非conntrack/ARP/MTU问题，dmesg无nf_conntrack和ARP相关错误"""
    return base


# ════════════════ Processes ════════════════

def processes_data(scenario: str) -> str:
    base = """PID 1234 app S 2.5% 8.2% java / PID 2345 app S 1.2% 4.5% mysqld"""
    if scenario == "memory_leak":
        return """PID 5678 app R 95.3% 78.5% java -Xmx512m -jar backend.jar (内存异常! RSS=6.2GB vs Xmx=512MB)
PID 2345 app S 3.1% 6.5% mysqld / PID 3456 app S 0.0% 0.3% gc-thread (blocked by GC)
Note: java PID 5678 RSS从512MB涨到6.2GB, Full GC频繁但无法回收 → native memory leak"""
    if scenario == "high_cpu_iowait":
        return """PID 1234 app D 2.7% 10.7% java / PID 2345 app D 1.8% 6.5% mysqld / PID 5678 app D 1.5% 4.2% nginx
3 processes in D state (uninterruptible sleep) — IO bottleneck"""
    if scenario == "disk_bottleneck":
        return """PID 1234 app D 3.2% 8.5% java / PID 2345 app D 2.1% 5.0% mysqld
PID 7890 app D 1.8% 3.5% postgres / PID 8901 app D 1.5% 2.8% elasticsearch
4+ processes in D state — severe disk IO contention"""
    if scenario == "container_crash":
        return """PID 1234 root S 25.0% 13.1% containerd / PID 2345 root S 15.0% 6.5% kubelet
Note: container java-app OOMKilled 12 times in last hour"""
    if scenario == "gpu_oom":
        return """PID 1234 ml R 95.0% 5.2% python3 train.py --batch-size=128 (CUDA OOM!)
PID 2345 ml S 2.5% 1.0% dataloader / PID 3456 ml S 0.0% 0.5% python watchdog"""
    if scenario == "kubelet_disk_io_starvation":
        return """PID 1234 root D 2.7% 13.1% containerd (D state! 等待IO)
PID 2345 root D 1.8% 6.5% kubelet (D state! 等待IO写入lease文件)
PID 5678 app D 3.5% 5.0% mysqld
PID 7890 app S 2.5% 3.0% java
注: containerd和kubelet均为D状态—磁盘IO阻塞导致守护进程无法工作"""
    if scenario == "node_oom_kubelet_killed":
        return """PID 3456 app S 42.0% 74.5% java -Xmx6g (内存消耗大户! RSS=5.8GB)
PID 7890 app S 18.0% 12.0% mysqld
PID 3210 root S 0.0% 0.0% [kubelet] (已被OOM Killer终止! 当前不可用)
dmesg: Out of memory: Killed process 3210 (kubelet) total-vm:4194304kB, anon-rss:245760kB
注: java占用5.8GB RSS触发系统OOM，OOM killer误杀了kubelet"""
    if scenario == "cpu_throttle_probe_failure":
        return """PID 5678 app R 48.0% 8.5% batch_processor.py (CPU密集型批处理!)
PID 5679 app R 45.0% 7.5% batch_processor.py (CPU密集型批处理!)
PID 1234 root S 8.0% 13.1% containerd (CPU被抢占)
PID 2345 root S 5.0% 6.5% kubelet (CPU被抢占，探针处理延迟!)
注: 批处理任务占满CPU，kubelet无法及时处理liveness probe → 超时"""
    if scenario == "conntrack_table_full":
        return """PID 1234 root S 8.0% 13.1% containerd (大量连接处理中)
PID 7890 app S 12.0% 4.5% java api-gateway (ESTAB 5000+ connections!)
PID 8901 app S 10.0% 4.0% envoy sidecar (ESTAB 4500+ connections!)
PID 3456 app S 5.0% 3.0% mysqld (ESTAB 800+ connections)
注: 高连接数进程导致conntrack表溢出，dmesg显示nf_conntrack: table full"""
    if scenario == "arp_cache_full":
        return """PID 1234 root S 5.0% 13.1% containerd
PID 2345 root S 5.0% 6.5% kubelet
PID 5678 root S 2.0% 3.0% kube-proxy (大量ARP解析请求)
注: 集群规模扩张导致ARP条目数超gc_thresh3=1024上限，ip neigh显示1024/1024已满"""
    if scenario == "mtu_mismatch":
        return """PID 1234 root S 8.0% 13.1% containerd
PID 5678 app S 25.0% 12.0% java api-server (接收大响应体时TCP重传率高)
PID 8901 app S 12.0% 4.0% nginx (upstream连接频繁TCP重传)
注: CNI MTU 1440 vs 主机 MTU 1500，大包被分片，jumbo frame应用(MySQL复制)影响严重"""
    if scenario == "softirq_starvation":
        return """PID 1234 root S 3.0% 8.0% containerd
PID 10 root S 92.0% 0.0% [ksoftirqd/0] (CPU0软中断处理占用92%!)
PID 2345 root S 2.0% 5.0% kubelet
注: ksoftirqd/0占满CPU0→NET_RX软中断处理瓶颈→ring buffer溢出→网卡硬件丢包"""
    if scenario == "tcp_accept_overflow":
        return """PID 5678 app S 35.0% 12.0% java api-server (单线程accept处理慢!)
PID 8901 app S 15.0% 8.0% nginx (worker_connections=128偏小)
注: 应用accept()调用频率不足，TCP accept队列达到somaxconn上限导致新连接被丢弃"""
    if scenario == "multi_layer_cascading":
        return """PID 5678 etcd D 3.5% 8.2% etcd --data-dir=/var/lib/etcd (D状态! 磁盘IO阻塞—etcd leader)
PID 8765 root S 12.0% 78.5% java -jar batch-processor.jar (主机Java! RSS=6.2GB超限)
PID 3210 root S 0.0% 0.0% [kubelet] (已被OOM Killer终止! worker-3上当前不可用)
PID 1234 root D 2.7% 13.1% containerd (D状态! 等待IO—worker-3)
PID 2345 root S 2.0% 5.0% kubelet (master-1,正常)
PID 9999 app S 55.0% 8.5% java api-gateway (RSS=650MiB > limit 512MiB! cgroup OOMKilled)
dmesg (master-1): INFO: task etcd:5678 blocked for more than 120 seconds (etcd IO hang!)
dmesg (worker-3): Out of memory: Killed process 3210 (kubelet) total-vm:4194304kB
注: 三个独立根因→etcd D状态(磁盘IO)、worker-3主机Java OOM杀kubelet、api-gateway cgroup OOM"""
    return base

def kubernetes_pods_data(scenario: str) -> str:
    base = """NAMESPACE  NAME                    READY STATUS   RESTARTS AGE
default    nginx-7d8f-abc12        1/1   Running 0        3d
default    redis-5c8b-ghi56        1/1   Running 1        5d
kube-system coredns-6d4b-jkl78     1/1   Running 2        7d"""
    if scenario == "container_crash":
        return """NAMESPACE  NAME                    READY STATUS             RESTARTS AGE
default    java-backend-abc12      0/1   CrashLoopBackOff   12       2h (反复重启!)
default    java-backend-def34      0/1   OOMKilled          8        2h (OOMKilled)
default    java-backend-ghi56      1/1   Running            3        5m (刚启动)
default    nginx-7d8b-jkl78        1/1   Running            0        3d
State: Waiting(CrashLoopBackOff) LastState: Terminated(OOMKilled,Exit:137)
Memory Limit: 512Mi — app needs ~800Mi under peak load"""
    if scenario == "gpu_oom":
        return """NAMESPACE  NAME                    READY STATUS             RESTARTS AGE
default    train-gpu-batch-abc12   0/1   OOMKilled          5        30m (CUDA OOM!)
default    train-gpu-batch-def34   1/1   Running            1        10m
nvidia     nvidia-device-plugin    1/1   Running            0        7d
GPU request: nvidia.com/gpu=1, no GPU memory limit set"""
    if scenario == "kubelet_disk_io_starvation":
        return """NAMESPACE  NAME                    READY STATUS             RESTARTS AGE
default    java-backend-abc12      1/1   Terminating         0        2d (被驱逐!)
default    java-backend-def34      0/1   Unknown             0        2d (因节点NotReady不可达!)
default    nginx-7d8b-jkl78        1/1   Unknown             0        3d (不可达!)
kube-system coredns-6d4b-mnp90     0/1   Unknown             0        7d
Events: Node worker-2 status is Unknown(NotReady). Pods being evicted.
kubelet日志: W0622... Error updating node lease, connect: disk IO timeout
说明: kubelet因磁盘IO阻塞无法续约lease，节点被标记NotReady，所有Pod被驱逐或不可达"""
    if scenario == "node_oom_kubelet_killed":
        return """NAMESPACE  NAME                    READY STATUS             RESTARTS AGE
default    java-backend-abc12      0/1   Unknown             0        2d (kubelet不可达!)
default    java-backend-def34      0/1   Unknown             0        2d (kubelet不可达!)
default    redis-5c8b-ghi56        0/1   Unknown             0        5d (kubelet不可达!)
kube-system coredns-6d4b-mnp90     0/1   Unknown             0        7d
Events: Node worker-2 status is Unknown. Kubelet stopped posting node status.
dmesg: oom-killer killed process kubelet (Virt:4194304kB, RSS:245760kB)
说明: 系统OOM Killer终止了kubelet进程，节点失联，所有Pod状态Unknown"""
    if scenario == "conntrack_table_full":
        return """NAMESPACE  NAME                    READY STATUS   RESTARTS AGE
default    api-gateway-abc12       1/1   Running 5        2d (频繁重启!)
default    api-gateway-def34       1/1   Running 3        2d (频繁重启!)
default    nginx-7d8b-jkl78        1/1   Running 0        3d
kube-system coredns-6d4b-mnp90     1/1   Running 0        7d
Events: api-gateway: Readiness probe failed (DNS lookup timeout 5s)
Events: api-gateway: Liveness probe failed (connection refused after conntrack drop)
dmesg: nf_conntrack: table full (131072/131072 entries), dropping packet
说明: conntrack表满导致UDP DNS查询和TCP连接被内核丢弃→探针超时→Pod重启"""
    if scenario == "cpu_throttle_probe_failure":
        return """NAMESPACE  NAME                    READY STATUS             RESTARTS AGE
default    java-backend-abc12      1/1   Running            8        2d (频繁重启!)
default    java-backend-def34      1/1   Running            6        2d (频繁重启!)
default    nginx-7d8b-jkl78        1/1   Running            0        3d
kube-system coredns-6d4b-mnp90     1/1   Running            0        7d
Events: java-backend: Liveness probe failed (http-get timeout 5s)
Events: java-backend: Readiness probe failed (timeout)
kubelet日志: E0622... PLEG is not healthy: pleg was last active 8.5s ago
说明: CPU被批处理任务占满，kubelet PLEG/Pod探针执行超时→健康Pod被kill重启"""
    if scenario == "arp_cache_full":
        return """NAMESPACE  NAME                    READY STATUS   RESTARTS AGE
default    api-gateway-abc12       1/1   Running 0        1d
default    api-gateway-def34       0/1   Running 8        1d (频繁重启!)
default    backend-worker-ghijk    0/1   Running 5        2h (与worker-3上的服务通信失败!)
default    nginx-7d8b-jkl78        1/1   Running 0        3d
kube-system kube-proxy-lmn99       1/1   Running 0        7d
Events: api-gateway: connection timeout to 10.0.3.45 (跨节点通信)
Events: backend-worker: ARP resolution failed for 10.0.3.45 (ARP缓存满)
dmesg: neighbour: arp_cache: neighbor table overflow! (gc_thresh3=1024已满)
说明: ARP缓存达到上限gc_thresh3=1024，新节点/Pod IP无法完成ARP解析→跨节点连接超时"""
    if scenario == "mtu_mismatch":
        return """NAMESPACE  NAME                    READY STATUS   RESTARTS AGE
default    api-server-abc12        1/1   Running 0        1d
default    mysql-master-def34      1/1   Running 0        1d
default    mysql-replica-ghij      1/1   Running 0        2h (MySQL复制频繁中断!)
default    nginx-7d8b-jkl78        1/1   Running 0        3d
Events: mysql-replica: io_thread connection timeout during binlog transfer
Events: api-server: upstream response truncated (大响应体)
注: MySQL复制使用大binlog事件(>1440字节MTU)，分片失败导致复制中断"""
    if scenario == "softirq_starvation":
        return """NAMESPACE  NAME                    READY STATUS   RESTARTS AGE
default    api-gateway-abc12       1/1   Running 0        1d
default    api-gateway-def34       1/1   Running 3        1d (重传过多触发探针超时)
default    nginx-7d8b-jkl78        1/1   Running 0        3d
Events: api-gateway: liveness probe failed (timeout 5s during high softirq)
说明: 软中断占满CPU0→网络包处理延迟→应用TCP重传升高→探针超时→Pod重启"""
    if scenario == "tcp_accept_overflow":
        return """NAMESPACE  NAME                    READY STATUS   RESTARTS AGE
default    api-server-abc12        1/1   Running 0        1d
default    api-server-def34        1/1   Running 0        1d
default    nginx-7d8b-jkl78        1/1   Running 0        3d
Events: api-server: clients reporting connection timeout (not crash)
说明: 节点和Pod均健康，但客户端间歇连接超时→TCP accept队列溢出"""
    if scenario == "multi_layer_cascading":
        return """NAMESPACE  NAME                    READY STATUS             RESTARTS AGE
default    api-gateway-abc12       0/1   CrashLoopBackOff     18       2h (反复重启!)
default    api-gateway-def34       0/1   OOMKilled            12       2h (OOMKilled)
default    api-gateway-ghi56       1/1   Running              2        8m (刚启动)
default    user-service-abc12      0/1   Unknown              0        2d (worker-3上, 因节点NotReady不可达!)
default    user-service-def34      1/1   Running              0        2d (worker-1上, 正常)
default    order-service-xyz78     0/1   Unknown              0        5d (worker-3上, 不可达!)
default    order-service-pqr99     1/1   Running              0        5d (worker-2上, 正常)
kube-system coredns-6d4b-xyz11     1/1   Running              0        7d (worker-1, 唯一正常!)
kube-system coredns-6d4b-abc99     0/1   Unknown              0        7d (worker-3上, 失联!)
kube-system coredns-6d4b-def88     0/1   Unknown              0        7d (worker-3上, 失联!)
api-gateway Events: State: Waiting(CrashLoopBackOff) LastState: Terminated(OOMKilled,Exit:137)
api-gateway Memory Limit: 512Mi | api-gateway实际RSS: ~650MiB
user-service Events: Node worker-3 status is Unknown(NotReady). Pod evicted.
说明: 三根因叠加→(1)api-gateway OOMKilled因limit<实际RSS→cgroup OOM
      (2)worker-3上Pod全部Unknown因kubelet被OOM Kill→节点失联
      (3)CoreDNS从3副本降至1副本→DNS查询负载集中→间歇超时"""
    return base


def kubernetes_nodes_data(scenario: str) -> str:
    base = """NAME          STATUS  CPU% MEM% VERSION
master-1      Ready   25%  45%  v1.28.5
worker-1      Ready   35%  55%  v1.28.5
worker-2      Ready   40%  60%  v1.28.5"""
    if scenario in ("memory_leak", "container_crash"):
        return """NAME          STATUS  CPU% MEM% CONDITIONS
master-1      Ready   30%  50%  -
worker-1      Ready   45%  78%  -
worker-2      Ready   55%  92%  MemoryPressure=True!"""
    if scenario == "gpu_oom":
        return """NAME          STATUS  CPU% MEM% GPU  CONDITIONS
master-1      Ready   30%  50%  -   -
worker-1      Ready   25%  40%  -   -
worker-gpu-1  Ready   60%  55%  A100 MemoryPressure=False"""
    if scenario == "kubelet_disk_io_starvation":
        return """NAME          STATUS     CPU% MEM% CONDITIONS
master-1      Ready      30%  50%  DiskPressure=False
worker-1      Ready      35%  55%  DiskPressure=False
worker-2      NotReady   20%  60%  Ready=Unknown(Reason:NodeStatusUnknown)
                                   DiskPressure=Unknown, MemoryPressure=Unknown
                                   Kubelet last heartbeat: 3m48s ago (超时!)
说明: worker-2 NotReady — kubelet lease因磁盘IO阻塞未续约，节点被标记Unknown"""
    if scenario == "node_oom_kubelet_killed":
        return """NAME          STATUS     CPU% MEM% CONDITIONS
master-1      Ready      30%  50%  -
worker-1      Ready      45%  78%  MemoryPressure=True
worker-2      NotReady   20%  95%  Ready=Unknown(Reason:NodeStatusUnknown)
                                   MemoryPressure=Unknown, DiskPressure=Unknown
                                   Kubelet last heartbeat: 5m12s ago (长时间失联!)
说明: worker-2 Kubelet被OOM Killer终止，节点完全失联，所有条件Unknown"""
    if scenario == "conntrack_table_full":
        return """NAME          STATUS  CPU% MEM% CONDITIONS
master-1      Ready   30%  50%  -
worker-1      Ready   45%  55%  NetworkUnavailable=False
worker-2      Ready   35%  52%  NetworkUnavailable=True (conntrack table full!)
说明: 节点表面Ready，但NetworkUnavailable因conntrack表满被标记—需查看dmesg"""
    if scenario == "cpu_throttle_probe_failure":
        return """NAME          STATUS  CPU% MEM% CONDITIONS
master-1      Ready   30%  50%  -
worker-1      Ready   95%  65%  (CPU几乎满载但节点Ready)
worker-2      Ready   92%  60%  (CPU几乎满载但节点Ready)
说明: 节点仍为Ready，但kubelet PLEG不健康—CPU争抢导致探针超时，非节点层面故障"""
    if scenario == "arp_cache_full":
        return """NAME          STATUS  CPU% MEM% CONDITIONS
master-1      Ready   30%  50%  -
worker-1      Ready   35%  55%  -
worker-2      Ready   40%  58%  -
worker-3      Ready   38%  52%  -
worker-4      Ready   32%  56%  NetworkUnavailable=True
说明: 节点表面Ready，但ARP缓存gc_thresh3=1024已达上限—新节点无法完成ARP解析，跨节点Pod通信丢包"""
    if scenario == "mtu_mismatch":
        return """NAME          STATUS  CPU% MEM% CONDITIONS
master-1      Ready   25%  45%  -
worker-1      Ready   35%  50%  -
worker-2      Ready   40%  55%  -
说明: 节点均Ready，无异常conditions—但CNI MTU(1440)≠主机MTU(1500)，大包TCP重传率高，ping大包不通"""
    if scenario == "softirq_starvation":
        return """NAME          STATUS  CPU% MEM% CONDITIONS
master-1      Ready   25%  45%  -
worker-1      Ready   35%  50%  -
worker-2      Ready   40%  55%  (ksoftirqd/0=92%CPU—软中断占满CPU0!)
说明: 节点Ready，无显式conditions异常—但CPU0的ksoftirqd占满导致ring buffer硬件丢包"""
    if scenario == "tcp_accept_overflow":
        return """NAME          STATUS  CPU% MEM% CONDITIONS
master-1      Ready   25%  45%  -
worker-1      Ready   35%  50%  -
worker-2      Ready   40%  55%  -
说明: 节点均Ready，资源正常—问题在应用层TCP accept队列(somaxconn=128不足)"""
    if scenario == "multi_layer_cascading":
        return """NAME          STATUS     CPU% MEM% CONDITIONS
master-1      Ready      35%  50%  (etcd IO饱和但节点仍Ready)
master-2      Ready      20%  45%  -
master-3      Ready      22%  48%  -
worker-1      Ready      45%  55%  -
worker-2      Ready      40%  52%  -
worker-3      NotReady   18%  92%  Ready=Unknown(Reason:NodeStatusUnknown)
                                  MemoryPressure=Unknown, DiskPressure=Unknown
                                  Kubelet last heartbeat: 6m35s ago (长时间失联!)
worker-4      Ready      38%  50%  -
worker-5      Ready      42%  58%  -
说明: worker-3 NotReady—kubelet被OOM Killer终止，所有Conditions变为Unknown
      master-1 Ready但etcd磁盘IO饱和(需从check_disk和check_processes发现)
      注意: 控制面节点表面Ready但kubectl操作慢→提示etcd性能问题"""
    return base


# ════════════════ GPU ════════════════

def gpu_health_data(scenario: str) -> str:
    base = """GPU 0: Tesla V100-SXM2-32GB
Temperature: 62°C | Power: 180W/300W | Throttle: None
ECC Errors: 0 correctable / 0 uncorrectable
PCIe: Gen3 x16, Link Speed: 8.0 GT/s"""
    if scenario == "gpu_oom":
        return """GPU 0: Tesla V100-SXM2-32GB
Temperature: 78°C (偏高) | Power: 250W/300W | Throttle: None
ECC Errors: 3 correctable / 0 uncorrectable
PCIe: Gen3 x16, Link Speed: 8.0 GT/s
Data Center GPU Max Clock Rate: 1380 MHz, Current: 1312 MHz"""
    return base


def gpu_memory_data(scenario: str) -> str:
    base = """GPU 0: 8192MiB / 32768MiB (25% used)
Processes: PID 1234 2048MiB python3, PID 2345 1024MiB python3"""
    if scenario == "gpu_oom":
        return """GPU 0: 32128MiB / 32768MiB (98% used! near OOM)
Processes: PID 1234 28672MiB python3 train.py (main consumer)
PID 1234 exit: CUDA out of memory. Tried to allocate 512.00 MiB
WARNING: 3 orphaned GPU memory allocations (2048MiB total) from crashed processes"""
    return base


def gpu_utilization_data(scenario: str) -> str:
    base = """GPU 0: GPU-Util 85% | Memory-Util 25% | SM Activity 82%
Encoder: 0% | Decoder: 15% | Memory BW: 45%
Running processes: PID 1234 (GpuMemory: 8GiB, Type: C)"""
    if scenario == "gpu_oom":
        return """GPU 0: GPU-Util 98% | Memory-Util 98% | SM Activity 95%
Encoder: 0% | Decoder: 0% | Memory BW: 92%
Running processes: PID 1234 (GpuMemory: 28GiB, Type: C)
System Memory BW: 15.0 GB/s read, 12.0 GB/s write (CPU-GPU transfer bottleneck?)"""
    return base


# ════════════════ K8s Control Plane ════════════════

def kubernetes_control_plane_data(scenario: str) -> str:
    base = """COMPONENT              STATUS   MESSAGE
kube-apiserver         Healthy  serving at https://172.16.0.1:6443
kube-scheduler         Healthy  leader elected
kube-controller-manager Healthy leader elected
etcd-0                 Healthy  raft index: 123456"""
    if scenario == "node_oom_kubelet_killed":
        return """COMPONENT              STATUS    MESSAGE
kube-apiserver         Unhealthy timeout: dial tcp 172.16.0.1:6443 i/o timeout
kube-scheduler         Healthy   leader elected
kube-controller-manager Healthy  leader elected
etcd-0                 Unhealthy request timed out (disk io saturated)"""
    if scenario == "api_server_overload":
        return """COMPONENT              STATUS    MESSAGE
kube-apiserver         Healthy   serving at https://172.16.0.1:6443 (high latency: 3s+ p99)
kube-scheduler         Healthy   leader elected
kube-controller-manager Healthy  leader elected
etcd-0                 Healthy   raft index: 234567 (fsync latency: 250ms p99)"""
    return base
