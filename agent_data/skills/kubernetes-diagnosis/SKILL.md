---
name: kubernetes-diagnosis
description: >
  Diagnose K8s workload issues: pod restarts, OOMKilled, node pressure,
  CrashLoopBackOff, resource quota problems. Use when pods are unstable
  or cluster health is questioned.
---

## General workflow

1. `check_kubernetes_pods(namespace)` — READY, STATUS, RESTARTS
2. `check_kubernetes_nodes()` — readiness, MemoryPressure, DiskPressure
3. Route based on status:
   - OOMKilled → follow OOMKilled workflow below
   - CrashLoopBackOff → check logs/describe for exit reason
   - Pending → resource or scheduling constraint
   - Running but high RESTARTS → probe failure or resource limit

## OOMKilled workflow

1. `check_kubernetes_pods()` → confirm OOMKilled, note memory limit
2. `check_kubernetes_nodes()` → check for MemoryPressure on affected node
3. `check_memory()` → distinguish node-level vs pod-level shortage
4. Compare: pod memory limit vs actual peak usage from describe output
5. Differentiate:
   - `peak > limit` → increase resources.limits.memory
   - node MemoryPressure=True → scale or evict
   - `peak < limit` but still OOM → leak in app, not limit issue

## Cross-references

- If MemoryPressure on node → also check memory-diagnosis
- If pod CPU throttled → also check cpu-diagnosis
- If persistent volume issues → check disk-io-diagnosis
