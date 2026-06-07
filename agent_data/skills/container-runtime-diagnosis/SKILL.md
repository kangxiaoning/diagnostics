---
name: container-runtime-diagnosis
description: >
  Diagnoses container runtime failures (containerd/docker) in Kubernetes
  — snapshot creation errors, image pull failures, CRI plugin disconnects,
  runtime OOM kills, task creation timeouts. Use when pods are stuck in
  `ContainerCreating` or kubelet logs show CRI errors.
---

# Container Runtime Diagnosis (containerd / docker)

## Runtime Reliability Check

```bash
# containerd
crictl info                          # runtime status
crictl pods                          # list all pods (CRI)
crictl ps -a                         # list all containers
ctr containers ls                    # containerd-native list
systemctl status containerd          # daemon health
journalctl -u containerd --since '30 min ago' | grep -iE 'error|warn|timeout|dead'

# docker (legacy)
docker info                          # daemon status
docker system df                     # disk usage
```

## Common Failure Modes

### containerd

| Symptom | Root Cause | Fix |
|---------|-----------|-----|
| Pod stuck `ContainerCreating` >5min | Image pull failure | Check image registry connectivity, auth, `crictl pull` |
| `failed to create shim task` | OCI runtime error (runc) | Check `/var/log/containerd/`, `runc list`, `runc delete` stale containers |
| `snapshot: snapshotter not ready` | overlay/aufs filesystem issue | `ctr snapshot ls`, check overlay module `lsmod | grep overlay` |
| `context deadline exceeded` (CRI) | containerd gRPC unresponsive | Containerd OOM or deadlocked, `systemctl restart containerd` |
| `no space left on device` | `/var/lib/containerd` full | `crictl rmi --prune`, clean old images/snapshots |
| `failed to reserve container name` | Stale container from crashed kubelet | `crictl rm <id>`, restart kubelet |
| `cgroups: cgroup mountpoint does not exist` | cgroup v1/v2 mismatch | Check `stat -fc %T /sys/fs/cgroup`, fix containerd cgroup driver config |

### containerd Resource Limits
```bash
# Check containerd OOM score (should be -999 to avoid OOM kill)
cat /proc/$(pidof containerd)/oom_score_adj

# Containerd memory usage
ps aux | grep containerd
# RSS: typically 500MB-2GB depending on pod count
```

## Image Pull Issues

```bash
# Manual pull to test registry
crictl pull registry.k8s.io/pause:3.9

# Check pull timeout (default: 2 min)
# In kubelet config: --image-pull-progress-deadline=2m0s

# Private registry auth
crictl pull --creds <user>:<pass> private.registry.io/image:tag
```

## Disk Space & Snapshots

```bash
# containerd storage
du -sh /var/lib/containerd/
# Snapshots leaking? → stuck container cleanup
ctr snapshot ls | wc -l

# Docker storage (legacy)
docker system prune -af  # remove ALL unused (careful!)
```

## Kubelet ↔ Runtime Connection

```bash
# Check CRI socket
ls -la /run/containerd/containerd.sock

# Kubelet connects via CRI to containerd
journalctl -u kubelet | grep -iE 'cri|cni|runtime'
# Common error: "failed to get sandbox status: ... connection refused"
```

## Recovery Priority
1. `crictl ps -a` → any containers in unknown/dying state?
2. `systemctl status containerd` → daemon health, restarts?
3. `df -h /var/lib/containerd` → out of space?
4. `crictl pull <pause-image>` → registry connectivity OK?
5. `journalctl -u kubelet --since '5 min ago' | grep -i error` → CRI errors?
