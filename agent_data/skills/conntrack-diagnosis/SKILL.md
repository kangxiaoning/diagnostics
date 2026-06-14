---
name: conntrack-diagnosis
description: "Diagnoses kernel connection tracking (conntrack) table saturation — when `nf_conntrack: table full` causes pod-to-pod networking failures, DNS timeouts, and connection drops. Use when pod networking fails but nodes show Ready."
---

# Conntrack Table Saturation Diagnosis

## Trigger
- dmesg shows `nf_conntrack: table full, dropping packet`
- Pods fail DNS lookups (CoreDNS UDP queries dropped by kernel)
- Pod-to-pod connections drop intermittently
- Nodes show Ready but `NetworkUnavailable=True` on affected nodes
- `conntrack -S` shows `insert_failed` counter growing
- Events show `connection refused` or `timeout` on service calls
- Multiple nodes affected simultaneously (shared conntrack exhaustion pattern)

## Workflow

Execute the following steps in order. Do NOT skip any step. Each step's output determines whether to continue to the next or escalate.

### Step 1: Verify conntrack saturation on affected nodes
- **check_network()** — examine dmesg for `nf_conntrack: table full, dropping packet`
- Confirm current conntrack count vs max (`sysctl net.netfilter.nf_conntrack_max`)
- **Expected for this scenario**: `nf_conntrack: table full (131072/131072 entries)` in dmesg

### Step 2: Assess cluster impact
- **check_kubernetes_nodes()** — verify `NetworkUnavailable=True` on worker-5 and worker-8
- **get_cluster_events()** — look for DNS timeout events, readiness probe failures
- **Expected**: NetworkUnavailable=True, ReadinessProbeFailed events citing DNS timeout

### Step 3: Identify high-connection sources
- **check_processes()** — find processes with unusually high ESTAB connection counts
- Look for: service mesh sidecars (envoy/istio-proxy) with 5000+ connections
- Look for: connection pool exhaustion in application logs
- **Expected for this scenario**: api-gateway or envoy sidecar with ESTAB 5000+ connections

### Step 4: Check CoreDNS impact
- **get_coredns_logs(tail_lines=200)** — look for UDP i/o timeout errors
- **describe_coredns()** — verify CoreDNS replicas are healthy and sufficient
- **Expected**: CoreDNS error logs with `read udp ... i/o timeout`

### Step 5: Root cause classification
Determine which of these applies:
| Cause | Indicator | Fix |
|-------|-----------|-----|
| Service mesh connection fan-out | envoy/istio-proxy with >5000 ESTAB connections | Reduce mesh connection pool size, increase `nf_conntrack_max` |
| DNS query amplification | CoreDNS logs showing very high QPS from misconfigured pods | Fix ndots, implement DNS caching |
| Default conntrack limit too low | `nf_conntrack_max=131072` but cluster has >100 pods | Increase `nf_conntrack_max` to 262144 or higher |
| Short-lived connection storm | Application creating/destroying connections rapidly (no keep-alive) | Enable HTTP keep-alive, connection pooling |
| Pod-to-pod mesh bypass needed | Conntrack tracking pod CIDR traffic unnecessarily | Add `NOTRACK` iptables rule for pod CIDR, or use eBPF-based CNI |

## Interpretation

| Metric | Normal | Warning | Critical |
|--------|--------|---------|----------|
| `conntrack count / max` | < 70% | 70-95% | > 95% (drops!) |
| `conntrack drops` (dmesg) | 0 | sporadic | `table full` |
| `insert_failed` (conntrack -S) | 0 | < 100/min | > 100/min |
| DNS latency | < 10ms | 50-500ms | timeouts |
| Node NetworkUnavailable | False | — | True |

## Fix Options (ordered by impact vs risk)

1. **Immediate mitigation**: Increase `nf_conntrack_max` via sysctl
   ```
   sysctl -w net.netfilter.nf_conntrack_max=262144
   ```
2. **Reduce tracking scope**: Add NOTRACK for pod-to-pod traffic (iptables/nftables)
3. **Reduce timeouts**: Lower `nf_conntrack_tcp_timeout_established` from 432000 (5 days) to 7200 (2 hours)
4. **Reduce connection fan-out**: Decrease service mesh connection pool size
5. **Long-term**: Switch to eBPF-based networking (Cilium) which doesn't use conntrack for pod-to-pod
