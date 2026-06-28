#!/usr/bin/env python3
"""Verify mock data completeness — flag scenarios with insufficient differentiation.

Practical validation:
  1. Each scenario must have ≥3 core data sources differing from "normal" default.
  2. check_dmesg() must have scenario-specific data for scenarios with kernel-level symptoms.
  3. check_conntrack() must have data for conntrack-related scenarios.

Usage:
    PYTHONPATH=. python3 scripts/verify_scenario_coverage.py
"""

from __future__ import annotations

import sys

sys.path.insert(0, ".")

from diagnostics.tools.mock.scenarios import AVAILABLE_SCENARIOS, set_scenario

from diagnostics.tools.mock.data import (
    _load_avg, cpu_data, memory_data, disk_data, network_data, processes_data,
    kubernetes_pods_data, kubernetes_nodes_data, kubernetes_control_plane_data,
    gpu_health_data, gpu_memory_data, gpu_utilization_data,
)
from diagnostics.tools.mock.argus_data import (
    query_argus_cpu_metrics, query_argus_memory_metrics, query_argus_disk_metrics,
    query_argus_network_metrics, query_argus_k8s_metrics,
)
from diagnostics.tools.mock.hosts import check_conntrack, check_dmesg


# ═══════════════════════════════════════════════════════════════════
# All host-level data sources (called with scenario ID)
# ═══════════════════════════════════════════════════════════════════

ALL_DATA_FUNCS: list[tuple[str, callable]] = [
    ("cpu_data", cpu_data),
    ("memory_data", memory_data),
    ("disk_data", disk_data),
    ("network_data", network_data),
    ("processes_data", processes_data),
    ("_load_avg", _load_avg),
    ("k8s_pods", kubernetes_pods_data),
    ("k8s_nodes", kubernetes_nodes_data),
    ("k8s_control_plane", kubernetes_control_plane_data),
    ("gpu_health", gpu_health_data),
    ("gpu_memory", gpu_memory_data),
    ("gpu_utilization", gpu_utilization_data),
]

ALL_ARGUS_FUNCS: list[tuple[str, callable]] = [
    ("argus.cpu", query_argus_cpu_metrics),
    ("argus.memory", query_argus_memory_metrics),
    ("argus.disk", query_argus_disk_metrics),
    ("argus.network", query_argus_network_metrics),
    ("argus.k8s", query_argus_k8s_metrics),
]

# Scenarios where check_dmesg MUST have specific data (kernel-level symptoms)
DMESG_REQUIRED_SCENARIOS = {
    "conntrack_table_full", "conntrack_and_oom", "memory_leak_and_disk_full",
    "node_oom_kubelet_killed", "multi_layer_cascading", "oom_score_misconfig",
    "softirq_starvation", "arp_cache_full", "stress_cpu_and_tc_loss",
    "disk_full_var_log", "fs_inode_exhaustion", "systemd_limit_nofile",
    "etcd_quota_near_full", "kubelet_disk_io_starvation",
}

# Scenarios where check_conntrack MUST have specific data
CONNTRACK_REQUIRED_SCENARIOS = {
    "conntrack_table_full", "conntrack_and_oom", "etcd_quota_near_full",
}

# Minimum number of data sources that must differ from "normal"
MIN_DIFF_SOURCES = 3


def _invoke_tool(func: callable) -> str:
    try:
        return func.invoke({})
    except Exception:
        return ""


def _differs(result: str, baseline: str) -> bool:
    return result.strip() != baseline.strip()


