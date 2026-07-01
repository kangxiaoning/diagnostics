"""Verification script for the scenario decoupling refactor."""
import sys
from pathlib import Path

# Ensure project root is on sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

def test_scenarios():
    from diagnostics.tools.mock.scenarios import (
        AVAILABLE_SCENARIOS, match_scenario, set_scenario, get_active_scenario
    )
    assert len(AVAILABLE_SCENARIOS) == 32, f"Expected 32 scenarios, got {len(AVAILABLE_SCENARIOS)}"
    print(f"✅ Loaded {len(AVAILABLE_SCENARIOS)} scenarios")

    # Test set/get
    set_scenario("stress_cpu_and_tc_loss")
    assert get_active_scenario() == "stress_cpu_and_tc_loss"
    print("✅ set_scenario / get_active_scenario work")

    # Test match_scenario
    result = match_scenario("prod-web-01 上 api-gateway 从 15:03 开始间歇 504，CPU 打满")
    assert result == "stress_cpu_and_tc_loss", f"Expected stress_cpu_and_tc_loss, got {result}"
    print(f"✅ match_scenario correctly matched: {result}")

    # Test a few more matches
    r2 = match_scenario("conntrack 表满了，大量 insert_failed，连接被丢弃")
    print(f"  conntrack match: {r2}")

    r3 = match_scenario("GPU OOM，显存不足，训练任务被 kill")
    print(f"  gpu_oom match: {r3}")


def test_data():
    from diagnostics.tools.mock.scenarios import set_scenario, get_active_scenario
    from diagnostics.tools.mock.data import (
        cpu_data, memory_data, disk_data, network_data,
        processes_data, gpu_health_data, gpu_memory_data, gpu_utilization_data,
        kubernetes_pods_data, kubernetes_nodes_data, kubernetes_control_plane_data,
    )

    set_scenario("stress_cpu_and_tc_loss")
    s = get_active_scenario()

    for name, fn in [
        ("cpu_data", cpu_data), ("memory_data", memory_data),
        ("disk_data", disk_data), ("network_data", network_data),
        ("processes_data", processes_data),
        ("gpu_health_data", gpu_health_data), ("gpu_memory_data", gpu_memory_data),
        ("gpu_utilization_data", gpu_utilization_data),
        ("k8s_pods", kubernetes_pods_data), ("k8s_nodes", kubernetes_nodes_data),
        ("k8s_control_plane", kubernetes_control_plane_data),
    ]:
        result = fn(s)
        assert result and len(result) > 0, f"{name} returned empty for {s}"
        print(f"✅ {name}: {len(result)} chars")

    # Test normal fallback
    set_scenario("normal")
    s2 = get_active_scenario()
    for name, fn in [("cpu_data", cpu_data), ("memory_data", memory_data)]:
        result = fn(s2)
        assert result and len(result) > 0, f"{name} returned empty for normal"
    print("✅ normal fallback works")


def test_argus():
    from diagnostics.tools.mock.scenarios import set_scenario, get_active_scenario
    from diagnostics.tools.mock.argus_data import (
        query_argus_cpu_metrics, query_argus_memory_metrics,
        query_argus_disk_metrics, query_argus_network_metrics,
        query_argus_nodes_metrics, query_argus_services_metrics,
    )

    set_scenario("stress_cpu_and_tc_loss")
    s = get_active_scenario()

    for name, fn in [
        ("argus_cpu", query_argus_cpu_metrics),
        ("argus_memory", query_argus_memory_metrics),
        ("argus_disk", query_argus_disk_metrics),
        ("argus_network", query_argus_network_metrics),
        ("argus_nodes", query_argus_nodes_metrics),
        ("argus_services", query_argus_services_metrics),
    ]:
        result = fn(s)
        assert result and len(result) > 0, f"{name} returned empty for {s}"
        print(f"✅ {name}: {len(result)} chars")


def test_hosts():
    from diagnostics.tools.mock.hosts import (
        check_cpu, check_memory, check_disk, check_network,
        check_processes, check_conntrack, check_dmesg,
        get_system_overview, list_diagnostic_capabilities,
    )

    from diagnostics.tools.mock.scenarios import set_scenario
    set_scenario("stress_cpu_and_tc_loss")

    for name, fn in [
        ("check_cpu", check_cpu), ("check_memory", check_memory),
        ("check_disk", check_disk), ("check_network", check_network),
        ("check_processes", check_processes), ("check_conntrack", check_conntrack),
        ("check_dmesg", check_dmesg), ("get_system_overview", get_system_overview),
        ("list_diagnostic_capabilities", list_diagnostic_capabilities),
    ]:
        result = fn.invoke({})
        assert result and len(result) > 0, f"{name} returned empty"
        print(f"✅ {name}: {len(result)} chars")


def test_kubernetes():
    from diagnostics.tools.mock.kubernetes import (
        get_pod_logs, get_pod_previous_logs, describe_pod,
        get_cluster_overview, get_cluster_events,
    )
    from diagnostics.tools.mock.scenarios import set_scenario

    set_scenario("stress_cpu_and_tc_loss")

    result = get_cluster_overview.invoke({"cluster_name": "default"})
    assert result and len(result) > 0
    print(f"✅ get_cluster_overview: {len(result)} chars")

    result = get_pod_logs.invoke({
        "cluster_name": "default", "namespace": "default", "pod_name": "api-gateway"
    })
    assert result and len(result) > 0
    print(f"✅ get_pod_logs: {len(result)} chars")


if __name__ == "__main__":
    tests = [
        ("Scenarios", test_scenarios),
        ("Data", test_data),
        ("Argus", test_argus),
        ("Hosts", test_hosts),
        ("Kubernetes", test_kubernetes),
    ]

    failures = 0
    for name, fn in tests:
        print(f"\n{'='*60}")
        print(f"Testing {name}...")
        print(f"{'='*60}")
        try:
            fn()
        except Exception as e:
            print(f"❌ {name} FAILED: {e}")
            import traceback
            traceback.print_exc()
            failures += 1

    print(f"\n{'='*60}")
    if failures:
        print(f"❌ {failures} test group(s) failed!")
        sys.exit(1)
    else:
        print("🎉 All verification tests passed!")
