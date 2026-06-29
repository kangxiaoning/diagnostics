"""Trace concrete signal pattern for memory_leak and disk_bottleneck."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from diagnostics.tools.mock.data import cpu_data, memory_data, disk_data, network_data, processes_data

for sid, label in [("memory_leak", "OOM 内存泄漏"), ("disk_bottleneck", "磁盘IO瓶颈")]:
    print(f"\n{'='*65}")
    print(f"SCENARIO: {sid} ({label})")
    print("  预期: 根因子系统 🔴  其他子系统 ✅")
    print(f"{'='*65}")

    for name, fn in [("CPU", cpu_data), ("Memory", memory_data),
                     ("Disk", disk_data), ("Network", network_data),
                     ("Processes", processes_data)]:
        result = fn(sid)
        shown = False
        for line in result.split("\n"):
            stripped = line.strip()
            if not stripped:
                continue
            # Find the most diagnostic line
            diag_words = [
                "RSS", "Swap", "AnonPages", "iowait", "await",
                "util", "dropped", "retransmits", "OOM", "leak",
                "D state", "饱和", "说明:",
            ]
            is_diag = any(w.lower() in stripped.lower() for w in diag_words)
            is_problem = any(w in stripped for w in [
                "异常", "OOM", "leak", "6.2GB", "1.5Gi used",
                "Full GC", "饱和!", "iowait=", "D state",
                "99.2", "98.5", "D state",
            ])
            if is_diag or ("util" in stripped and "%" in stripped):
                symbol = "🔴" if is_problem else "✅"
                print(f"  {symbol} [{name:<9}] {stripped[:110]}")
                shown = True
                break
        if not shown:
            # Show first non-architecture line
            for line in result.split("\n"):
                stripped = line.strip()
                if stripped and not stripped.startswith("Architecture") and not stripped.startswith("NUMA"):
                    print(f"  ✅ [{name:<9}] {stripped[:110]}")
                    break
    print()
