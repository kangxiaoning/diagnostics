"""Extract all scenario mock data for analysis.
Reads each scenario's SPEC dict and outputs structured data."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from diagnostics.tools.mock.loader import get_all_specs

# Mapping from test-scenarios-full.md scenario number to scenario ID
SCENARIO_MAP = [
    (1, "memory_leak"),
    (2, "disk_bottleneck"),
    (3, "network_congestion"),
    (4, "container_crash"),
    (5, "arp_cache_full"),
    (6, "mtu_mismatch"),
    (7, "conntrack_table_full"),
    (8, "kubelet_disk_io_starvation"),
    (9, "node_oom_kubelet_killed"),
    (10, "cpu_throttle_probe_failure"),
    (11, "softirq_starvation"),
    (12, "tcp_accept_overflow"),
    (13, "gpu_oom"),
    (14, "high_cpu_iowait"),
    (15, "mixed_io_memory"),
    (16, "coredns_cache_poison"),
    (17, "etcd_quorum_loss"),
    (18, "image_pull_backoff"),
    (19, "oom_score_misconfig"),
    (20, "disk_full_var_log"),
    (21, "fs_inode_exhaustion"),
    (22, "systemd_limit_nofile"),
    (23, "ebpf_probe_overhead"),
    (24, "swap_thrashing"),
    (25, "stress_cpu_and_tc_loss"),
    (26, "etcd_quota_near_full"),
    (27, "disk_io_and_dns"),
    (28, "memory_leak_and_disk_full"),
    (29, "dns_and_etcd"),
    (30, "multi_layer_cascading"),
    (31, "conntrack_and_oom"),
]

# Fields to extract
DATA_FIELDS = [
    "load_avg", "cpu", "memory", "disk", "network", "processes",
    "dmesg", "conntrack", "gpu_health", "gpu_memory", "gpu_utilization",
    "k8s_pods", "k8s_nodes", "k8s_control_plane",
]
ARGUS_FIELDS = [
    "argus_cpu", "argus_memory", "argus_disk", "argus_network",
    "argus_nodes", "argus_services",
]
LOG_FIELDS = ["pod_logs", "pod_describe"]

def extract_scenario(num, sid, specs, outfile):
    spec = specs.get(sid)
    if not spec:
        outfile.write(f"\n{'='*80}\n")
        outfile.write(f"场景 {num}: {sid} — ❌ 未找到 SPEC\n")
        outfile.write(f"{'='*80}\n\n")
        return

    outfile.write(f"\n{'='*80}\n")
    outfile.write(f"场景 {num}: {sid}\n")
    outfile.write(f"{'='*80}\n")
    outfile.write(f"ID: {spec.get('id', sid)}\n")
    outfile.write(f"Label: {spec.get('label', '')}\n")
    outfile.write(f"Description: {spec.get('description', '')}\n")
    outfile.write(f"Entity Type: {spec.get('entity_type', '')}\n")
    outfile.write(f"User Message: {spec.get('user_message', '')}\n")
    outfile.write(f"Keywords: {spec.get('keywords', [])}\n\n")

    outfile.write(f"--- Host Data ---\n")
    for field in DATA_FIELDS:
        val = spec.get(field, "")
        if isinstance(val, str):
            outfile.write(f"\n[{field}]\n{val}\n")
        elif callable(val):
            outfile.write(f"\n[{field}]\n{val()}\n")
        else:
            outfile.write(f"\n[{field}]\n{val}\n")

    outfile.write(f"\n--- Pod Logs & Describe ---\n")
    for field in LOG_FIELDS:
        val = spec.get(field, "")
        if isinstance(val, str):
            outfile.write(f"\n[{field}]\n{val}\n")
        elif callable(val):
            outfile.write(f"\n[{field}]\n{val()}\n")

    outfile.write(f"\n--- Argus Time Series ---\n")
    for field in ARGUS_FIELDS:
        val = spec.get(field, "")
        if isinstance(val, str):
            outfile.write(f"\n[{field}]\n{val}\n")
        elif callable(val):
            outfile.write(f"\n[{field}]\n{val()}\n")

    outfile.write(f"\n")

def main():
    specs = get_all_specs()
    out_path = Path(__file__).resolve().parent.parent / "private" / "mock-comparison" / "_all_scenario_data.txt"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w") as f:
        f.write("All Scenario Mock Data Extract\n")
        f.write(f"Total scenarios in loader: {len(specs)}\n\n")
        for num, sid in SCENARIO_MAP:
            extract_scenario(num, sid, specs, f)
    print(f"Extracted {len(SCENARIO_MAP)} scenarios to {out_path}")

if __name__ == "__main__":
    main()
