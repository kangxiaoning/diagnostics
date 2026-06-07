---
name: softirq-starvation
description: Diagnoses network packet loss caused by softirq starvation — when `/proc/softirqs` NET_RX spikes, `ksoftirqd` consumes >50% CPU, `ethtool -S` shows `rx_missed_errors`, and network interface ring buffer overflows. Use when `ip -s link` shows packets dropped but there's no obvious application or TCP-level congestion.
---

# Softirq Starvation → Ring Buffer Overflow

## Trigger
- `ip -s link show dev eth0` shows `dropped` increasing
- `ethtool -S eth0` shows `rx_missed_errors` or `rx_fifo_errors`
- `top` shows `ksoftirqd/N` processes consuming >50% of a CPU core
- `/proc/softirqs` shows `NET_RX` count on some CPUs 10× higher than others
- dmesg shows `kernel: net_ratelimit: N callbacks suppressed`

## Workflow

1. **check_network()** — look for `rx_dropped` or `rx_missed_errors` in interface stats
2. **check_cpu()** — check if `sys%` is abnormally high (softirq shows as sys CPU)
3. **On affected node**, run the ring buffer / softirq diagnostic chain below

## Diagnostic Chain

### Layer 1: Confirm hardware drops at NIC level
```bash
ethtool -S eth0 | grep -E 'rx_missed|rx_fifo|rx_over_errors'
ip -s -s link show dev eth0 | grep -A5 'RX:'
```
If `rx_missed_errors` > 0 → NIC ring buffer full, packets dropped before kernel sees them.

### Layer 2: Check per-CPU softirq distribution
```bash
cat /proc/softirqs | grep NET_RX
# Compare per-CPU counts. If CPU0: 50M and CPU1: 200K → IRQ affinity imbalance.
cat /proc/net/softnet_stat
# Column 2 = processed, Column 3 = dropped (backlog full), Column 11 = times squeezed
```

### Layer 3: Check interrupt affinity
```bash
cat /proc/interrupts | grep eth0
# If all interrupts on single CPU → bottleneck
ethtool -g eth0  # Check current ring buffer size
ethtool -G eth0 rx 4096  # Increase ring buffer if below max
```

## Interpretation

| Metric | Normal | Warning | Critical |
|--------|--------|---------|----------|
| `ethtool -S rx_missed_errors` | 0 | >0 growing | >1000/sec |
| `/proc/softnet_stat` col 3 | 0 | occasional spike | continuously growing |
| `ksoftirqd` CPU% per core | <5% | 20-50% | >50% (saturated) |
| Ring buffer size vs max | max | 50-75% of max | <50% of max |

## Root Causes
- **IRQ affinity imbalance**: all NIC interrupts bound to CPU0 → single core bottleneck
- **Ring buffer too small**: default 256/512 entries not enough for 10G+ NICs
- **Interrupt coalescing too aggressive**: NIC batches too many packets before raising IRQ → ring buffer fills during batch period
- **NAPI budget exhausted**: `net.core.netdev_budget` (default 300) not enough for high-throughput
- **CPU frequency scaling**: governors like `powersave` slow down CPU → softirq processing delayed

## Fix Options
- **Increase ring buffer**: `ethtool -G eth0 rx 4096`
- **Spread IRQ affinity**: set `smp_affinity` or use `irqbalance`
- **Tune interrupt coalescing**: increase `rx-frames` / decrease `rx-usecs` via `ethtool -C`
- **Increase NAPI budget**: `sysctl -w net.core.netdev_budget=600`
- **Increase backlog**: `sysctl -w net.core.netdev_max_backlog=2000`
