---
name: memory-diagnosis
description: >
  Diagnose memory leaks, OOM risks, swap thrashing. Use when memory usage
  grows over time, swap increases, or processes are OOM-killed.
---

1. `get_os_mem_5s()` — examine free/available ratios, swap usage, MemAvailable
   - `Available < 10% total` → critical pressure
   - `swap si/so > 0` → active swapping
2. `get_os_cpu_ps_elf()` — sort by %MEM, compare RSS vs expected limits
   - `RSS >> JVM -Xmx` → native memory leak (DirectByteBuffer, JNI)
   - `RSS steady ~Xmx` → normal, may just need larger heap
3. If K8s context: `check_k8s_pods()` → check OOMKilled events
   - Compare pod memory limit vs actual peak usage
   - Check `check_k8s_nodes()` for MemoryPressure
4. Classify:
   - RSS growing unbounded → application memory leak
   - RSS steady, swap growing → just under-provisioned
   - K8s OOMKilled + limit < peak → increase resources.limits.memory
   - K8s OOMKilled + limit > peak in describe → genuine leak, not limit
