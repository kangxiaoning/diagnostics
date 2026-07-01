"""Diagnose the 3 routing conflicts."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from diagnostics.tools.mock.scenarios import AVAILABLE_SCENARIOS, match_scenario

pairs = [
    ("kubelet_disk_io_starvation", "conntrack_table_full"),
    ("oom_score_misconfig", "node_oom_kubelet_killed"),
    ("swap_thrashing", "high_cpu_iowait"),
]

for expected, actual in pairs:
    sc_e = AVAILABLE_SCENARIOS[expected]
    sc_a = AVAILABLE_SCENARIOS[actual]
    print(f"\n{'='*70}")
    print(f"冲突: {expected} 的 user_message 被 {actual} 抢先匹配")
    print(f"{'='*70}")

    print(f"\n期望场景: {expected}")
    print(f"  user_message: {sc_e.user_message}")
    print(f"  keywords ({len(sc_e.keywords)} rules):")
    for kw in sc_e.keywords:
        print(f"    {kw}  (len={len(kw)})")

    print(f"\n抢先场景: {actual}")
    print(f"  user_message: {sc_a.user_message}")
    print(f"  keywords ({len(sc_a.keywords)} rules):")
    for kw in sc_a.keywords:
        print(f"    {kw}  (len={len(kw)})")

    # Find which rule of 'actual' matches 'expected's user_message
    msg = sc_e.user_message.lower()
    print(f"\n冲突分析 — actual 的哪些规则命中了 expected 的 user_message:")
    for kw in sc_a.keywords:
        hits = [(k, k in msg) for k in kw]
        all_hit = all(h for _, h in hits)
        if all_hit:
            print(f"  !! 规则 {kw} (len={len(kw)}) 全部命中")
        else:
            miss = [k for k, h in hits if not h]
            print(f"     规则 {kw} 未命中: 缺少 {miss}")

    # Also check what expected's own rules do
    print(f"\n自检 — expected 自身规则是否命中自己的 user_message:")
    for kw in sc_e.keywords:
        hits = [(k, k in msg) for k in kw]
        all_hit = all(h for _, h in hits)
        status = "HIT" if all_hit else "MISS"
        miss = [k for k, h in hits if not h]
        print(f"  [{status}] {kw}" + (f"  缺少: {miss}" if miss else ""))
