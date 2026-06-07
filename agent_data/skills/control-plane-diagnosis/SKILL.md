---
name: control-plane-diagnosis
description: >
  Diagnoses Kubernetes control plane component failures — kube-apiserver
  (etcd connection, TLS cert expiry, request throttling), kube-controller-manager
  (leader election, garbage collection), and kube-scheduler (pod scheduling
  failures, resource starvation). Use when `kubectl` returns timeouts,
  nodes disappear from cluster, or pods remain Pending despite available
  resources.
---

# Control Plane Component Diagnosis

## Component Map

```
                    ┌─────────────────┐
                    │  kube-apiserver  │ ← primary failure surface
                    └────────┬────────┘
                  ┌──────────┼──────────┐
          ┌───────┴──────┐ ┌─┴──────────┴─┐
          │ etcd cluster │ │   authz web-  │
          └──────────────┘ │   hooks, etc  │
                           └───────────────┘
           ┌─────────────────┐  ┌──────────────┐
           │controller-mgr   │  │  scheduler    │
           └─────────────────┘  └──────────────┘
```

## kube-apiserver

### Quick Health Check
```bash
kubectl get --raw /healthz           # basic health
kubectl get --raw /healthz/etcd      # etcd connectivity
kubectl get --raw /livez             # liveness (not restarting)
```

### Common Failure Modes

| Symptom | Root Cause | Fix |
|---------|-----------|-----|
| `kubectl` timeout + `/healthz` 504 | API server OOM or CPU throttled | Check node resources, increase limits |
| `/healthz` OK but `/healthz/etcd` fails | etcd unreachable or slow (>3s) | Check etcd disk IO, leader election (`etcdctl endpoint health`) |
| `x509: certificate has expired` | TLS cert expired (usually 1 year) | `kubeadm certs renew all` then restart |
| `etcdserver: mvcc: database space exceeded` | etcd DB > 2GB, compaction failed | `etcdctl compact`, `defrag`, increase quota |
| `caller: too many requests` | Request throttled | Check `--max-requests-inflight` / `--max-mutating-requests-inflight` |
| `watch chan error: etcdserver: no leader` | etcd leader election in progress | Restart etcd member, check network between etcd peers |
| API server restart loop | Misconfigured admission webhook | Check `kubectl get validatingwebhookconfigurations` |

### Diagnostic Commands
```bash
# API server audit log (expensive, enable temporarily)
grep 'responseStatus.code.........5[0-9][0-9]' /var/log/kube-apiserver/audit.log

# In-flight request count
curl -sk https://localhost:6443/metrics | grep apiserver_current_inflight_requests

# Etcd request latency
curl -sk https://localhost:6443/metrics | grep etcd_request_duration_seconds_bucket
```

## kube-controller-manager

### Key Checks
```bash
kubectl get pods -n kube-system -l component=kube-controller-manager
kubectl logs -n kube-system -l component=kube-controller-manager --tail=50
```

### Common Failure Modes
- **Leader election stuck**: `failed to renew lease kube-system/kube-controller-manager` → kube-controller-manager leader can't renew the lease, new leader not elected → all controllers offline
- **Node controller**: Node objects stuck in `Deleting` state → node controller not running GC
- **PodGC**: Orphaned completed pods accumulating → `PodGCController` misconfigured
- **ServiceAccount token not created**: `Tokencleaner` or `ServiceAccount` controller stalled

## kube-scheduler

### Key Checks
```bash
kubectl get pods -n kube-system -l component=kube-scheduler
kubectl logs -n kube-system -l component=kube-scheduler --tail=50
```

### Common Failure Modes
- **Pods Pending with no events**: scheduler pod is down or leader election stuck
- **Pods Pending + events: `0/nodes available`**: resource constraints, taints, or nodeSelector mismatch
- **Pods Pending + events: `pod affinity/anti-affinity`**: scheduling constraints unsatisfiable
- **Scheduler slow (>30s to schedule)**: profiling needed, check `scheduler_pending_pods` metrics

### K8s-Level Validation
```bash
kubectl get events --all-namespaces --field-selector type!=Normal | tail -20
kubectl get pods --all-namespaces --field-selector status.phase=Pending
kubectl describe pod <pending-pod> | grep -A5 Events
```

## Component Health Quick Reference

| Component | Pod Label | Log Command | Health Endpoint |
|-----------|----------|-------------|-----------------|
| kube-apiserver | `component=kube-apiserver` | `kubectl logs -n kube-system <pod>` | `:6443/healthz` |
| kube-controller-mgr | `component=kube-controller-manager` | same pattern | `:10257/healthz` |
| kube-scheduler | `component=kube-scheduler` | same pattern | `:10259/healthz` |
