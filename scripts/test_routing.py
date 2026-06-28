#!/usr/bin/env python3
"""Verify all 31 scenarios route correctly via declarative match_scenario()."""

import sys
sys.path.insert(0, "/Users/kangxiaoning/workspace/diagnostics")

from diagnostics.tools.mock.scenarios import AVAILABLE_SCENARIOS, match_scenario

# Collect scenarios that have user_message defined
passed = 0
failed = 0
failures: list[tuple[str, str, str, str]] = []

for sid, sc in AVAILABLE_SCENARIOS.items():
    if sid == "normal" or not sc.user_message:
        continue
    got = match_scenario(sc.user_message)
    if got == sid:
        passed += 1
        print(f"✅ {sid:<40s} → got: {got}")
    else:
        failed += 1
        failures.append((sid, got or "None", sc.user_message, str(sc.keywords)))
        print(f"❌ {sid:<40s} → got: {got}")

print(f"\n{'='*60}")
if failed == 0:
    print(f"PASSED: All {passed} scenarios routed correctly")
else:
    print(f"FAILED: {failed} mismatches")
    for expected, got, msg, kw in failures:
        print(f"  Expected: {expected}, Got: {got}")
        print(f"  Input: {msg}")
        print(f"  Keywords: {kw}")
    sys.exit(1)
