---
name: kernel-parameter-drops
description: Diagnoses packet loss caused by misconfigured kernel parameters — when `net.core.rmem_max` or `tcp_rmem` is too small for high-BDP links, `netdev_max_backlog` is insufficient, or dangerous settings like `tcp_tw_recycle` cause connection resets. Use when TCP throughput is unexpectedly low or connections are dropped without infrastructure-level anomalies.
---

# Kernel Parameter Misconfiguration → Packet Loss

## Trigger
- TCP throughput on high-latency links far below expected bandwidth-delay product
- `nstat` counters show persistent drops in unexpected categories
- No network interface errors, no softirq starvation, no application backlog
- Recent kernel tuning or undocumented sysctl changes on the node

## Key Parameters to Audit

### Receive Buffer: `rmem_max` / `tcp_rmem`
```
net.core.rmem_max           # Per-socket receive buffer MAX (default ~212992)
net.ipv4.tcp_rmem           # [min, default, max] for TCP auto-tuning
```
**Failure**: `rmem_max` too small on a 100ms RTT × 10Gbps link → window capped at 2.6MB, throughput limited to ~210Mbps regardless of bandwidth.
**Diagnose**: `ss -ti` check `rwin`, `rcv_ssthresh`, `rcv_space` — if all at `rmem_max` cap → bottleneck.

### Backlog: `netdev_max_backlog`
```
net.core.netdev_max_backlog  # Packets queued per CPU when softirq can't keep up (default 1000)
```
**Failure**: `/proc/net/softnet_stat` column 3 (dropped) growing continuously.
**Diagnose**: `cat /proc/net/softnet_stat | awk '{print $3}'` — non-zero means backlog drops.

### SYN Flood Protection
```
net.ipv4.tcp_max_syn_backlog  # Half-open connection queue (default 128-256)
net.ipv4.tcp_syncookies       # Enable SYN cookies to survive SYN flood (1=enabled)
```
**Failure**: `TCPReqQFullDoCookies` counter growing, clients seeing high SYN-ACK response latency.
**Diagnose**: `nstat -a | grep TCPReqQFullDoCookies`

### Dangerous Parameters (never change without expert review)
```
net.ipv4.tcp_tw_recycle  → REMOVED since 4.12! Causes NAT connection breakage.
net.ipv4.tcp_tw_reuse    → Only safe if timestamps are strictly increasing (breaks behind NAT).
net.ipv4.tcp_abort_on_overflow → 1 causes RST instead of silent drop (use only for debugging).
```

## Workflow

1. **check_network()** — interface errors? softirq drops?
2. **check_processes()** — is the application setting socket options?
3. **Audit sysctl** — `sysctl -a | grep -E 'rmem|wmem|backlog|tcp_tw|somaxconn|netdev'`
4. **ss -ti** — per-connection TCP window and retransmit diagnostics
5. **nstat** — kernel-wide TCP counters for overflow/drop patterns

## Common Misconfigurations

| Symptom | Likely Parameter | Check |
|---------|-----------------|-------|
| Low throughput on fast link | `rmem_max` too small | `ss -ti` rwin capped at ~200KB |
| UDP packet loss in bursts | `rmem_max` + `netdev_max_backlog` | `softnet_stat` col 3 growing |
| Intermittent TCP RST on SYN | `tcp_abort_on_overflow=1` | Reset to 0 in production |
| NAT clients can't reconnect | `tcp_tw_recycle=1` | REMOVED in 4.12, upgrade kernel |
| High latency under load | `netdev_budget` too small | `softnet_stat` col 11 (squeezed) |

## Fixes (in order of safety)
1. `sysctl -w net.core.rmem_max=16777216`
2. `sysctl -w net.ipv4.tcp_rmem='4096 87380 16777216'`
3. `sysctl -w net.core.netdev_max_backlog=5000`
4. `sysctl -w net.ipv4.tcp_max_syn_backlog=8192`
5. Never enable `tcp_tw_recycle` (removed) or `tcp_abort_on_overflow` (unless debugging)
