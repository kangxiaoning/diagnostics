---
name: tcp-listen-overflow
description: Diagnoses TCP accept/listen queue overflow — when `nstat` shows `ListenOverflows` or `TCPReqQFullDrop`, `ss -lnt` shows Recv-Q approaching SOMAXCONN, and clients experience intermittent connection timeouts or immediate RST after SYN-ACK. Use when applications report intermittent connection failures with no visible CPU/memory/disk pressure.
---

# TCP Listen & Accept Queue Overflow

## Background
Two queues control new TCP connections:
- **SYN queue** (incomplete): half-open connections (SYN sent, not yet ACKed). Controlled by `tcp_max_syn_backlog`.
- **Accept queue** (complete): fully established (3-way handshake done), waiting for `accept()`. Controlled by `backlog` (listen param) capped at `somaxconn`.

When either queue overflows, the kernel silently drops (or RSTs) new connections.

## Trigger
- Applications report `connection timeout` or `connection refused` intermittently
- No CPU/memory/disk pressure on the node
- `ss -lnt` shows `Recv-Q` close to `Send-Q` (accept queue filling up)
- `nstat -a | grep ListenOverflows` shows non-zero growing count

## Workflow

1. **ss -lnt** — check listening sockets: `Recv-Q` column is accept queue current depth, `Send-Q` column is configured `backlog` limit
2. **nstat** for overflow counters
3. **ss -ti** for TCP socket details on stuck connections
4. **sysctl** to check kernel limits

## Diagnostic Commands

```bash
# Listen queue status
ss -lnt | awk '{print $1,$2,$3}' | column -t
# If Recv-Q == Send-Q on any listener → accept queue full!

# Overflow counters
nstat -a | grep -E 'ListenOverflows|TCPReqQFullDrop|TCPSynRetrans'
#   ListenOverflows > 0: accept queue overflow, kernel dropped SYN
#   TCPReqQFullDrop > 0: request socket table overflow (with syncookies)

# TCP-level per-connection diagnostics
ss -ti state established | grep -E 'retrans|rcv_ssthresh|data_segs_out'

# Kernel sysctl limits
sysctl net.core.somaxconn              # Accept queue global cap
sysctl net.ipv4.tcp_max_syn_backlog     # SYN queue global cap
sysctl net.ipv4.tcp_abort_on_overflow   # 0=silent drop, 1=RST (dev only)
```

## Interpretation

| Condition | Meaning |
|-----------|---------|
| `Recv-Q == Send-Q`, `ss -lnt` | Accept queue full — app not calling `accept()` fast enough |
| `Recv-Q > 0` but `< Send-Q` | Healthier, but app may still lag behind burst |
| `ListenOverflows` growing | Kernel has been dropping new connections |
| `ss -ti` shows high `retrans:5+` | Client side TCP retransmits due to server drops |

## Root Causes
- **`net.core.somaxconn` too low** (default 128/256) for high-throughput services
- **Application `listen(fd, backlog)` too low** — capped by somaxconn
- **Application slow to `accept()`**: single-threaded event loop blocking on IO
- **Connection burst**: thundering herd or load balancer health checks saturating queue

## Fix Options
- `sysctl -w net.core.somaxconn=4096`
- Update application listen backlog: `nginx worker_connections` or `Gunicorn --backlog`
- Scale application horizontally or use multi-accept (`SO_REUSEPORT`)
- Monitor: `watch -n1 'ss -lnt | awk "NR>1 && \$2>0 {print}"'`
