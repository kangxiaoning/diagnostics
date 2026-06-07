---
name: system-health-check
description: >
  Quick system health assessment. Use when user reports general slowness,
  unknown symptoms, or wants a first-pass check. Calls get_system_overview
  then routes to subsystem skills based on findings.
---

1. `get_system_overview()` — check uptime, load average, kernel version
2. Route based on load:
   - load < 2× cores → normal, no urgent action
   - load > 2× cores → proceed to cpu-diagnosis skill
3. Route based on memory hints:
   - swap used > 0 or available < 10% → memory-diagnosis skill
4. Route based on user context:
   - mentions "K8s" or "pod" or "restart" → kubernetes-diagnosis skill
   - mentions "GPU" or "CUDA" or "training" → gpu-diagnosis skill
   - mentions "slow disk" or "IO" or "database timeout" → disk-io-diagnosis skill
   - mentions "network" or "timeout" or "connection" → network-diagnosis skill
5. If no clear signal, ask user for more specific symptoms before drilling down.
