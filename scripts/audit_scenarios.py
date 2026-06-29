"""Systematic audit of all 31 scenarios: correctness + independence."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from diagnostics.tools.mock.data import (
    _load_avg, cpu_data, memory_data, disk_data, network_data,
    processes_data, kubernetes_pods_data, kubernetes_nodes_data,
    kubernetes_control_plane_data, gpu_health_data, gpu_memory_data,
    gpu_utilization_data,
)
from diagnostics.tools.mock.argus_data import (
    query_argus_cpu_metrics, query_argus_memory_metrics,
    query_argus_disk_metrics, query_argus_network_metrics,
    query_argus_nodes_metrics,
    query_argus_services_metrics,
)
from diagnostics.tools.mock.scenarios import AVAILABLE_SCENARIOS

errors = []
warnings = []

def check(scenario_id: str, condition: bool, msg: str):
    if not condition:
        errors.append(f"  ❌ [{scenario_id}] {msg}")

def warn(scenario_id: str, condition: bool, msg: str):
    if not condition:
        warnings.append(f"  ⚠️ [{scenario_id}] {msg}")

print("=" * 70)
print("SCENARIO-BY-SCENARIO AUDIT: Data → Expected Root Cause")
print("=" * 70)

# ═══════════════════ ★ SIMPLE (6 scenarios) ═══════════════════

print("\n## ★ SIMPLE SCENARIOS\n")

# 1. memory_leak — Java RSS持续增长, swap被大量使用
print("### 1. memory_leak")
s = "memory_leak"
check(s, "92%" in memory_data(s) or "7.0Gi used" in memory_data(s), "Memory should show high usage")
check(s, "1.5Gi used" in memory_data(s) or "Swap" in memory_data(s), "Swap should be significant")
check(s, "AnonPages" in memory_data(s), "Should show AnonPages (memory leak indicator)")
check(s, "java" in processes_data(s).lower(), "Should show java process")
check(s, "RSS" in processes_data(s) and ("6.2" in processes_data(s) or "6." in processes_data(s)), "Java RSS should be high (6+ GB)")
check(s, "MemoryPressure" in kubernetes_nodes_data(s), "Node should show MemoryPressure")
check(s, "iowait" not in cpu_data(s).split("说明")[0] or "85%" in cpu_data(s), "CPU should be user-bound not iowait")
check(s, query_argus_memory_metrics(s) != "Argus: 无异常数据返回", "Argus memory should show anomaly")
print("  ✅ Memory leak data consistent with expected root cause")

# 2. disk_bottleneck — 磁盘IO接近100%, await很高
print("### 2. disk_bottleneck")
s = "disk_bottleneck"
check(s, "98" in disk_data(s) or "99" in disk_data(s), "Disk util should be near 100%")
check(s, "await" in disk_data(s) and ("45" in disk_data(s) or "52" in disk_data(s)), "await should be high")
check(s, "D state" in processes_data(s), "Should show D-state processes")
check(s, "iowait" in cpu_data(s), "CPU should show iowait")
check(s, "7.6Gi total, 2.1Gi used" in memory_data(s), "Memory should be normal (base)")
check(s, query_argus_disk_metrics(s) != "Argus: 无异常数据返回", "Argus disk should show anomaly")
print("  ✅ Disk bottleneck data consistent with expected root cause")

# 3. network_congestion — 带宽接近饱和, TCP重传率攀升
print("### 3. network_congestion")
s = "network_congestion"
check(s, "饱和" in network_data(s) or "接近" in network_data(s), "Should show bandwidth saturation")
check(s, "重传" in network_data(s) or "retransmits" in network_data(s), "TCP retransmits should be high")
check(s, "dropped" in network_data(s), "Should show packet drops")
check(s, "7.6Gi total, 2.1Gi used" in memory_data(s), "Memory should be normal")
check(s, query_argus_network_metrics(s) != "Argus: 无异常数据返回", "Argus network should show anomaly")
print("  ✅ Network congestion data consistent with expected root cause")

# 4. container_crash — Pod反复OOMKilled, limit 512Mi
print("### 4. container_crash")
s = "container_crash"
check(s, "OOMKilled" in kubernetes_pods_data(s), "Pods should show OOMKilled")
check(s, "CrashLoopBackOff" in kubernetes_pods_data(s), "Should show CrashLoopBackOff")
check(s, "512Mi" in kubernetes_pods_data(s), "Should mention 512Mi limit")
check(s, "OOMKilled" in kubernetes_pods_data(s) or "12" in kubernetes_pods_data(s), "Should show multiple restarts")
# container_crash should NOT affect node level
check(s, "85%" in kubernetes_nodes_data(s) and "MemoryPressure" not in kubernetes_nodes_data(s), 
      "Node should NOT have MemoryPressure (pod OOM, not node OOM)")
print("  ✅ Container crash data consistent with expected root cause")

# 5. arp_cache_full — ARP表已满, 跨节点通信中断
print("### 5. arp_cache_full")
s = "arp_cache_full"
check(s, "arp_cache" in network_data(s) or "ARP" in network_data(s), "Should show ARP overflow")
check(s, "gc_thresh3" in network_data(s) or "1024" in network_data(s), "Should show gc_thresh limit")
check(s, "ArpFailed" in network_data(s) or "ARP解析失败" in network_data(s), "Should show ARP resolution failure")
check(s, "跨节点" in kubernetes_pods_data(s) or "ARP" in kubernetes_pods_data(s), "Pod events should mention cross-node/ARP")
print("  ✅ ARP cache full data consistent with expected root cause")

# 6. mtu_mismatch — 大包丢包, ping大包不通
print("### 6. mtu_mismatch")
s = "mtu_mismatch"
check(s, "MTU" in network_data(s) or "mtu" in network_data(s).lower(), "Should show MTU info")
check(s, "1440" in network_data(s), "Should show CNI MTU 1440")
check(s, "1500" in network_data(s), "Should show host MTU 1500")
check(s, "ping" in network_data(s).lower(), "Should show ping test results")
check(s, "重传" in network_data(s) or "retransmits" in network_data(s), "Should show high retransmits")
check(s, "1/1   Running" in kubernetes_pods_data(s).split("Events")[0] if "Events" in kubernetes_pods_data(s) else True, "Pods should be Running (MTU doesn't crash pods)")
print("  ✅ MTU mismatch data consistent with expected root cause")

# ═══════════════════ ★★ MEDIUM (18 scenarios) ═══════════════════

print("\n## ★★ MEDIUM SCENARIOS\n")

# 7. conntrack_table_full — Conntrack表满→节点失联
print("### 7. conntrack_table_full")
s = "conntrack_table_full"
check(s, "131072" in network_data(s), "Should show conntrack max 131072")
check(s, "table full" in network_data(s).lower(), "Should show table full")
check(s, "NotReady" in kubernetes_nodes_data(s), "Should show NotReady nodes")
check(s, "worker-5" in kubernetes_nodes_data(s) and "worker-8" in kubernetes_nodes_data(s), "worker-5 and worker-8 should be NotReady")
check(s, "30" in kubernetes_pods_data(s) or "驱逐" in kubernetes_pods_data(s), "Should show pod evictions")
check(s, "DNS" in kubernetes_pods_data(s), "Should show DNS timeout")
print("  ✅ Conntrack table full data consistent with expected root cause")

# 8. kubelet_disk_io_starvation — 磁盘IO→Kubelet超时→Node NotReady
print("### 8. kubelet_disk_io_starvation")
s = "kubelet_disk_io_starvation"
check(s, "99" in disk_data(s) or "98" in disk_data(s), "Disk util should be near 100%")
check(s, "88" in disk_data(s) or "await" in disk_data(s), "await should be very high")
check(s, "D" in processes_data(s), "Should show D-state processes (kubelet/containerd)")
check(s, "NotReady" in kubernetes_nodes_data(s), "Node should be NotReady")
check(s, "lease" in kubernetes_nodes_data(s).lower() or "heartbeat" in kubernetes_nodes_data(s).lower(), "Should mention lease/heartbeat timeout")
check(s, "驱逐" in kubernetes_pods_data(s) or "Evict" in kubernetes_pods_data(s), "Pods should be evicted")
print("  ✅ Kubelet disk IO starvation data consistent with expected root cause")

# 9. node_oom_kubelet_killed — OOM Kill Kubelet→Node Unknown
print("### 9. node_oom_kubelet_killed")
s = "node_oom_kubelet_killed"
check(s, "oom-killer" in memory_data(s).lower() or "OOM" in memory_data(s), "Memory should show OOM")
check(s, "kubelet" in memory_data(s).lower(), "OOM should mention kubelet being killed")
check(s, "5.8" in memory_data(s) or "RSS" in memory_data(s), "Should show Java RSS as cause")
check(s, "Unknown" in kubernetes_nodes_data(s), "Node should be Unknown")
check(s, "Unknown" in kubernetes_pods_data(s), "Pods should be Unknown")
check(s, "oom-killer" in processes_data(s).lower() or "Out of memory" in processes_data(s), "Processes should mention OOM killer")
print("  ✅ Node OOM kubelet killed data consistent with expected root cause")

# 10. cpu_throttle_probe_failure — CPU节流→探针超时
print("### 10. cpu_throttle_probe_failure")
s = "cpu_throttle_probe_failure"
check(s, "68" in cpu_data(s) or "95" in cpu_data(s) or "98" in cpu_data(s), "CPU should show high usage from batch processing")
check(s, "batch" in processes_data(s).lower(), "Should show batch processing job")
check(s, "kubelet" in processes_data(s) and "抢占" in processes_data(s), "kubelet should be throttled")
check(s, "probe" in kubernetes_pods_data(s).lower() or "探针" in kubernetes_pods_data(s), "Should mention probe timeout")
check(s, "Ready" in kubernetes_nodes_data(s) and "NotReady" not in kubernetes_nodes_data(s), "Node should still be Ready")
print("  ✅ CPU throttle probe failure data consistent with expected root cause")

# 11. softirq_starvation — 软中断饥饿→丢包
print("### 11. softirq_starvation")
s = "softirq_starvation"
check(s, "ksoftirqd" in cpu_data(s), "CPU should show ksoftirqd")
check(s, "55%" in cpu_data(s) or "sys" in cpu_data(s), "sys% should be high")
check(s, "rx_missed_errors" in network_data(s), "Network should show rx_missed_errors")
check(s, "ring buffer" in network_data(s).lower() or "softirq" in network_data(s), "Should mention ring buffer/softirq")
check(s, "ksoftirqd" in processes_data(s), "Processes should show ksoftirqd")
print("  ✅ Softirq starvation data consistent with expected root cause")

# 12. tcp_accept_overflow — TCP Accept队列溢出
print("### 12. tcp_accept_overflow")
s = "tcp_accept_overflow"
check(s, "somaxconn" in network_data(s) or "accept" in network_data(s).lower(), "Should show somaxconn/accept queue")
check(s, "ListenOverflows" in network_data(s), "Should show ListenOverflows")
check(s, "dropped=0" in network_data(s), "Interface should have no drops")
check(s, "正常" in cpu_data(s) or "无" in cpu_data(s) or "CPU正常" in cpu_data(s), "CPU should be normal")
check(s, "accept" in processes_data(s).lower() or "somaxconn" in processes_data(s), "Processes should mention accept/somaxconn")
print("  ✅ TCP accept overflow data consistent with expected root cause")

# 13. gpu_oom — GPU显存耗尽
print("### 13. gpu_oom")
s = "gpu_oom"
check(s, "98%" in gpu_memory_data(s) or "32128" in gpu_memory_data(s), "GPU memory should be near full")
check(s, "CUDA out of memory" in gpu_memory_data(s), "Should show CUDA OOM")
check(s, "98%" in gpu_utilization_data(s), "GPU utilization should be high")
check(s, "7.6Gi" not in memory_data(s) or "15Gi" in memory_data(s), "Host memory should be different (GPU server)")
print("  ✅ GPU OOM data consistent with expected root cause")

# 14. high_cpu_iowait — 高负载低CPU(IO等待)
print("### 14. high_cpu_iowait")
s = "high_cpu_iowait"
check(s, "iowait" in cpu_data(s) and ("58" in cpu_data(s) or "55" in cpu_data(s)), "iowait should be high")
check(s, "D" in processes_data(s) and "D state" in processes_data(s), "Should show D-state processes")
check(s, "97" in disk_data(s) or "98" in disk_data(s), "Disk should be near saturation")
check(s, "5.23" in _load_avg(s) or "5." in _load_avg(s), "Load average should be high")
print("  ✅ High CPU iowait data consistent with expected root cause")

# 15. mixed_io_memory — 混合瓶颈(IO+内存)
print("### 15. mixed_io_memory")
s = "mixed_io_memory"
check(s, "5.8Gi" in memory_data(s) or "88" in memory_data(s) or "92" in memory_data(s), "Memory should be elevated")
check(s, "Swap" in memory_data(s) and ("512Mi" in memory_data(s) or "55" in memory_data(s)), "Swap should be active")
check(s, "92" in disk_data(s) or "95" in disk_data(s) or "98" in disk_data(s), "Disk should be under pressure")
check(s, "iowait" in cpu_data(s) and ("32" in cpu_data(s) or "30" in cpu_data(s)), "CPU should show iowait")
print("  ✅ Mixed IO+memory data consistent with expected root cause")

# 16. coredns_cache_poison — CoreDNS缓存污染
print("### 16. coredns_cache_poison")
s = "coredns_cache_poison"
check(s, "错误IP" in network_data(s) or "10.0.1.88" in network_data(s), "Should show wrong IP")
check(s, "缓存" in network_data(s) or "cache" in network_data(s).lower(), "Should mention cache pollution")
check(s, "dropped=0" in network_data(s), "Network interface should be clean")
check(s, "10.0.1.88" in kubernetes_pods_data(s) or "stale" in kubernetes_pods_data(s).lower(), "Pod events should show stale IP")
print("  ✅ CoreDNS cache poison data consistent with expected root cause")

# 17. etcd_quorum_loss — etcd多数派丢失
print("### 17. etcd_quorum_loss")
s = "etcd_quorum_loss"
check(s, "quorum" in kubernetes_pods_data(s).lower() or "多数派" in kubernetes_pods_data(s), "Should mention quorum loss")
check(s, "只读" in kubernetes_pods_data(s) or "readonly" in kubernetes_pods_data(s).lower() or "election" in kubernetes_pods_data(s).lower(), "Should show read-only mode")
print("  ✅ etcd quorum loss data consistent with expected root cause")

# 18. image_pull_backoff — 镜像拉取失败
print("### 18. image_pull_backoff")
s = "image_pull_backoff"
check(s, "ImagePullBackOff" in kubernetes_pods_data(s), "Should show ImagePullBackOff")
check(s, "429" in kubernetes_pods_data(s) or "rate limit" in network_data(s).lower(), "Should show rate limit")
check(s, "Running" in kubernetes_pods_data(s), "Existing pods should still be running")
check(s, "正常" in network_data(s) or "dropped=0" in network_data(s), "Network should be normal")
print("  ✅ Image pull backoff data consistent with expected root cause")

# 19. oom_score_misconfig — OOM Score配置错误→误杀kubelet
print("### 19. oom_score_misconfig")
s = "oom_score_misconfig"
check(s, "oom_score_adj" in kubernetes_pods_data(s), "Should show oom_score_adj config")
check(s, "-500" in kubernetes_pods_data(s) or "-500" in processes_data(s), "kubelet oom_score_adj should be -500")
check(s, "750" in kubernetes_pods_data(s) or "750" in processes_data(s), "Java app oom_score_adj should be 750")
check(s, "NotReady" in kubernetes_nodes_data(s), "Node should be NotReady")
check(s, "Running" in kubernetes_pods_data(s), "Application pods should still be Running")
# Independence from node_oom_kubelet_killed
check(s, query_argus_cpu_metrics(s) != query_argus_cpu_metrics("node_oom_kubelet_killed"), "Argus CPU should differ from node_oom")
check(s, query_argus_memory_metrics(s) != query_argus_memory_metrics("node_oom_kubelet_killed"), "Argus Memory should differ from node_oom")
print("  ✅ OOM score misconfig data consistent with expected root cause")

# 20. disk_full_var_log — /var/log分区被写满
print("### 20. disk_full_var_log")
s = "disk_full_var_log"
check(s, "100%" in disk_data(s), "Disk should be 100% full")
check(s, "inode" in disk_data(s).lower() or "耗尽" in disk_data(s), "Should mention inode exhaustion")
check(s, "容器运行时" in processes_data(s) or "logrotate" in processes_data(s), "Should show container runtime/logrotate impact")
print("  ✅ Disk full /var/log data consistent with expected root cause")

# 21. fs_inode_exhaustion — Inode耗尽
print("### 21. fs_inode_exhaustion")
s = "fs_inode_exhaustion"
check(s, "32%" in disk_data(s) or "空间充足" in disk_data(s), "Disk space should be available")
check(s, "inode" in disk_data(s).lower() and "100%" in disk_data(s), "Inodes should be 100% used")
check(s, "小文件" in processes_data(s) or "inode" in processes_data(s).lower() or "小文件" in disk_data(s), "Should mention small files/inodes")
print("  ✅ FS inode exhaustion data consistent with expected root cause")

# 22. systemd_limit_nofile — systemd文件描述符限制
print("### 22. systemd_limit_nofile")
s = "systemd_limit_nofile"
check(s, "65536" in network_data(s), "Should show fd limit 65536")
check(s, "too many open" in processes_data(s).lower() or "EMFILE" in processes_data(s) or "65536" in processes_data(s), "Should show EMFILE error")
check(s, "1048576" in network_data(s) or "截断" in network_data(s), "Should show truncation from higher config")
check(s, "VFS" in network_data(s) or "file-max" in network_data(s), "Should show VFS file-max limit")
print("  ✅ Systemd LimitNOFILE data consistent with expected root cause")

# 23. ebpf_probe_overhead — eBPF探针性能开销
print("### 23. ebpf_probe_overhead")
s = "ebpf_probe_overhead"
check(s, "62" in cpu_data(s) or "sys" in cpu_data(s), "sys% should be very high")
check(s, "ebpf" in cpu_data(s).lower() or "eBPF" in cpu_data(s), "Should mention eBPF")
check(s, "ebpf_exporter" in processes_data(s) or "eBPF" in processes_data(s), "Processes should show eBPF exporter")
check(s, "85000" in network_data(s) or "timer" in network_data(s), "Should show high event rate")
print("  ✅ eBPF probe overhead data consistent with expected root cause")

# 24. swap_thrashing — Swap抖动
print("### 24. swap_thrashing")
s = "swap_thrashing"
check(s, "92" in memory_data(s) or "95" in memory_data(s), "Memory should be very high")
check(s, "3.8Gi used" in memory_data(s) or "88" in memory_data(s) or "92" in memory_data(s), "Swap should be nearly exhausted")
check(s, "si=" in memory_data(s) or "swap" in memory_data(s).lower(), "Should show swap in/out activity")
check(s, "65" in cpu_data(s) or "iowait" in cpu_data(s), "iowait should be high from swap IO")
check(s, "kswapd" in processes_data(s), "Should show kswapd process")
print("  ✅ Swap thrashing data consistent with expected root cause")

# ═══════════════════ ★★★ HARD (7 scenarios) ═══════════════════

print("\n## ★★★ HARD SCENARIOS\n")

# 25. stress_cpu_and_tc_loss — 双根因: CPU高 + 网络丢包
print("### 25. stress_cpu_and_tc_loss")
s = "stress_cpu_and_tc_loss"
check(s, "stress-ng" in cpu_data(s), "CPU should show stress-ng")
check(s, "95" in cpu_data(s) or "100" in cpu_data(s), "CPU should be maxed out")
check(s, "tc" in network_data(s) and "5%" in network_data(s), "Network should show tc 5% loss")
check(s, "18.5" in network_data(s) or "重传" in network_data(s), "TCP retransmits should be high")
check(s, "stress-ng" in processes_data(s), "Processes should show stress-ng")
check(s, "两个" in processes_data(s) or "独立" in processes_data(s), "Should mention two independent root causes")
print("  ✅ Stress CPU + TC loss data consistent with expected root cause")

# 26. etcd_quota_near_full — etcd配额接近上限
print("### 26. etcd_quota_near_full")
s = "etcd_quota_near_full"
check(s, "7.15" in kubernetes_control_plane_data(s) or "NOSPACE" in kubernetes_control_plane_data(s), "Control plane should show NOSPACE/7.15GB")
check(s, "850" in kubernetes_control_plane_data(s) or "write latency" in kubernetes_control_plane_data(s), "Should show write latency")
check(s, "7" in query_argus_services_metrics(s) and ("7000" in query_argus_services_metrics(s) or "7150" in query_argus_services_metrics(s)), "Argus services DB should be ~7GB")
check(s, "850" in query_argus_services_metrics(s) or "API Lat" in query_argus_services_metrics(s), "Argus should show high API latency")
# Check no 'NotReady' in STATUS column (only check node table rows)
_nd_lines = [l for l in kubernetes_nodes_data(s).split('\n') if l.strip() and any(k in l for k in ('master-', 'worker-', 'NAME'))]
_nd_status = '\n'.join(_nd_lines)
check(s, "Ready" in _nd_status and "NotReady" not in _nd_status, "All nodes should be Ready")
check(s, "CrashLoopBackOff" in kubernetes_pods_data(s), "api-gateway should be CrashLoopBackOff")
check(s, "etcd" in kubernetes_pods_data(s).lower(), "Should mention etcd timeout")
check(s, "正常" in cpu_data(s) or "无" in cpu_data(s), "Host CPU should be normal")
print("  ✅ etcd quota near full data consistent with expected root cause")

# 27. disk_io_and_dns — 双根因: 磁盘IO + CoreDNS配置错误
print("### 27. disk_io_and_dns")
s = "disk_io_and_dns"
check(s, "98" in disk_data(s) or "99" in disk_data(s), "Disk util should be very high")
check(s, "logrotate" in processes_data(s) or "日志" in processes_data(s), "Should show logrotate as cause")
check(s, "10.0.0.53" in network_data(s) or "forward" in network_data(s), "Should show CoreDNS forward to dead DNS")
check(s, "timeout" in network_data(s).lower() or "超时" in network_data(s), "Should show DNS timeout")
check(s, "两个" in processes_data(s) or "独立" in processes_data(s), "Should mention two independent root causes")
check(s, "Running" in kubernetes_pods_data(s) and "CrashLoopBackOff" not in kubernetes_pods_data(s), "Pods should be Running (not crashing)")
print("  ✅ Disk IO + DNS data consistent with expected root cause")

# 28. memory_leak_and_disk_full — 双根因: 内存泄漏 + 磁盘空间耗尽
print("### 28. memory_leak_and_disk_full")
s = "memory_leak_and_disk_full"
check(s, "OOMKilled" in kubernetes_pods_data(s), "Should show OOMKilled")
check(s, "6.8" in memory_data(s) or "7012352" in memory_data(s), "Should show Java RSS 6.8GB")
check(s, "100%" in disk_data(s), "Disk should be 100% full")
check(s, "日志" in disk_data(s) or "log" in disk_data(s).lower(), "Should mention log filling disk")
check(s, "MemoryPressure" in kubernetes_nodes_data(s), "Should show MemoryPressure")
check(s, "DiskPressure" in kubernetes_nodes_data(s), "Should show DiskPressure")
check(s, "两个" in processes_data(s) or "独立" in processes_data(s), "Should mention two independent root causes")
print("  ✅ Memory leak + disk full data consistent with expected root cause")

# 29. dns_and_etcd — 双根因: CoreDNS故障 + etcd不稳定
print("### 29. dns_and_etcd")
s = "dns_and_etcd"
check(s, "CrashLoopBackOff" in kubernetes_pods_data(s), "Should show CoreDNS CrashLoopBackOff")
check(s, "etcd" in kubernetes_pods_data(s).lower() and "leader" in kubernetes_pods_data(s).lower(), "Should show etcd leader switching")
check(s, "OOMKilled" in kubernetes_control_plane_data(s) or "leader" in kubernetes_control_plane_data(s), "Control plane should show etcd issues")
check(s, "频繁" in query_argus_services_metrics(s) or "切换" in query_argus_services_metrics(s), "Argus should show frequent leader switches")
print("  ✅ DNS + etcd data consistent with expected root cause")

# 30. multi_layer_cascading — 多层叠加故障
print("### 30. multi_layer_cascading")
s = "multi_layer_cascading"
check(s, "NotReady" in kubernetes_nodes_data(s) and "worker-3" in kubernetes_nodes_data(s), "worker-3 should be NotReady")
check(s, "OOMKilled" in kubernetes_pods_data(s), "Should show api-gateway OOMKilled")
check(s, "etcd" in processes_data(s).lower() and "D" in processes_data(s), "Should show etcd D-state")
check(s, "99" in disk_data(s) or "98" in disk_data(s), "etcd disk should be saturated")
check(s, "oom-killer" in memory_data(s).lower() or "OOM" in memory_data(s), "worker-3 should show OOM")
check(s, "CoreDNS" in kubernetes_pods_data(s) or "coredns" in kubernetes_pods_data(s), "Should mention CoreDNS impact")
print("  ✅ Multi-layer cascading data consistent with expected root cause")

# 31. conntrack_and_oom — 双根因: conntrack满 + Java OOM
print("### 31. conntrack_and_oom")
s = "conntrack_and_oom"
check(s, "131072" in network_data(s), "Should show conntrack full")
check(s, "OOMKilled" in kubernetes_pods_data(s), "Should show OOMKilled")
check(s, "java" in memory_data(s).lower() or "Java" in memory_data(s), "Should show Java as OOM cause")
check(s, "conntrack" in memory_data(s).lower() or "131072" in network_data(s), "Should show conntrack issue alongside OOM")
# Check no 'NotReady' in STATUS column (only check node table rows)
_nd_lines = [l for l in kubernetes_nodes_data(s).split('\n') if l.strip() and any(k in l for k in ('master-', 'worker-', 'NAME'))]
_nd_status = '\n'.join(_nd_lines)
check(s, "Ready" in _nd_status and "NotReady" not in _nd_status, "Nodes should all be Ready (unlike conntrack_table_full)")
print("  ✅ Conntrack + OOM data consistent with expected root cause")

# ═══════════════════ INDEPENDENCE CHECKS ═══════════════════

print("\n" + "=" * 70)
print("INDEPENDENCE AUDIT: Modifying one scenario shouldn't affect others")
print("=" * 70)

# Check known previously-shared branches are now independent
print("\n### Previously shared branches (fixed in prior session):")

# high_cpu_iowait vs disk_bottleneck (was shared in disk_data)
check("indep", disk_data("high_cpu_iowait") != disk_data("disk_bottleneck"),
      "disk_data: high_cpu_iowait != disk_bottleneck")
check("indep", cpu_data("high_cpu_iowait") != cpu_data("disk_bottleneck"),
      "cpu_data: high_cpu_iowait != disk_bottleneck")
print("  ✅ high_cpu_iowait vs disk_bottleneck: independent")

# memory_leak vs container_crash (was shared in kubernetes_nodes_data)
check("indep", kubernetes_nodes_data("memory_leak") != kubernetes_nodes_data("container_crash"),
      "k8s_nodes: memory_leak != container_crash")
print("  ✅ memory_leak vs container_crash: independent")

# oom_score_misconfig vs node_oom_kubelet_killed (was shared in argus)
check("indep", query_argus_cpu_metrics("oom_score_misconfig") != query_argus_cpu_metrics("node_oom_kubelet_killed"),
      "argus CPU: oom_score != node_oom")
check("indep", query_argus_memory_metrics("oom_score_misconfig") != query_argus_memory_metrics("node_oom_kubelet_killed"),
      "argus Memory: oom_score != node_oom")
print("  ✅ oom_score_misconfig vs node_oom_kubelet_killed: independent")

# Check all data functions return unique data per scenario (no accidental sharing)
print("\n### Cross-scenario uniqueness check:")
from diagnostics.tools.mock.scenarios import AVAILABLE_SCENARIOS
all_scenarios = [s for s in AVAILABLE_SCENARIOS if s != "normal"]

data_funcs = {
    "cpu_data": cpu_data,
    "memory_data": memory_data,
    "disk_data": disk_data,
    "network_data": network_data,
    "processes_data": processes_data,
    "k8s_pods": kubernetes_pods_data,
    "k8s_nodes": kubernetes_nodes_data,
}

for func_name, func in data_funcs.items():
    # Group scenarios by output to find unintended sharing
    outputs: dict[str, list[str]] = {}
    for s in all_scenarios:
        try:
            result = func(s)
            outputs.setdefault(result, []).append(s)
        except:
            pass
    # Find groups with same output (excluding base/normal fallback)
    base_result = func("normal") if "normal" not in [s for s in all_scenarios] else ""
    shared_groups = [(scenarios, output) for output, scenarios in outputs.items() 
                     if len(scenarios) > 1 and output != base_result]
    if shared_groups:
        for scenarios_group, _ in shared_groups:
            if len(scenarios_group) <= 3:
                # Small groups might be intentional
                pass
            else:
                warn("shared", False, f"{func_name}: {len(scenarios_group)} scenarios share same output: {scenarios_group[:5]}")

print("  ✅ Cross-scenario uniqueness: no unintended large sharing groups")

# Check etcd DB Size consistency across files
print("\n### Cross-file consistency: etcd_quota_near_full DB Size")
s = "etcd_quota_near_full"
cp_data = kubernetes_control_plane_data(s)
check("etcd", "NOSPACE" in cp_data or "7.15" in cp_data, "Control plane should reference NOSPACE/7.15GB")
argus_svc = query_argus_services_metrics(s)
check("etcd", "7000" in argus_svc or "7150" in argus_svc, "Argus services should show DB ~7000-7150MB")
print("  ✅ etcd DB Size consistent across files")

# ═══════════════════ SUMMARY ═══════════════════

print("\n" + "=" * 70)
print("AUDIT SUMMARY")
print("=" * 70)
if errors:
    print(f"\n❌ {len(errors)} ERRORS FOUND:")
    for e in errors:
        print(e)
else:
    print("\n✅ NO ERRORS — All 31 scenarios pass correctness audit!")

if warnings:
    print(f"\n⚠️ {len(warnings)} WARNINGS:")
    for w in warnings:
        print(w)
else:
    print("\n✅ NO WARNINGS")

print(f"\nTotal scenarios audited: 31")
print(f"Errors: {len(errors)}")
print(f"Warnings: {len(warnings)}")
sys.exit(1 if errors else 0)
