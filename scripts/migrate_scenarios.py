#!/usr/bin/env python3
"""Generate per-scenario data files from existing mock data sources.

Run: python scripts/migrate_scenarios.py

This script reads all existing mock data (data.py, argus_data.py, kubernetes.py,
hosts.py) and generates one self-contained .py file per scenario under
diagnostics/tools/mock/scenarios/.
"""

import os
import sys
import textwrap

# Ensure project root is on path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from diagnostics.tools.mock.scenarios import AVAILABLE_SCENARIOS

# ── Import data sources ──
from diagnostics.tools.mock.data import (
    _load_avg,
    cpu_data,
    memory_data,
    disk_data,
    network_data,
    processes_data,
    kubernetes_pods_data,
    kubernetes_nodes_data,
    kubernetes_control_plane_data,
    gpu_health_data,
    gpu_memory_data,
    gpu_utilization_data,
)
from diagnostics.tools.mock.argus_data import (
    query_argus_cpu_metrics,
    query_argus_memory_metrics,
    query_argus_disk_metrics,
    query_argus_network_metrics,
    query_argus_nodes_metrics,
    query_argus_services_metrics,
)


def _get_k8s_inline(scenario_id: str) -> str:
    """Extract inline dict data from kubernetes.py."""
    try:
        from diagnostics.tools.mock import kubernetes as k8s_mod
        # Set the active scenario temporarily
        from diagnostics.tools.mock.scenarios import set_scenario
        old = None
        try:
            from diagnostics.tools.mock.scenarios import get_active_scenario
            old = get_active_scenario()
        except Exception:
            pass
        set_scenario(scenario_id)

        d = {}
        # _POD_LOGS
        logs = getattr(k8s_mod, "_POD_LOGS", {})
        if scenario_id in logs:
            d["pod_logs"] = logs[scenario_id]

        prev = getattr(k8s_mod, "_POD_PREVIOUS_LOGS", {})
        if scenario_id in prev:
            d["pod_previous_logs"] = prev[scenario_id]

        desc = getattr(k8s_mod, "_POD_DESCRIBE", {})
        if scenario_id in desc:
            d["pod_describe"] = desc[scenario_id]

        # Restore
        if old:
            set_scenario(old)
        return d
    except Exception as e:
        print(f"  Warning: k8s inline data for {scenario_id}: {e}")
        return {}


def _get_hosts_inline(scenario_id: str) -> dict:
    """Extract inline if/elif data from hosts.py (check_dmesg, check_conntrack)."""
    from diagnostics.tools.mock.scenarios import set_scenario, get_active_scenario
    old = None
    try:
        old = get_active_scenario()
    except Exception:
        pass
    set_scenario(scenario_id)

    d = {}
    try:
        from diagnostics.tools.mock.hosts import check_dmesg, check_conntrack
        # These are @tool wrapped, access the underlying function
        dmesg_fn = check_dmesg.func if hasattr(check_dmesg, 'func') else check_dmesg
        conntrack_fn = check_conntrack.func if hasattr(check_conntrack, 'func') else check_conntrack

        dmesg_result = dmesg_fn()
        if dmesg_result:
            d["dmesg"] = dmesg_result
        conntrack_result = conntrack_fn()
        if conntrack_result:
            d["conntrack"] = conntrack_result
    except Exception as e:
        print(f"  Warning: hosts inline data for {scenario_id}: {e}")

    if old:
        set_scenario(old)
    return d


def _escape_str(s: str) -> str:
    """Prepare a string for embedding in Python source."""
    # Use triple-quoted strings; escape any triple quotes in content
    s = s.replace("\\", "\\\\")
    s = s.replace('"""', '\\"\\"\\"')
    return s


def _gen_argus_callable(raw: str) -> str:
    """Generate a lambda source that returns the raw argus data."""
    escaped = _escape_str(raw)
    return f'lambda: """{escaped}"""'


