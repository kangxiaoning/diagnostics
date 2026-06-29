"""Find all scenarios without dedicated data in each data.py function."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from diagnostics.tools.mock.data import (
    _load_avg, cpu_data, memory_data, disk_data, network_data,
    processes_data, kubernetes_pods_data, kubernetes_nodes_data,
    kubernetes_control_plane_data,
    gpu_health_data, gpu_memory_data, gpu_utilization_data,
)
from diagnostics.tools.mock.scenarios import AVAILABLE_SCENARIOS

FUNCTIONS = {
    "_load_avg": _load_avg,
    "cpu_data": cpu_data,
    "memory_data": memory_data,
    "disk_data": disk_data,
    "network_data": network_data,
    "processes_data": processes_data,
    "k8s_pods": kubernetes_pods_data,
    "k8s_nodes": kubernetes_nodes_data,
    "k8s_control_plane": kubernetes_control_plane_data,
    "gpu_health": gpu_health_data,
    "gpu_memory": gpu_memory_data,
    "gpu_utilization": gpu_utilization_data,
}

scenarios = [s for s in AVAILABLE_SCENARIOS if s != "normal"]
# Compare against actual fallback output (for "normal" scenario), not raw base
base_outputs = {}
for name, fn in FUNCTIONS.items():
    try:
        # Use "___nonexistent___" to trigger the default fallback path
        base_outputs[name] = fn("___nonexistent___")
    except:
        base_outputs[name] = fn("normal")

# ── Find missing entries per function ──
missing: dict[str, list[str]] = {}
for func_name, fn in FUNCTIONS.items():
    base = base_outputs[func_name]
    missing[func_name] = []
    for sid in scenarios:
        output = fn(sid)
        if output == base:
            missing[func_name].append(sid)

print("Scenarios returning DEFAULT (base) per function:\n")
total = 0
for func_name, sids in sorted(missing.items(), key=lambda x: -len(x[1])):
    if sids:
        print(f"  {func_name} ({len(sids)} gaps): {', '.join(sids)}")
        total += len(sids)

print(f"\nTotal gaps: {total}")

# ── Show which functions need normal data for which entity types ──
print(f"\n{'='*60}")
print("HOST scenarios needing CPU/memory/disk/network data:")
for sid in scenarios:
    sc = AVAILABLE_SCENARIOS[sid]
    if sc.entity_type == "host":
        needed = [fn for fn in ["cpu_data","memory_data","disk_data","network_data"] if sid in missing[fn]]
        if needed:
            print(f"  {sid}: {needed}")

print(f"\nK8S scenarios needing CPU/memory/disk/network data (host layer is normal):")
for sid in scenarios:
    sc = AVAILABLE_SCENARIOS[sid]
    if sc.entity_type == "kubernetes":
        needed = [fn for fn in ["cpu_data","memory_data","disk_data","network_data"] if sid in missing[fn]]
        if needed:
            print(f"  {sid}: {needed}")

print(f"\nScenarios needing k8s data:")
for fn in ["k8s_pods","k8s_nodes","k8s_control_plane"]:
    if missing[fn]:
        print(f"  {fn}: {', '.join(missing[fn])}")
