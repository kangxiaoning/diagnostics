---
name: grpc-connection-leak
description: "Diagnoses gRPC dead connection / connection leak between kubelet and API server -- when kubelet stops reporting node status and logs show `transport: http2Client.notifyError` or `rpc error: code = Unavailable`. Use when a node suddenly becomes NotReady without clear resource pressure (CPU/memory/disk all normal) and kubelet logs contain gRPC transport errors."
---

# gRPC Dead Connection (Kubelet↔API Server) Diagnosis

## Background
Kubelet communicates with the API server over HTTP/2 (gRPC). A long-lived TCP connection can become half-open (e.g., due to a network device timing out the NAT entry) without either side detecting it. gRPC's default keepalive (infinite timeout) does not detect these stale connections, causing:
- Kubelet stops receiving watch events from API server
- Kubelet fails to update node lease / status
- Node becomes NotReady with `NodeStatusUnknown`
- Kubelet logs show transport errors but no clear resource exhaustion

This is a **class of bugs**, not a single issue. Fixed in kubelet v1.20+ with HTTP/2 PING health checks.

## Trigger
- Node flips to NotReady with all conditions Unknown
- Node CPU, memory, disk are within normal ranges — no resource pressure
- Kubelet process is running (not OOM killed)
- Kubelet logs contain gRPC transport errors
- Network between node and API server appears stable (ping, SSH work)

## Workflow

1. **check_kubernetes_nodes()** — node Ready=Unknown? conditions all Unknown?
2. **check_cpu()** — CPU/load normal? (if normal, not a CPU starvation issue)
3. **check_memory()** — memory normal? (if normal, not OOM)
4. **check_disk()** — disk util normal? (if normal, not disk IO starvation)
5. **Check kubelet logs** (`journalctl -u kubelet --since '30 min ago'`):
   - `transport: http2Client.notifyError got notified that the client transport was broken`
   - `rpc error: code = Unavailable desc = transport is closing`
   - `grpc: addrConn.createTransport failed to connect`
   - `connection closing` / `error reading server preface: EOF`
6. **Check TCP state**: `ss -tnp | grep kubelet` — look for CLOSE_WAIT or ESTABLISHED with zero Recv-Q

## Root Cause Discrimination

| Condition | Likely Cause |
|-----------|-------------|
| gRPC errors + kubelet running + all resources normal | **gRPC dead connection** (HTTP/2 keepalive not detecting half-open TCP) |
| gRPC errors + CPU/memory/disk pressure | Check corresponding host resource skill first |
| gRPC errors + API server overloaded | Check API server metrics, not a kubelet issue |

## Fix Options
- **Upgrade kubelet to v1.20+** — HTTP/2 PING health check is built-in (PR #95981)
- **For older versions (< v1.20)**: restart kubelet as temporary workaround, or:
  - Set gRPC keepalive via kubelet config: `apiServer.clientConnection.keepAlive: 30s`
  - Reduce `node-status-update-frequency` to 20s
- **Verify API server reachability**: `curl -k https://<api-server>:6443/healthz` from node
- **Check NAT/load balancer timeouts**: cloud LB idle timeout < TCP keepalive interval → stale connections
