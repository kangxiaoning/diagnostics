"""Verify signal pattern: abnormal root-cause subsystem vs normal other subsystems.

For each scenario, check that:
- The root-cause subsystem shows ABNORMAL signals
- Unrelated subsystems show EXPLICITLY NORMAL signals (not DEFAULT/ambiguous)
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from diagnostics.tools.mock.data import (
    cpu_data, memory_data, disk_data, network_data, processes_data,
    kubernetes_pods_data, kubernetes_nodes_data, kubernetes_control_plane_data,
)
from diagnostics.tools.mock.argus_data import (
    query_argus_cpu_metrics, query_argus_memory_metrics,
    query_argus_disk_metrics, query_argus_network_metrics,
    query_argus_nodes_metrics, query_argus_services_metrics,
)
from diagnostics.tools.mock.scenarios import AVAILABLE_SCENARIOS

# ── Expected signal pattern per scenario ──
# "abnormal": subsystem(s) that should show abnormal signals
# "normal": subsystem(s) that should show explicit normal signals
# "irrelevant": subsystems where base/default is acceptable
EXPECTED_PATTERNS = {
    # ── ★ 简单 ──
    "memory_leak": {
        "abnormal": ["memory", "processes"],  # root cause: memory
        "normal": ["disk", "network", "cpu"],  # should show normal
    },
    "disk_bottleneck": {
        "abnormal": ["disk", "cpu"],  # root cause: disk IO → iowait
        "normal": ["memory", "network"],
    },
    "network_congestion": {
        "abnormal": ["network"],
        "normal": ["memory", "disk", "cpu"],
    },
    "container_crash": {
        "abnormal": ["k8s_pods"],  # pod-level OOMKilled
        "normal": ["k8s_nodes"],  # node should be healthy
    },
    "arp_cache_full": {
        "abnormal": ["network", "k8s_pods"],
        "normal": ["memory", "disk", "cpu"],
    },
    "mtu_mismatch": {
        "abnormal": ["network"],
        "normal": ["memory", "disk", "cpu"],
    },
    # ── ★★ 中等 ──
    "conntrack_table_full": {
        "abnormal": ["network", "k8s_nodes"],
        "normal": ["memory", "disk"],
    },
    "kubelet_disk_io_starvation": {
        "abnormal": ["disk", "k8s_nodes", "processes"],  # disk → kubelet D-state
        "normal": ["memory", "network"],
    },
    "node_oom_kubelet_killed": {
        "abnormal": ["memory", "k8s_nodes"],
        "normal": ["network"],
    },
    "cpu_throttle_probe_failure": {
        "abnormal": ["cpu", "processes", "k8s_pods"],
        "normal": ["memory", "disk", "network"],
    },
    "softirq_starvation": {
        "abnormal": ["cpu", "network"],
        "normal": ["memory", "disk"],
    },
    "tcp_accept_overflow": {
        "abnormal": ["network", "processes"],
        "normal": ["memory", "disk"],
    },
    "gpu_oom": {
        "abnormal": ["gpu"],
        "normal": ["memory", "disk", "network"],
    },
    "high_cpu_iowait": {
        "abnormal": ["cpu", "disk", "processes"],  # iowait → disk IO
        "normal": ["memory"],
    },
    "mixed_io_memory": {
        "abnormal": ["disk", "memory"],  # both problem
        "normal": ["network"],
    },
    "coredns_cache_poison": {
        "abnormal": ["network", "k8s_pods"],
        "normal": ["memory", "disk", "cpu"],
    },
    "etcd_quorum_loss": {
        "abnormal": ["k8s_control_plane", "k8s_pods"],
        "normal": ["memory", "network"],
    },
    "image_pull_backoff": {
        "abnormal": ["k8s_pods"],
        "normal": ["memory", "disk", "network", "cpu"],
    },
    "oom_score_misconfig": {
        "abnormal": ["processes", "k8s_nodes", "k8s_pods"],
        "normal": ["memory", "disk", "network"],
    },
    "disk_full_var_log": {
        "abnormal": ["disk"],
        "normal": ["memory", "network"],
    },
    "fs_inode_exhaustion": {
        "abnormal": ["disk"],
        "normal": ["memory", "network"],
    },
    "systemd_limit_nofile": {
        "abnormal": ["network", "processes"],
        "normal": ["memory", "disk"],
    },
    "ebpf_probe_overhead": {
        "abnormal": ["cpu", "processes"],
        "normal": ["memory", "disk", "network"],
    },
    "swap_thrashing": {
        "abnormal": ["memory", "cpu", "disk", "processes"],  # cascading
        "normal": [],
    },
    # ── ★★★ 困难 ──
    "stress_cpu_and_tc_loss": {
        "abnormal": ["cpu", "network", "processes"],  # dual root
        "normal": ["memory", "disk"],
    },
    "etcd_quota_near_full": {
        "abnormal": ["k8s_control_plane", "k8s_pods"],
        "normal": ["cpu", "memory"],
    },
    "disk_io_and_dns": {
        "abnormal": ["disk", "network"],
        "normal": ["memory"],
    },
    "memory_leak_and_disk_full": {
        "abnormal": ["memory", "disk", "k8s_nodes"],
        "normal": ["network"],
    },
    "dns_and_etcd": {
        "abnormal": ["k8s_pods", "k8s_control_plane"],
        "normal": ["cpu", "memory", "disk"],
    },
    "multi_layer_cascading": {
        "abnormal": ["disk", "memory", "processes", "k8s_nodes", "k8s_pods"],
        "normal": ["network"],
    },
    "conntrack_and_oom": {
        "abnormal": ["network", "memory"],
        "normal": ["disk", "cpu"],
    },
}

# Map subsystem names to data functions
SUB_TO_FUNC = {
    "cpu": cpu_data,
    "memory": memory_data,
    "disk": disk_data,
    "network": network_data,
    "processes": processes_data,
    "k8s_pods": kubernetes_pods_data,
    "k8s_nodes": kubernetes_nodes_data,
    "k8s_control_plane": kubernetes_control_plane_data,
}

# Base outputs for comparison
BASE = {name: fn("normal") for name, fn in SUB_TO_FUNC.items()}

ABNORMAL_KW = [
    "异常", "错误", "error", "失败", "超时", "timeout",
    "OOM", "Killed", "killed", "NotReady", "CrashLoopBackOff",
    "D state", "iowait", "饱和", "满", "溢出", "overflow",
    "丢包", "dropped", "retransmits", "重传",
    "RSS", "泄漏", "leak", "blocked", "Unhealthy",
    "ImagePullBackOff", "100%", "99%", "98%", "不足", "极度",
]

NORMAL_KW = [
    "正常", "without", "healthy", "Healthy",
    "No errors", "dropped=0", "No queue backlog",
]

print("=" * 90)
print("SIGNAL PATTERN VERIFICATION: abnormal root subsystem ≠ normal others")
print("=" * 90)

errors = []
warnings = []
ok_count = 0

scenario_list = [s for s in AVAILABLE_SCENARIOS if s != "normal"]

for sid in scenario_list:
    pattern = EXPECTED_PATTERNS.get(sid)
    if not pattern:
        continue

    sc = AVAILABLE_SCENARIOS[sid]
    abnormal_subs = set(pattern["abnormal"])
    normal_subs = set(pattern["normal"])

    scenario_ok = True

    for sub_name in abnormal_subs:
        if sub_name not in SUB_TO_FUNC:
            continue
        fn = SUB_TO_FUNC[sub_name]
        result = fn(sid)
        is_base = result == BASE[sub_name]
        has_abnormal = any(kw.lower() in result.lower() for kw in ABNORMAL_KW)

        if is_base:
            errors.append(f"  ❌ [{sid}] {sub_name}: expected ABNORMAL, got DEFAULT (base)")
            scenario_ok = False
        elif not has_abnormal:
            warnings.append(f"  ⚠ [{sid}] {sub_name}: has dedicated data but no clear abnormal keywords")

    for sub_name in normal_subs:
        if sub_name not in SUB_TO_FUNC:
            continue
        fn = SUB_TO_FUNC[sub_name]
        result = fn(sid)
        is_base = result == BASE[sub_name]
        has_abnormal = any(kw.lower() in result.lower() for kw in ABNORMAL_KW)

        if is_base:
            warnings.append(f"  ⚠ [{sid}] {sub_name}: expected NORMAL, got DEFAULT (base)")
        elif has_abnormal:
            errors.append(f"  ❌ [{sid}] {sub_name}: expected NORMAL but contains abnormal keywords")
            scenario_ok = False

    if scenario_ok:
        ok_count += 1

print(f"\nResults: {ok_count}/{len(scenario_list)} scenarios have correct signal pattern\n")

if errors:
    print(f"❌ {len(errors)} ERRORS (wrong signal in expected subsystem):")
    for e in errors:
        print(e)
else:
    print("✅ NO ERRORS — All abnormal subsystems show abnormal, normal show normal")

print()
if warnings:
    print(f"⚠ {len(warnings)} WARNINGS (explicit vs default):")
    for w in warnings:
        print(w)
else:
    print("✅ NO WARNINGS — All normal subsystems have explicit normal data")
