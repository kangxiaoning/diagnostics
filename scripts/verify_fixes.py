#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Verify all fixes for conntrack_and_oom scenario."""

# 1. Verify ledger_middleware delegation saturation fix
import inspect
import diagnostics.agent.ledger_middleware as lm

src = inspect.getsource(lm)
assert "_last_finding_round > 0" in src, "delegation saturation fix missing"
print("[PASS] ledger_middleware: delegation saturation fix present")

# 2. Verify argus_data.py summaries (no causal arrows)
from diagnostics.tools.mock.argus_data import (
    query_argus_cpu_metrics,
    query_argus_memory_metrics,
    query_argus_network_metrics,
    query_argus_nodes_metrics,
)

for name, fn in [
    ("CPU", query_argus_cpu_metrics),
    ("Memory", query_argus_memory_metrics),
    ("Network", query_argus_network_metrics),
    ("Nodes", query_argus_nodes_metrics),
]:
    r = fn("conntrack_and_oom")
    summary = r.split("\u6458\u8981")[1] if "\u6458\u8981" in r else r[-100:]
    # Allow range arrows like "2ms\u2192250ms" but reject causal chains between root causes
    import re
    causal = re.sub(r'\d+ms\u2192\d+ms', '', summary)
    assert "\u2192" not in causal, f"{name} summary has causal arrow: {summary[:80]}"
    print(f"[PASS] argus {name}: no causal chain in summary")

# 3. Verify data.py (no leading descriptions)
from diagnostics.tools.mock.data import cpu_data, memory_data, network_data, processes_data

c = cpu_data("conntrack_and_oom")
assert "堆外泄漏" not in c and "堆外内存泄漏" not in c, "cpu: diagnosis text found"
print("[PASS] data.py cpu_data: neutralized")

m = memory_data("conntrack_and_oom")
assert "堆外内存泄漏" not in m, "memory: diagnosis text found"
assert "两条独立根因" not in m, "memory: two-root hint found"
print("[PASS] data.py memory_data: neutralized")

import re

def check_explain_section(text, label):
    """Only check the explanation/summary section for causal arrows, not command output."""
    if "\u8bf4\u660e:" in text:
        explain = text.split("\u8bf4\u660e:")[1]
        assert "\u2192" not in explain, f"{label}: causal arrow in explain section"

n = network_data("conntrack_and_oom")
assert "\u4e24\u4e2a\u72ec\u7acb\u6839\u56e0" not in n, "network: two-root hint found"
check_explain_section(n, "network")
print("[PASS] data.py network_data: neutralized")

p = processes_data("conntrack_and_oom")
assert "\u5806\u5916\u6cc4\u6f0f" not in p, "processes: diagnosis text found"
check_explain_section(p, "processes")
print("[PASS] data.py processes_data: neutralized")

# 4. Verify other scenarios still work
scenarios = [
    "memory_leak", "container_crash", "conntrack_table_full",
    "disk_io_and_dns", "stress_cpu_and_tc_loss", "multi_layer_cascading",
    "high_cpu_iowait", "gpu_oom", "node_oom_kubelet_killed",
]
for sc in scenarios:
    cpu_data(sc)
    memory_data(sc)
    network_data(sc)
    processes_data(sc)
print(f"[PASS] {len(scenarios)} other scenarios: all callable without error")

print()
print("=" * 50)
print("ALL VERIFICATIONS PASSED")
