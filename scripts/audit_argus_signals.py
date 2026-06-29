"""Audit Argus data: does each scenario have explicit normal + abnormal signals?

The Coordinator first queries Argus (via host-argus-expert + k8s-argus-expert)
in the UNDERSTAND phase.  Correct normal/abnormal signals in Argus data are
critical for forming accurate initial hypotheses.  Host/K8s expert tools
(deep diagnosis) are called LATER in the VERIFY phase.
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from diagnostics.tools.mock.argus_data import (
    query_argus_cpu_metrics, query_argus_memory_metrics,
    query_argus_disk_metrics, query_argus_network_metrics,
    query_argus_nodes_metrics, query_argus_services_metrics,
)
from diagnostics.tools.mock.scenarios import AVAILABLE_SCENARIOS

ARGUS_FUNCTIONS = {
    "cpu": query_argus_cpu_metrics,
    "memory": query_argus_memory_metrics,
    "disk": query_argus_disk_metrics,
    "network": query_argus_network_metrics,
    "nodes": query_argus_nodes_metrics,
    "services": query_argus_services_metrics,
}

DEFAULT_OUTPUT = "Argus: 无异常数据返回（系统正常或指标不可用）"

ABNORMAL_KEYWORDS = [
    # Chinese
    "严重", "异常", "错误", "失败", "超时", "timeout",
    "OOM", "oom", "Killed", "killed", "NotReady",
    "CrashLoopBackOff", "ImagePullBackOff",
    "饱和", "100%", "99%", "满", "溢出", "overflow",
    "丢包", "dropped", "重传", "retransmits",
    "泄漏", "leak", "blocked", "Unhealthy", "unhealthy",
    "突变", "飙升", "暴增", "劣化",
    # English / mixed
    "WARNING", "CRITICAL", "unavailable",
    "refused", "denied", "abort",
]

print("=" * 85)
print("ARGUS DATA: NORMAL vs ABNORMAL SIGNALS PER SCENARIO")
print("Key: 🔴=abnormal  ✅=normal/specified  ·=default/not-available")
print("=" * 85)
print(f"{'Scenario':<28} {'Entity':>8} |", end="")
for name in ARGUS_FUNCTIONS:
    print(f" {name:>7}", end="")
print(" | Gaps")
print("-" * 85)

scenario_list = [s for s in AVAILABLE_SCENARIOS if s != "normal"]
total_gaps = 0
scenario_gaps: dict[str, list[str]] = {}

for sid in scenario_list:
    sc = AVAILABLE_SCENARIOS[sid]
    entity = sc.entity_type  # host or kubernetes
    label = sc.label[:26]

    # Determine expected abnormal subsystems for this scenario
    is_host = entity == "host"
    is_k8s = entity == "kubernetes"

    print(f"{label:<28} {entity:>8} |", end="")
    gaps = []

    for func_name, fn in ARGUS_FUNCTIONS.items():
        result = fn(sid)
        is_default = result == DEFAULT_OUTPUT
        has_abnormal = any(kw.lower() in result.lower() for kw in ABNORMAL_KEYWORDS)

        if is_default:
            symbol = "   ·   "
            gaps.append(func_name)
            total_gaps += 1
        elif has_abnormal:
            symbol = "  🔴  "
        else:
            symbol = "  ✅  "

        print(symbol, end="")

    print(f" | {len(gaps)}")
    if gaps:
        scenario_gaps[sid] = gaps

print("-" * 85)

# Analyze: for host scenarios, which Argus functions should always have data?
# For K8s scenarios, nodes/services should always have data
host_gaps_count = sum(1 for sid, g in scenario_gaps.items()
                      if AVAILABLE_SCENARIOS[sid].entity_type == "host")
k8s_gaps_count = sum(1 for sid, g in scenario_gaps.items()
                     if AVAILABLE_SCENARIOS[sid].entity_type == "kubernetes")

print(f"\nARGUS DATA GAP SUMMARY")
print(f"{'='*85}")
print(f"  Total gaps: {total_gaps} out of {len(scenario_list) * len(ARGUS_FUNCTIONS)} "
      f"({total_gaps / (len(scenario_list) * len(ARGUS_FUNCTIONS)) * 100:.1f}%)")

print(f"\nScenarios with Argus data gaps:")
for sid, gaps in sorted(scenario_gaps.items(), key=lambda x: len(x[1]), reverse=True):
    sc = AVAILABLE_SCENARIOS[sid]
    print(f"  {'⚠' if len(gaps) >= 3 else '·'} [{sid}] ({sc.entity_type}): "
          f"{len(gaps)} gaps → {', '.join(gaps)}")

print(f"\nCRITICAL GAPS (K8s scenarios missing nodes/services):")
critical = [(sid, gaps) for sid, gaps in scenario_gaps.items()
            if AVAILABLE_SCENARIOS[sid].entity_type == "kubernetes"
            and ("nodes" in gaps or "services" in gaps)]
if critical:
    for sid, gaps in critical:
        print(f"  🔴 [{sid}]: missing {[g for g in gaps if g in ('nodes', 'services')]}")
else:
    print("  ✅ All K8s scenarios have nodes + services Argus data")
