---
name: etcd-diagnosis
description: >
  Diagnoses etcd cluster failures — leader election timeouts, disk I/O
  latency impact on fsync, database size exceeding quota, member heartbeat
  failures, network partition between peers. Use when `kubectl` commands
  time out, `etcdctl endpoint health` reports unhealthy, or kube-apiserver
  logs show etcd errors.
---

# etcd Cluster Diagnosis

## Health Check

```bash
# All endpoints
etcdctl endpoint health --cluster -w table

# Detailed status per member
etcdctl endpoint status --cluster -w table
# Watch: DB SIZE, LEADER (same across all?), RAFT INDEX (lagging?)

# Disk performance (etcd is fsync-sensitive!)
fio --rw=write --ioengine=sync --fdatasync=1 --size=22m --bs=2300 --name=etcd-test
# etcd requires disk: p99 fsync < 10ms. Anything >25ms causes leader election.
```

## Common Failure Modes

| Symptom | Root Cause | Diagnostic |
|---------|-----------|------------|
| Leader changes every 30s | Disk fsync > election timeout | `etcd_disk_wal_fsync_duration_seconds` p99 > 25ms |
| `etcdserver: mvcc: database space exceeded` | DB size > `--quota-backend-bytes` (default 2GB) | `etcdctl endpoint status` DB SIZE, run `etcdctl compact` + `defrag` |
| `etcdserver: request timed out` | Slow disk or network | Check `etcd_network_peer_round_trip_time_seconds` |
| `etcdserver: no leader` | Leader election failing (all members) | Network partition between etcd peers, check latency |
| Member `started` but not `healthy` | Member can't catch up Raft log | `etcdctl endpoint status` → RAFT INDEX lagging |
| `failed to send out heartbeat on time` | Network latency between peers > 100ms | `ping` between etcd nodes, check cloud SDN latency |
| API server: `etcdserver: leader changed` | etcd leader change during write | Backend etcd instability, check disk IO |

## Impact on Kubernetes

| etcd Failure | K8s Symptom |
|-------------|------------|
| Leader election (3-10s) | Write requests queued, `kubectl apply` slow |
| Disk IO >50ms p99 | `kubectl get` slow (reads go to leader), watch API stalls |
| DB space exceeded | ALL write operations fail (`kubectl create/edit/apply` return errors) |
| Majority members down | Cluster read-only or unavailable, apiserver `/healthz/etcd` fails |
| Network partition | Split cluster, write conflicts possible on heal |

## Immediate Recovery Actions
1. **DB full**: `etcdctl compact <revision>` then `etcdctl defrag --cluster`
2. **Slow disk**: Migrate etcd data dir to SSD with high fsync IOPS
3. **Member down**: If still has quorum, `etcdctl member remove <id>` then re-add
4. **All members down**: Restore from snapshot `etcdctl snapshot restore`
