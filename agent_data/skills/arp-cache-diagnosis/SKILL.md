---
name: arp-cache-diagnosis
description: "Diagnoses Linux ARP neighbor table overflow -- when `arp_cache: neighbor table overflow!` appears in dmesg and causes cross-node pod-to-pod communication failures in large Kubernetes clusters. Use when pods on different nodes cannot reach each other despite nodes being Ready and CNI appearing healthy."
---

# ARP Cache Overflow Diagnosis

## Trigger
- Pods on different nodes can't communicate (connection timeout)
- Cluster has grown past 500+ pods/nodes
- `ip neigh` shows 1024/1024 (or similar gc_thresh3 limit)
- dmesg contains `neighbour: arp_cache: neighbor table overflow!`

## Workflow

1. **check_kubernetes_nodes()** — any node with `NetworkUnavailable=True`?
2. **check_network()** — look for `arp_cache: neighbor table overflow!` in dmesg
3. **On affected node**: `ip neigh show nud stale | wc -l` — count stale entries
4. **Check GC thresholds**: `sysctl net.ipv4.neigh.default.gc_thresh1/2/3`

## Interpretation

| Metric | Normal | Warning | Critical |
|--------|--------|---------|----------|
| `ip neigh count` / `gc_thresh3` | < 70% | 70-90% | > 90% (drops!) |
| `nstat ArpFailed` | 0 | sporadic | increasing |
| `gc_thresh3` default | 1024 | — | too small for >500-node cluster |

## Root Causes
- Cluster scale exceeds default `gc_thresh3=1024` — each pod and node requires an ARP entry
- Too many stale ARP entries (GC interval too long)
- Kube-proxy in iptables mode creates many transient IPs

## Fix Options
- Increase `net.ipv4.neigh.default.gc_thresh1=1024`, `gc_thresh2=2048`, `gc_thresh3=4096`
- Decrease `gc_stale_time` from default 60s to 30s
- Switch kube-proxy to IPVS mode (fewer ARP entries)
- Consider using a CNI that avoids per-pod ARP (e.g., Calico with IPIP/BGP)
