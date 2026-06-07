---
name: cross-layer-diagnosis
description: Diagnoses host-to-Kubernetes cascading failures — when physical/virtual machine resource issues (disk I/O starvation, OOM, CPU throttling, kernel packet drops) cause kubelet failures, node degradation, and pod-level anomalies. Use when pods are evicted without apparent pod-level cause, or when Node status flips to NotReady.
---

# Host → Kubernetes Cascading Failure Diagnosis

## Layer Model
```
  Host Layer (CPU / Memory / Disk / Network / Kernel)
      └── Container Runtime (containerd / docker)
             └── Kubelet (node heartbeat, pod lifecycle)
                    └── Pod (application process)
```

A failure at any layer cascades downward. The key principle: **always check the layer below**.

## Workflow

1. **check_kubernetes_nodes()** — Is the node NotReady/Unknown?
   - `Ready=Unknown && NodeStatusUnknown` → kubelet stopped posting → check host layer
   - `Ready=True` but pods failing → check kubelet events and host resources

2. **Identify the host-layer trigger using cluster info from node conditions:**
   - `DiskPressure=True` → **disk I/O starvation**: run `check_disk()`
   - `MemoryPressure=True` → **host memory shortage**: run `check_memory()`
   - `NetworkUnavailable=True` → **kernel networking**: run `check_network()`
   - All conditions `Unknown` → **kubelet dead/unreachable**: check `dmesg` in `check_processes()` and `check_memory()`

3. **Cross-reference** host data with pod symptoms:
   - Host iowait > 50% AND kubelet D-state → **Disk IO blocking kubelet lease**
   - Host mem near 100%, swap near full, OOM killer active → **System OOM killing kubelet**
   - Conntrack table full → **Kernel dropping pod networking packets**
   - Host CPU util > 90% with non-pod processes → **CPU starvation causing probe timeouts**

## Common Patterns

### Pattern 1: Disk IO → Kubelet Heartbeat Timeout
```
Symptom: Node NotReady, all conditions Unknown, pods evicted
Root:   Host disk %util 99%+, iowait 60%+
        Kubelet can't write lease file → heartbeat times out
        Containerd/kubelet in D-state (uninterruptible sleep)
Fix:    Reduce disk load, add dedicated kubelet disk, tune lease duration
```

### Pattern 2: System OOM → Kubelet Killed
```
Symptom: Node NotReady, kubelet not running, all pods Unknown
Root:   Non-pod process (e.g., host Java) consumes 6GB+ RSS
        System OOM killer terminates kubelet as part of cleanup
        Node goes dark — no status updates reach API server
Fix:    Limit host processes, set kubelet OOM score adjustment
```

### Pattern 3: Conntrack Table Full → Pod DNS/Network
```
Symptom: Pods running but DNS timeouts, connection refused
        Readiness/Liveness probe failures
Root:   `nf_conntrack: table full` in dmesg
        High connection count from pod-to-pod traffic
        UDP packets (DNS) silently dropped first
Fix:    Increase nf_conntrack_max, tune table expiration
```

### Pattern 4: CPU Starvation → Liveness Probe Timeout
```
Symptom: Pods restarting despite healthy app processes
        Events show "Liveness probe failed: timeout"
Root:   Host CPU 95%+ from batch jobs
        Kubelet can't execute probes within timeout period
        PLEG (Pod Lifecycle Event Generator) delayed
Fix:    Set CPU limits, add node anti-affinity, tune probe `timeoutSeconds`
```

## Summary
- Host-layer metrics always have priority over pod-level assumptions
- Kubelet is a single point of failure — protect its IO and CPU
- Cross-reference: a disk issue on one node should show consistently in `check_disk()` AND `check_processes()` AND `check_kubernetes_nodes()`