def main() -> None:
    scenarios = {
        sid: sc for sid, sc in AVAILABLE_SCENARIOS.items() if sid != "normal"
    }

    # Compute baselines
    set_scenario("normal")
    baselines_data = {lbl: func("normal") for lbl, func in ALL_DATA_FUNCS}
    baselines_argus = {lbl: func("normal") for lbl, func in ALL_ARGUS_FUNCS}
    baseline_dmesg = _invoke_tool(check_dmesg)
    baseline_conntrack = _invoke_tool(check_conntrack)

    errors: list[str] = []
    warnings: list[str] = []
    summary: dict[str, tuple[int, int]] = {}  # sid -> (covered, total)

    for sid, sc in scenarios.items():
        set_scenario(sid)
        covered = 0
        total = 0
        all_sources: list[tuple[str, bool]] = []

        # Check data functions
        for lbl, func in ALL_DATA_FUNCS:
            result = func(sid)
            is_diff = _differs(result, baselines_data[lbl])
            total += 1
            if is_diff:
                covered += 1
            all_sources.append((lbl, is_diff))

        # Check argus functions
        for lbl, func in ALL_ARGUS_FUNCS:
            result = func(sid)
            is_diff = _differs(result, baselines_argus[lbl])
            total += 1
            if is_diff:
                covered += 1
            all_sources.append((lbl, is_diff))

        # Check tools
        dmesg_result = _invoke_tool(check_dmesg)
        dmesg_diff = _differs(dmesg_result, baseline_dmesg)
        total += 1
        if dmesg_diff:
            covered += 1
        all_sources.append(("check_dmesg", dmesg_diff))

        conntrack_result = _invoke_tool(check_conntrack)
        conntrack_diff = _differs(conntrack_result, baseline_conntrack)
        total += 1
        if conntrack_diff:
            covered += 1
        all_sources.append(("check_conntrack", conntrack_diff))

        summary[sid] = (covered, total)

        # ── Validation rules ──

        # Rule 1: Minimum differentiation
        if covered < MIN_DIFF_SOURCES:
            errors.append(
                f"  {sid}: only {covered}/{total} sources differ from normal "
                f"(minimum {MIN_DIFF_SOURCES} required)"
            )

        # Rule 2: check_dmesg for kernel-level scenarios
        if sid in DMESG_REQUIRED_SCENARIOS and not dmesg_diff:
            errors.append(
                f"  {sid}: check_dmesg() returns default data "
                f"(kernel-level symptoms expected)"
            )

        # Rule 3: check_conntrack for conntrack scenarios
        if sid in CONNTRACK_REQUIRED_SCENARIOS and not conntrack_diff:
            errors.append(
                f"  {sid}: check_conntrack() returns default data "
                f"(conntrack-related scenario)"
            )

        # Warning: scenarios with >50% default sources
        pct = covered / total * 100
        if pct < 30 and covered >= MIN_DIFF_SOURCES:
            diff_names = [lbl for lbl, d in all_sources if d]
            warnings.append(
                f"  {sid}: {covered}/{total} ({pct:.0f}%) — "
                f"only: {', '.join(diff_names)}"
            )

    set_scenario("normal")

    # ── Report ──
    print(f"\n{'='*70}")
    print("SCENARIO DATA COVERAGE REPORT")
    print(f"{'='*70}\n")

    if errors:
        print(f"❌ ERRORS ({len(errors)}):\n")
        for e in errors:
            print(e)
        print()
    else:
        print("✅ No errors — all scenarios pass validation rules!\n")

    if warnings:
        print(f"⚠️  WARNINGS ({len(warnings)} scenarios with <30% coverage):\n")
        for w in warnings:
            print(w)
        print()

    # Per-scenario summary
    print(f"{'='*70}")
    print("PER-SCENARIO SUMMARY:\n")
    for sid in scenarios:
        covered, total = summary[sid]
        pct = covered / total * 100
        status = "✅" if pct >= 50 else "⚠️" if pct >= 30 else "❌"
        print(f"  {status} {sid:<40s} {covered:2d}/{total:2d} ({pct:4.0f}%)")

    print(f"\n{'='*70}")
    if errors:
        print(f"RESULT: FAIL ({len(errors)} errors)")
        sys.exit(1)
    else:
        print(f"RESULT: PASS")


if __name__ == "__main__":
    main()
