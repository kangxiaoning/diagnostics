---
name: conntrack-diagnosis
description: "Diagnoses kernel connection tracking (conntrack) table saturation -- when `nf_conntrack: table full` causes pod-to-pod networking failures, DNS timeouts, and connection drops. Use when pod networking fails but nodes show Ready."
---

# Conntrack Table Saturation Diagnosis

## Trigger
- Pods fail DNS lookups (CoreDNS UDP)
- Pod-to-pod connections drop intermittently
- Events show `connection refused` or `timeout` on service calls
- Nodes remain Ready but `NetworkUnavailable=True`

## Workflow

1. **check_network()** — look for `nf_conntrack: table full, dropping packet` in dmesg
2. **check_kubernetes_nodes()** — verify `NetworkUnavailable=True` condition
3. **check_processes()** — check for high connection-count processes

## Interpretation

| Metric | Normal | Warning | Critical |
|--------|--------|---------|----------|
| `conntrack count / max` | < 70% | 70-95% | > 95% (drops!) |
| `conntrack drops` (dmesg) | 0 | sporadic | `table full` |
| DNS latency | < 10ms | 50-500ms | timeouts |

## Root Causes
- Too many short-lived connections from service mesh (Istio/Envoy)
- DNS query amplification from misconfigured CoreDNS
- Default `nf_conntrack_max` too small for dense pod deployments

## Fix Options
- Increase `nf_conntrack_max` via sysctl
- Reduce `nf_conntrack_tcp_timeout_established`
- Exclude pod CIDR from connection tracking (`NOTRACK` iptables rule)
- Reduce service mesh connection pool size
