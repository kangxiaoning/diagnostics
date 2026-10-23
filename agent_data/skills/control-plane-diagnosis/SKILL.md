---
name: control-plane-diagnosis
description: "Kubernetes 控制面故障（apiserver/controller-manager/scheduler）：kubectl 超时、节点从集群消失、Pod 长期 Pending"
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

## 过载级联（资源饱和 → etcd 选主 → apiserver OOM → list 风暴）

> 从 AGENTS.md 移入：单一故障类的详细复盘，按需阅读。

### 证据采集


| 观测面 | 采集方式（Coordinator 经 task 委派） | 关键信号 |
|--------|--------------------------------|---------|
| 控制面主机 | 集群 argus 专家（master 维度）+ 主机 argus 专家 | CPU util/sys/iowait、CFS throttled、MemAvailable、磁盘 util/await |
| apiserver | `get_k8s_system_pods`、`get_k8s_pod_restart_counts`、`get_k8s_pod_logs`、`get_k8s_resource_yaml`（启动参数） | 重启次数与 lastState、in-flight/APF/watch-cache 配置、实例间是否均衡 |
| etcd | `get_k8s_etcd_status`、`get_k8s_etcd_metrics`、`get_k8s_etcd_logs`、`get_k8s_etcd_check` | leader 变更次数、fsync/backend commit 延迟、slow apply、心跳失败、DB 大小与压缩 |
| 客户端 | `get_k8s_cluster_events`、审计日志（可采时） | 谁在 LIST：ServiceAccount、CNI agent、监控/Operator、批量 `kubectl -A` |


### 修复建议


#### 1. 应急止血（目标是恢复可响应，不是消除根因）

| 动作 | 具体做法 | 代价 / 注意 |
|------|---------|------------|
| 削并发上限 | 调低 `--max-requests-inflight` / `--max-mutating-requests-inflight`（默认 400/200；APF 开启时二者之和即全局并发上限） | 吞吐下降、429 与排队延迟上升；恢复后按观测逐级回调，长期停留低位会拖慢控制器调谐 |
| 隔离 list 大户 | APF 新建低 `nominalConcurrencyShares` 的 `PriorityLevelConfiguration` + 按 user/apiGroup/verb=list 匹配的 `FlowSchema`（`distinguisherMethod: ByUser`，`Queue` 或 `Reject`） | 编辑默认 FlowSchema/PL 前先设 `apf.kubernetes.io/autoupdate-spec: "false"`；份额此消彼长，需同时保 `system`/`leader-election` |
| 削减重负载流量 | 暂缓批量 `kubectl get -A`、巡检脚本、Operator 全量 list；LIST 加分页 `limit` 与字段/标签选择器 | 只削峰，不改变内存无上限的本质 |
| 保活而非群重启 | 保持至少一台 apiserver 可用，实例逐个重启（static pod 由 kubelet 拉起） | 全体同时重启 = 冷 watch cache + 全员 relist，风暴翻倍 |
| 临时扩容 | 提高 apiserver 内存 limit 或迁至空闲节点 | 只买时间；与 etcd 混部时扩容会挤压 etcd |

#### 2. 根因消除（控制面资源与 etcd 稳定性）

- 定位 CPU 饱和来源：apiserver 自身（list 序列化、审计/日志、admission webhook）、etcd（fsync、压缩）、他进程争抢、内存紧缩引发的换页读盘；容器化 apiserver/etcd 按 Guaranteed QoS 设置 `limits == requests`，避免 CFS 限流打掉心跳。
- etcd 数据目录用独立盘或独占节点（避免 burstable 云盘），`ionice -c2 -n0` 提升 etcd IO 优先级；fsync p99 持续 >1s 时优先解决 IO，而不是调大超时掩盖。
- etcd 时间参数（`--heartbeat-interval` 默认 100ms、`--election-timeout` 默认 1000ms）按成员实测 RTT 设定（heartbeat ≈ RTT、election ≥ 10×RTT），全成员取值一致；调大是延长故障检测时间的权衡。
- 确认 `--pre-vote=true`（3.5 起默认开启）并开启 `--check-quorum`，抑制落后或有 IO 抖动的成员单方面抬高 term 踢掉健康 leader。
- 控制面容量预留：按节点规模规划核数与内存（经验基线：100 节点 / 1400 Pod 约 2Gi）；CPU governor 设 `performance` 降低延迟抖动。

#### 3. 结构性加固（防止再次自持放大）

- list 开销治理：客户端分页 + 选择器；周期 list 改 watch；升级到支持 WatchList / 流式集合编码的版本（1.32 Beta，1.33 起 `StreamingCollectionEncodingTo*`），把 list 内存从整包组装降为常量级（官方实测 20GB → 2GB）。
- APF 纳入平台基线：为 CNI agent、监控采集、日志/Operator 等重客户端预置 FlowSchema，为系统关键优先级留足份额。
- 告警项：etcd leader 变更次数、WAL fsync p99、`apiserver_current_inflight_requests`、`apiserver_flowcontrol_rejected_requests_total`、apiserver RSS 斜率、按 user/resource 分组的 LIST QPS。
- 架构：etcd 与 apiserver 不混部；apiserver 前置 LB 保证实例均衡；客户端具备带 jitter 的指数退避。


### 修复验证


- apiserver RSS 不再单调上升，LIST 洪峰后回落；
- `etcd_server_leader_changes_seen_total` 数小时无增长，endpoint health 呈 1 主多从；
- `apiserver_current_inflight_requests` 稳定低于上限、flowcontrol 拒绝数回落、429 消失；
- Node 恢复 Ready、控制器 leader 稳定；逐级回调 in-flight 参数过程中上述指标不再劣化。

### 已知放大机制（真实复盘形态）


- **慢盘选主风暴**：etcd WAL fsync p99 远超 election timeout（默认 1000ms）→ 反复选主 → 每次选主冻结写入 → IO 积压 → 更多选主。
- **重客户端 LIST 打爆单实例**：CNI agent / 采集器对集群级资源全量 LIST，内存冲至数十 GB → OOM → 请求漂移到下一实例复现（多实例同时崩溃多为流量漂移，不是多个独立故障）。
- **实现缺陷叠加**：etcd 超时触发的请求失败路径若泄漏 goroutine/对象（如旧版 apiserver `DeleteCollection`），内存只增不减，最终 OOMKilled，并被控制器重启后的 ListAndWatch 二次放大。
