---
name: disk-io-diagnosis
description: >
  Diagnose disk IO saturation and storage latency. Use when iowait exceeds
  30%, disk util is near 100%, or applications report slow storage.
---

1. `check_disk()` — examine iostat: %util, await, svctm, aqu-sz
   - `%util > 90%` → near saturation
   - `await > 30ms` → high latency (HDD typical 5-10ms, SSD < 2ms)
   - `await >> svctm` → request queuing, NOT device speed issue
   - `aqu-sz > 1` → IO requests waiting in queue
2. Cross-reference with cpu-diagnosis:
   - If `check_cpu()` shows iowait > 30% + D-state processes → disk is bottleneck
   - If iowait low but await high → disk itself is slow, not system load
3. Check filesystem: `df` via check_disk output
   - `Use% > 85%` → space pressure risk
4. Identify type:
   - read-heavy: r/s >> w/s → reads are bottleneck
   - write-heavy: w/s >> r/s → writes are bottleneck
   - mixed: both high → general IO saturation
