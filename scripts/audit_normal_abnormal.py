"""Audit: Does each scenario have explicit normal + abnormal signals?

For each scenario, check every data function to determine if it returns:
- DEDICATED data (explicitly tailored for this scenario - either normal or abnormal)
- Or falls back to DEFAULT base (ambiguous: LLM can't tell "checked=normal" vs "not checked")

A well-formed scenario should have dedicated data for ALL core subsystems,
even if some subsystems are explicitly NORMAL (to rule out competing hypotheses).

The audit counts:
- ABNORMAL: dedicated data that clearly signals a problem
- NORMAL: dedicated data that explicitly states everything is fine
- DEFAULT: falls back to base (ambiguous)
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from diagnostics.tools.mock.data import (
    _load_avg, cpu_data, memory_data, disk_data, network_data,
    processes_data, kubernetes_pods_data, kubernetes_nodes_data,
    kubernetes_control_plane_data,
)
from diagnostics.tools.mock.scenarios import AVAILABLE_SCENARIOS

# Functions that are mandatory to have dedicated data per scenario
CORE_FUNCTIONS = {
    "cpu_data": cpu_data,
    "memory_data": memory_data,
    "disk_data": disk_data,
    "network_data": network_data,
    "processes_data": processes_data,
}

K8S_FUNCTIONS = {
    "k8s_pods": kubernetes_pods_data,
    "k8s_nodes": kubernetes_nodes_data,
    "k8s_control_plane": kubernetes_control_plane_data,
}

# For each (scenario, function), classify: ABNORMAL | NORMAL | DEFAULT
def classify(output: str, scenario: str, func_name: str, base: str) -> str:
    if output == base:
        return "DEFAULT"
    
    # Check if output contains explicit "normal" markers
    normal_markers = [
        "无异常", "正常", "within normal", "healthy", "Healthy",
        "No queue backlog", "No errors", "dropped=0",
        "0B used", "free", "2.1Gi used",  # base memory is low-usage
    ]
    
    # Check if output contains "abnormal" markers specific to this scenario
    abnormal_markers = [
        "异常", "错误", "error", "失败", "超时", "timeout",
        "OOM", "oom", "Killed", "killed", "NotReady",
        "CrashLoopBackOff", "ImagePullBackOff",
        "D state", "D-state", "iowait", "饱和", "100%",
        "满", "溢出", "overflow", "丢包", "dropped",
        "retransmits", "重传", "RSS=", "泄漏", "leak",
        "blocked", "Unhealthy", "unhealthy",
    ]
    
    is_abnormal = any(m.lower() in output.lower() for m in abnormal_markers)
    is_normal = any(m.lower() in output.lower() for m in normal_markers)
    
    if is_abnormal and is_normal:
        return "MIXED"  # has both normal and abnormal signals (e.g. multi-node)
    if is_abnormal:
        return "ABNORMAL"
    if is_normal:
        return "NORMAL"
    # Dedicated data but neither clearly normal nor abnormal
    return "DEDICATED"


print("=" * 85)
print("NORMAL vs ABNORMAL SIGNAL AUDIT")
print("=" * 85)
print(f"{'Scenario':<28} {'Entity':>8} |", end="")
for f in CORE_FUNCTIONS:
    print(f" {f[:6]:>6}", end="")
for f in K8S_FUNCTIONS:
    print(f" {f[:6]:>6}", end="")
print()

SYMBOLS = {"DEFAULT": " · ", "ABNORMAL": " 🔴", "NORMAL": " ✅", "MIXED": " 🔀", "DEDICATED": " ~ "}

# Summary counters
summary = {"ABNORMAL": 0, "NORMAL": 0, "DEFAULT": 0, "MIXED": 0, "DEDICATED": 0}
scenario_level_gaps: dict[str, list[str]] = {}

scenario_list = [s for s in AVAILABLE_SCENARIOS if s != "normal"]
base_outputs = {name: fn("normal") for name, fn in {**CORE_FUNCTIONS, **K8S_FUNCTIONS}.items()}

for sid in scenario_list:
    sc = AVAILABLE_SCENARIOS[sid]
    entity = sc.entity_type  # "host" or "kubernetes"
    label = sc.label[:26]
    
    print(f"{label:<28} {entity:>8} |", end="")
    
    gaps: list[str] = []
    
    for func_name, fn in CORE_FUNCTIONS.items():
        output = fn(sid)
        base = base_outputs[func_name]
        cls = classify(output, sid, func_name, base)
        summary[cls] += 1
        if cls == "DEFAULT":
            gaps.append(func_name)
        print(SYMBOLS[cls], end="")
    
    for func_name, fn in K8S_FUNCTIONS.items():
        output = fn(sid)
        base = base_outputs[func_name]
        cls = classify(output, sid, func_name, base)
        summary[cls] += 1
        if cls == "DEFAULT":
            gaps.append(func_name)
        print(SYMBOLS[cls], end="")
    
    print()
    
    if gaps:
        scenario_level_gaps[sid] = gaps

print()
print(f"{'='*85}")
print(f"SUMMARY (classifications across all scenarios × functions)")
print(f"{'='*85}")
for cls, count in sorted(summary.items(), key=lambda x: -x[1]):
    pct = count / (len(scenario_list) * (len(CORE_FUNCTIONS) + len(K8S_FUNCTIONS))) * 100
    print(f"  {SYMBOLS.get(cls, ' ?')} {cls}: {count} ({pct:.1f}%)")

print(f"\n{'='*85}")
print(f"SCENARIOS WITH DEFAULT (AMBIGUOUS) FUNCTIONS")
print(f"{'='*85}")

if scenario_level_gaps:
    for sid, gaps in sorted(scenario_level_gaps.items(), key=lambda x: len(x[1]), reverse=True):
        print(f"  ⚠ {sid}: {len(gaps)} gaps → {', '.join(gaps)}")
else:
    print("  ✅ All scenarios have dedicated data for all core functions!")
