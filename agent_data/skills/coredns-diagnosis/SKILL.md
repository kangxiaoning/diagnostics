---
name: coredns-diagnosis
description: >
  Diagnoses CoreDNS failures in Kubernetes — DNS resolution timeouts,
  NXDOMAIN/SERVFAIL errors, pod crashes, CoreDNS configuration issues,
  RBAC permission gaps, and performance degradation under high query load.
  Use when `nslookup` from pods fails, services are unreachable by name,
  or CoreDNS pods show CrashLoopBackOff.
---

# CoreDNS Diagnosis

## Quick Triage

```bash
# 1. Is CoreDNS running?
kubectl get pods -n kube-system -l k8s-app=kube-dns

# 2. Check logs for errors
kubectl logs -n kube-system -l k8s-app=kube-dns --tail=50

# 3. Test resolution from a pod
kubectl run -it dnsutils --image=busybox:1.28 --restart=Never -- nslookup kubernetes.default

# 4. Check CoreDNS service and endpoints
kubectl get svc -n kube-system kube-dns
kubectl get endpoints -n kube-system kube-dns
```

## Common Failure Modes

| Symptom | Root Cause | Fix |
|---------|-----------|-----|
| `SERVFAIL` for cluster.local | RBAC: `system:coredns` lacks `endpointslices` list/watch | `kubectl edit clusterrole system:coredns` add endpointslices permission |
| `nslookup: can't resolve` | CoreDNS pods not running | Check pod status, node resources, taints |
| Timeout on external resolution | Upstream DNS (8.8.8.8) unreachable | Check egress firewall, proxy settings in Corefile |
| `NXDOMAIN` for existing service | Wrong namespace or Corefile misconfig | Use FQDN `<svc>.<ns>.svc.cluster.local` |
| CoreDNS OOMKilled | Too many records, cache too large | Increase memory limit, reduce `cache` TTL, limit `max_concurrent` |
| High query latency (>100ms) | CoreDNS pod overloaded or single replica | Scale: `kubectl scale deployment coredns -n kube-system --replicas=3` |
| Intermittent DNS failures | kube-proxy not updating iptables, stale endpoints | Check kube-proxy logs, restart kube-proxy |
| `connection refused` to DNS port | CoreDNS not listening on port 53 | Check Corefile, container port mapping |
| `no route to host` for DNS IP | CNI network issue | Check pod network connectivity, firewall rules |

## Corefile Configuration Check

```bash
kubectl get configmap coredns -n kube-system -o yaml
```

Look for:
- `forward . /etc/resolv.conf` → may cause loop on systemd-resolved nodes
- Missing `log` plugin → no query logs for debugging
- `cache 30` → cache TTL (too high = stale, too low = etcd pressure)
- `kubernetes` plugin `pods insecure` → enables pod DNS records

## RBAC Validation

```bash
kubectl describe clusterrole system:coredns
# Must have: list/watch on endpoints, endpointslices, services, pods, namespaces
```

## Metrics (CoreDNS default port 9153)
```
coredns_dns_requests_total{rcode="SERVFAIL"}  # failing queries
coredns_dns_request_duration_seconds           # latency percentiles
coredns_panic_count_total                      # CoreDNS panics
```

## Performance Tuning
- `cache` TTL: default 30s, increase to 60s for stable records
- `max_concurrent`: default 10000, lower for memory-constrained environments
- Scale horizontally: 2+ replicas with `podAntiAffinity`
- Use `nodeSelector` to avoid noisy neighbor effects
