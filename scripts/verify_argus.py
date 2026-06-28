"""Verify argus_data.py coverage after patching."""
import re, sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

all_scenarios = [
    'memory_leak', 'disk_bottleneck', 'network_congestion', 'container_crash',
    'arp_cache_full', 'mtu_mismatch', 'conntrack_table_full', 'kubelet_disk_io_starvation',
    'node_oom_kubelet_killed', 'cpu_throttle_probe_failure', 'softirq_starvation',
    'tcp_accept_overflow', 'gpu_oom', 'high_cpu_iowait', 'mixed_io_memory',
    'coredns_cache_poison', 'etcd_quorum_loss', 'image_pull_backoff', 'oom_score_misconfig',
    'disk_full_var_log', 'fs_inode_exhaustion', 'systemd_limit_nofile', 'ebpf_probe_overhead',
    'swap_thrashing', 'stress_cpu_and_tc_loss', 'etcd_quota_near_full', 'disk_io_and_dns',
    'memory_leak_and_disk_full', 'dns_and_etcd', 'multi_layer_cascading', 'conntrack_and_oom',
]

# Check argus_data.py file
with open('diagnostics/tools/mock/argus_data.py') as f:
    content = f.read()

missing = []
covered = []
for s in all_scenarios:
    pattern = '"' + re.escape(s) + '"'
    matches = re.findall(pattern, content)
    if matches:
        covered.append(s)
    else:
        missing.append(s)

print(f'argus_data.py: {len(covered)}/31 covered, {len(missing)} missing')
if missing:
    print(f'Missing: {missing}')

# Test actual data retrieval
from diagnostics.tools.mock.scenarios import set_scenario
from diagnostics.tools.mock.argus_data import (
    query_argus_cpu_metrics, query_argus_memory_metrics,
    query_argus_disk_metrics, query_argus_network_metrics,
    query_argus_k8s_metrics,
)

errors = []
for s in all_scenarios:
    set_scenario(s)
    try:
        cpu = query_argus_cpu_metrics(s)
        mem = query_argus_memory_metrics(s)
        disk = query_argus_disk_metrics(s)
        net = query_argus_network_metrics(s)
        k8s = query_argus_k8s_metrics(s)
        default_msg = '无异常数据返回'
        all_default = all(default_msg in r for r in [cpu, mem, disk, net, k8s])
        if all_default:
            errors.append(f'{s}: ALL subsystems returned default!')
    except Exception as e:
        errors.append(f'{s}: {e}')

if errors:
    print('\nErrors:')
    for e in errors:
        print(f'  {e}')
else:
    print('\nAll 31 scenarios return non-default data for at least one subsystem ✓')