def generate_scenario_file(scenario_id: str, scenario, output_dir: str) -> None:
    """Generate a single scenario .py file."""
    print(f"  Generating {scenario_id}...")

    # Collect host data
    load_avg = _load_avg(scenario_id)
    cpu = cpu_data(scenario_id)
    mem = memory_data(scenario_id)
    dsk = disk_data(scenario_id)
    net = network_data(scenario_id)
    proc = processes_data(scenario_id)

    # GPU data
    gpu_h = gpu_health_data(scenario_id)
    gpu_m = gpu_memory_data(scenario_id)
    gpu_u = gpu_utilization_data(scenario_id)

    # K8s data
    k8s_pods = kubernetes_pods_data(scenario_id)
    k8s_nodes = kubernetes_nodes_data(scenario_id)
    k8s_cp = kubernetes_control_plane_data(scenario_id)

    # K8s inline data
    k8s_inline = _get_k8s_inline(scenario_id)

    # Hosts inline data
    hosts_inline = _get_hosts_inline(scenario_id)

    # Argus data
    argus_cpu = query_argus_cpu_metrics(scenario_id)
    argus_mem = query_argus_memory_metrics(scenario_id)
    argus_disk = query_argus_disk_metrics(scenario_id)
    argus_net = query_argus_network_metrics(scenario_id)
    argus_nodes = query_argus_nodes_metrics(scenario_id)
    argus_svc = query_argus_services_metrics(scenario_id)

    # Build keywords repr
    kw_repr = repr(scenario.keywords)

    # Build the file content
    lines = []
    lines.append(f'"""Scenario: {scenario.label}"""')
    lines.append("")
    lines.append("SPEC = {")

    # Metadata
    lines.append(f'    "id": "{scenario_id}",')
    lines.append(f'    "label": "{_escape_str(scenario.label)}",')
    lines.append(f'    "description": "{_escape_str(scenario.description)}",')
    lines.append(f'    "entity_type": "{scenario.entity_type}",')
    lines.append(f'    "user_message": "{_escape_str(scenario.user_message)}",')
    lines.append(f'    "keywords": {kw_repr},')

    # Host data
    lines.append(f'    "load_avg": "{_escape_str(load_avg)}",')
    lines.append(f'    "cpu": """{_escape_str(cpu)}""",')
    lines.append(f'    "memory": """{_escape_str(mem)}""",')
    lines.append(f'    "disk": """{_escape_str(dsk)}""",')
    lines.append(f'    "network": """{_escape_str(net)}""",')
    lines.append(f'    "processes": """{_escape_str(proc)}""",')

    # dmesg / conntrack from hosts.py inline
    dmesg = hosts_inline.get("dmesg", "")
    conntrack = hosts_inline.get("conntrack", "")
    if dmesg:
        lines.append(f'    "dmesg": """{_escape_str(dmesg)}""",')
    if conntrack:
        lines.append(f'    "conntrack": """{_escape_str(conntrack)}""",')

    # GPU data
    lines.append(f'    "gpu_health": """{_escape_str(gpu_h)}""",')
    lines.append(f'    "gpu_memory": """{_escape_str(gpu_m)}""",')
    lines.append(f'    "gpu_utilization": """{_escape_str(gpu_u)}""",')

    # K8s data
    lines.append(f'    "k8s_pods": """{_escape_str(k8s_pods)}""",')
    lines.append(f'    "k8s_nodes": """{_escape_str(k8s_nodes)}""",')
    lines.append(f'    "k8s_control_plane": """{_escape_str(k8s_cp)}""",')

    # K8s inline
    if "pod_logs" in k8s_inline:
        lines.append(f'    "pod_logs": """{_escape_str(k8s_inline["pod_logs"])}""",')
    if "pod_previous_logs" in k8s_inline:
        lines.append(f'    "pod_previous_logs": """{_escape_str(k8s_inline["pod_previous_logs"])}""",')
    if "pod_describe" in k8s_inline:
        lines.append(f'    "pod_describe": """{_escape_str(k8s_inline["pod_describe"])}""",')

    # Argus data (as lambda callables)
    lines.append(f'    "argus_cpu": {_gen_argus_callable(argus_cpu)},')
    lines.append(f'    "argus_memory": {_gen_argus_callable(argus_mem)},')
    lines.append(f'    "argus_disk": {_gen_argus_callable(argus_disk)},')
    lines.append(f'    "argus_network": {_gen_argus_callable(argus_net)},')
    lines.append(f'    "argus_nodes": {_gen_argus_callable(argus_nodes)},')
    lines.append(f'    "argus_services": {_gen_argus_callable(argus_svc)},')

    lines.append("}")
    lines.append("")

    content = "\n".join(lines)

    filepath = os.path.join(output_dir, f"{scenario_id}.py")
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(content)

    print(f"  → {filepath} ({len(content)} bytes)")


def main():
    output_dir = os.path.join(
        os.path.dirname(__file__), "..",
        "diagnostics", "tools", "mock", "scenario_data",
    )
    os.makedirs(output_dir, exist_ok=True)

    print(f"Generating {len(AVAILABLE_SCENARIOS)} scenario files to {output_dir}/")

    for sid, scenario in AVAILABLE_SCENARIOS.items():
        generate_scenario_file(sid, scenario, output_dir)

    print(f"\nDone! Generated {len(AVAILABLE_SCENARIOS)} scenario files.")
    print("Next steps:")
    print("  1. Review generated files in diagnostics/tools/mock/scenarios/")
    print("  2. Refactor scenarios.py to use loader.get_all_specs()")
    print("  3. Refactor data.py / argus_data.py to use loader proxies")


if __name__ == "__main__":
    main()
