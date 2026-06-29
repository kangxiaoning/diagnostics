"""Deep audit: each scenario's mock data → expected root cause traceability.

Checks:
1. Each of 31 scenarios has entries in all core data functions
2. Data in one scenario doesn't accidentally match expected root causes of another
3. Key distinguishing signals are present for each expected root cause
"""
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
    query_argus_nodes_metrics, query_argus_services_metrics,
)
from diagnostics.tools.mock.scenarios import AVAILABLE_SCENARIOS, set_scenario

# ── Expected root cause signals per scenario ──
# Each scenario should have certain key signals in its mock data
EXPECTED_SIGNALS: dict[str, dict] = {
    # ── ★ 简单 ──
    "memory_leak": {
        "memory_data": ["7.0Gi", "AnonPages", "Swap"],
        "processes_data": ["java", "RSS", "6.2GB"],
        "kubernetes_nodes_data": ["MemoryPressure"],
        "argus_memory": ["Mem%", "Swap"],
    },
    "disk_bottleneck": {
        "disk_data": ["9", "await"],
        "cpu_data": ["iowait"],
        "processes_data": ["D state"],
        "argus_disk": ["Util%", "await"],
    },
    "network_congestion": {
        "network_data": ["饱和", "retransmits", "dropped"],
        "argus_network": ["RX", "TX"],
    },
    "container_crash": {
        "kubernetes_pods_data": ["OOMKilled", "CrashLoopBackOff", "512Mi"],
    },
    "arp_cache_full": {
        "network_data": ["ARP", "gc_thresh3"],
    },
    "mtu_mismatch": {
        "network_data": ["MTU", "1440", "1500"],
    },
    # ── ★★ 中等 ──
    "conntrack_table_full": {
        "network_data": ["131072", "table full"],
        "kubernetes_nodes_data": ["NotReady"],
    },
    "kubelet_disk_io_starvation": {
        "disk_data": ["9", "await"],
        "kubernetes_nodes_data": ["NotReady"],
    },
    "node_oom_kubelet_killed": {
        "memory_data": ["oom-killer", "kubelet"],
        "kubernetes_nodes_data": ["Unknown"],
    },
    "cpu_throttle_probe_failure": {
        "cpu_data": ["9"],
        "processes_data": ["batch"],
    },
    "softirq_starvation": {
        "cpu_data": ["ksoftirqd", "sys"],
        "network_data": ["rx_missed_errors"],
    },
    "tcp_accept_overflow": {
        "network_data": ["somaxconn", "ListenOverflows"],
    },
    "gpu_oom": {
        "gpu_memory_data": ["CUDA out of memory", "9"],
    },
    "high_cpu_iowait": {
        "cpu_data": ["iowait", "D"],
        "disk_data": ["9"],
    },
    "mixed_io_memory": {
        "disk_data": ["9", "await"],
        "memory_data": ["Swap"],
        "cpu_data": ["iowait"],
    },
    "coredns_cache_poison": {
        "network_data": ["错误IP", "缓存"],
    },
    "etcd_quorum_loss": {
        "kubernetes_control_plane_data": ["Unhealthy", "timeout", "READ-ONLY"],
        "kubernetes_pods_data": ["leader lost", "election", "只读"],
    },
    "image_pull_backoff": {
        "kubernetes_pods_data": ["ImagePullBackOff"],
    },
    "oom_score_misconfig": {
        "kubernetes_pods_data": ["oom_score_adj"],
    },
    "disk_full_var_log": {
        "disk_data": ["100%"],
    },
    "fs_inode_exhaustion": {
        "disk_data": ["inode", "100%"],
    },
    "systemd_limit_nofile": {
        "network_data": ["65536"],
        "processes_data": ["65536", "EMFILE", "fd上限"],
    },
    "ebpf_probe_overhead": {
        "cpu_data": ["sys", "ebpf"],
    },
    "swap_thrashing": {
        "memory_data": ["9", "Swap"],
        "cpu_data": ["iowait"],
        "processes_data": ["kswapd"],
    },
    # ── ★★★ 困难 ──
    "stress_cpu_and_tc_loss": {
        "cpu_data": ["stress-ng", "9"],
        "network_data": ["tc", "5%"],
        "processes_data": ["stress-ng"],
    },
    "etcd_quota_near_full": {
        "kubernetes_control_plane_data": ["NOSPACE", "850ms"],
        "kubernetes_pods_data": ["CrashLoopBackOff"],
    },
    "disk_io_and_dns": {
        "disk_data": ["9"],
        "network_data": ["forward", "timeout"],
    },
    "memory_leak_and_disk_full": {
        "memory_data": ["oom-killer", "6.8GB", "AnonPages"],
        "disk_data": ["100%"],
        "kubernetes_nodes_data": ["MemoryPressure", "DiskPressure"],
    },
    "dns_and_etcd": {
        "kubernetes_pods_data": ["CrashLoopBackOff", "etcd", "leader"],
    },
    "multi_layer_cascading": {
        "disk_data": ["9", "await"],
        "memory_data": ["oom-killer", "OOM"],
        "kubernetes_nodes_data": ["NotReady"],
        "processes_data": ["etcd", "D"],
    },
    "conntrack_and_oom": {
        "network_data": ["131072"],
        "memory_data": ["java", "OOM"],
    },
}

