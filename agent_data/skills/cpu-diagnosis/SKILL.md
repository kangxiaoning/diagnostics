---
name: cpu-diagnosis
description: >
  Diagnose CPU bottlenecks: high load, abnormal utilization, iowait, D-state
  processes. Use when load avg exceeds 2× CPU cores or CPU usage patterns are
  anomalous.
---

1. `check_cpu()` — examine vmstat r/b columns, us/sy/wa/id percentages
2. Classify bottleneck:
   - `wa > 30%` → IO-bound, cross-reference with disk-io-diagnosis
   - `sy > us` → kernel overhead, check system calls / softirq
   - `r > CPU cores` → CPU saturated, check_processes() for top consumers
   - `b > 0` + wa not high → possible lock contention
3. `check_processes()` — count D-state processes
   - ≥2 D-state → strong IO bottleneck signal
   - high %CPU user mode → application compute bottleneck
4. If suspected IO: switch to disk-io-diagnosis skill
5. If suspected memory: switch to memory-diagnosis skill
