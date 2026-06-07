---
name: network-diagnosis
description: >
  Diagnose network congestion, packet loss, TCP retransmits, connection
  issues at OSI L3/L4 layers. Use when network latency is high or connections
  timeout. For deeper investigation into specific failure modes, cross-reference
  with sub-skills below.
---

# Network Layer Diagnosis (L3/L4)

## Primary Tool
`check_network()` — returns `ss` connections, interface errors/drops, TCP retransmits, dmesg.

## Quick Triage

| Signal | Meaning | Deeper Skill |
|--------|---------|-------------|
| `Send-Q > 0` | Receiver not consuming → app bottleneck | `kubernetes-diagnosis` |
| `TCP retransmit > 5%` | Network quality issue | This skill → classify below |
| `dropped > 0` (interface) | NIC ring buffer overflow | `softirq-starvation` |
| `CLOSE-WAIT` accumulating | Socket leak in application | `kubernetes-diagnosis` |
| `dmesg: arp_cache overflow` | ARP table full | `arp-cache-diagnosis` |
| `dmesg: nf_conntrack table full` | Conntrack saturation | `conntrack-diagnosis` |
| `ping large size fails` (small OK) | MTU mismatch | `mtu-misconfig-diagnosis` |
| High retransmit but no interface drops | Kernel parameter bottleneck | `kernel-parameter-drops` |
| `ss -lnt` Recv-Q = Send-Q | TCP accept queue overflow | `tcp-listen-overflow` |
| `ethtool -S rx_missed_errors > 0` | NIC ring buffer hardware drops | `softirq-starvation` |
| Intermittent connection refused/RST | TCP listen queue or kernel param | `tcp-listen-overflow` → `kernel-parameter-drops` |

## Diagnostic Tiers

### Tier 1: Interface Level
```
ip -s link → dropped / overrun → NIC ring buffer or driver
ethtool -S → rx_missed_errors → hardware-level drops (before kernel)
```

### Tier 2: Kernel Stack Level
```
/proc/net/softnet_stat col 3 → backlog drops → netdev_max_backlog too small
/proc/softirqs NET_RX → softirq imbalance → IRQ affinity + ring buffer
```

### Tier 3: TCP/Application Level
```
ss -ti → retrans, rcv_ssthresh, data_segs_out → per-connection health
nstat | grep -E 'Drop|Overflow|Retrans' → system-wide counters
ss -lnt → Recv-Q vs Send-Q → accept queue depth
```