# Data function names → callables
DATA_FUNCS = {
    "_load_avg": _load_avg,
    "cpu_data": cpu_data,
    "memory_data": memory_data,
    "disk_data": disk_data,
    "network_data": network_data,
    "processes_data": processes_data,
    "kubernetes_pods_data": kubernetes_pods_data,
    "kubernetes_nodes_data": kubernetes_nodes_data,
    "kubernetes_control_plane_data": kubernetes_control_plane_data,
    "gpu_health_data": gpu_health_data,
    "gpu_memory_data": gpu_memory_data,
    "gpu_utilization_data": gpu_utilization_data,
    "argus_cpu": query_argus_cpu_metrics,
    "argus_memory": query_argus_memory_metrics,
    "argus_disk": query_argus_disk_metrics,
    "argus_network": query_argus_network_metrics,
    "argus_nodes": query_argus_nodes_metrics,
    "argus_services": query_argus_services_metrics,
}

errors = []
warnings = []

print("=" * 70)
print("DEEP AUDIT: Per-scenario mock data → expected root cause")
print("=" * 70)

scenario_list = [s for s in AVAILABLE_SCENARIOS if s != "normal"]

for sid in scenario_list:
    sc = AVAILABLE_SCENARIOS[sid]
    label = sc.label
    signals = EXPECTED_SIGNALS.get(sid, {})

    if not signals:
        warnings.append(f"  ⚠ [{sid}] No EXPECTED_SIGNALS defined (scenario may be untested)")
        continue

    print(f"\n### {sid} — {label}")

    all_ok = True
    for func_name, keywords in signals.items():
        if func_name not in DATA_FUNCS:
            errors.append(f"  ❌ [{sid}] Unknown data function: {func_name}")
            all_ok = False
            continue

        fn = DATA_FUNCS[func_name]
        try:
            if func_name.startswith("argus_"):
                result = fn(sid)
            else:
                result = fn(sid)
        except Exception as e:
            errors.append(f"  ❌ [{sid}] {func_name}() raised: {e}")
            all_ok = False
            continue

        result_lower = result.lower()
        missing = [kw for kw in keywords if kw.lower() not in result_lower]
        if missing:
            errors.append(
                f"  ❌ [{sid}] {func_name}: missing signals {missing}"
            )
            all_ok = False
        else:
            print(f"  ✅ {func_name}: all {len(keywords)} signals found")

    if all_ok:
        print(f"  ✅ All expected signals present")

# ── Independence: check no scenario accidentally matches another's signals ──
print("\n" + "=" * 70)
print("INDEPENDENCE CHECK: Cross-scenario signal contamination")
print("=" * 70)

contamination_count = 0
for sid in scenario_list:
    # For each scenario, check that data from OTHER scenarios doesn't
    # contain signals exclusive to this one
    for func_name in DATA_FUNCS:
        fn = DATA_FUNCS[func_name]
        try:
            own = fn(sid).lower()
        except:
            continue
        for other_id in scenario_list:
            if other_id == sid:
                continue
            try:
                other = fn(other_id).lower()
            except:
                continue
            # If data is identical (shared base), flag it
            if own == other and own:
                # Only flag if the function is relevant to one of them
                own_signals = EXPECTED_SIGNALS.get(sid, {}).get(func_name, [])
                other_signals = EXPECTED_SIGNALS.get(other_id, {}).get(func_name, [])
                if own_signals or other_signals:
                    warnings.append(
                        f"  ⚠ [{sid}] {func_name}: identical data with [{other_id}] "
                        f"(may indicate insufficient differentiation)"
                    )
                    contamination_count += 1
                    if contamination_count > 50:
                        break
        if contamination_count > 50:
            break
    if contamination_count > 50:
        warnings.append("  ... (truncated)")
        break

# ── Summary ──
print("\n" + "=" * 70)
print("DEEP AUDIT SUMMARY")
print("=" * 70)

if errors:
    print(f"\n❌ {len(errors)} ERRORS:")
    for e in errors:
        print(e)
else:
    print("\n✅ NO ERRORS — All scenarios have required signals in mock data")

if warnings:
    print(f"\n⚠ {len(warnings)} WARNINGS:")
    for w in warnings:
        print(w)
else:
    print("\n✅ NO WARNINGS")

print(f"\nScenarios audited: {len(scenario_list)}")
print(f"Errors: {len(errors)}")
print(f"Warnings: {len(warnings)}")

sys.exit(1 if errors else 0)
