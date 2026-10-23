## 补充思维模型

诊断流程由系统指令中的假设驱动循环控制。以下模型用于辅助故障识别和假设形成：

- **USE 方法**：针对每种资源按利用率→饱和度→错误顺序检查。
- **分层诊断**：OS层 → 内核层 → 运行时层 → 集群层 → 应用层，下层问题向上传导。

## Argus 监控协作模型

Coordinator **不持有** `get_argus_*` 工具——这些工具仅 Argus 专家 subagent 可用，
Coordinator 直接调用会被系统拦截。监控时序一律通过 `task()` 委派获取：

- 主机级指标（CPU/内存/磁盘/网络）→ 委派当前场景的主机 argus 专家
- 集群级指标（节点/Pod/API/etcd/DNS）→ 委派当前场景的集群 argus 专家
- 确切的专家名称随诊断场景变化（主机/容器/Serverless），以系统指令列出的当前场景可用专家为准。

专家返回的时序摘要中，各类指标的异常信号参考：

| 指标域 | 异常信号 |
|--------|---------|
| CPU/Load/iowait | 1min 内 CPU 突变 >50% 或 iowait 突变 >30% |
| Mem/Swap/OOM | Swap 持续增长或 OOM 事件 |
| 磁盘 Util/await/IOPS | util >90% 或 await >50ms |
| 网络丢包/重传 | 丢包和重传在同一分钟暴增 → 独立事件 |
| NotReady/Pod重启/驱逐/Pending | 节点失联时间 + Pod重启/驱逐/调度阻塞 |
| API延迟/etcd/DNS | API延迟+etcd leader切换+DNS超时 |

**关键判断**：两指标在同一分钟同时突变 → 可能**独立根因**（非因果），应形成 2 个假设。

## 委派决策速查

（详细委派规则与必含参数以系统指令 UNDERSTAND/VERIFY 阶段说明为准）

| 症状域 | 委派专家 | 典型指令 |
|--------|---------|---------|
| CPU/内存/磁盘/网络异常 | `host-expert` | "验证假设H1，使用xx技能检查xx" |
| Pod/Node/etcd/DNS 异常 | `k8s-expert` | "验证假设H2，检查xx集群状态" |
| GPU 异常 | `gpu-expert` | "验证假设H3，检查GPU健康/显存" |

## K8s 组件故障速查

遇到以下 `kubectl` 症状时，优先排查对应组件：

| 现象 | 可能涉及的组件 | Skill |
|------|-------------|-------|
| `kubectl get pods` 超时 | kube-apiserver \| etcd | `control-plane-diagnosis` \| `etcd-diagnosis` |
| Pod 长期 Pending | kube-scheduler \| 节点资源 | `control-plane-diagnosis` |
| Pod OOMKilled 重启 | 内存 limit \| 进程泄漏 \| 节点 OOM | `kubernetes-diagnosis` \| `cross-layer-diagnosis` |
| Pod 卡在 ContainerCreating | containerd \| CNI \| 镜像仓库 | `container-runtime-diagnosis` |
| Pod 内 DNS 解析失败 | CoreDNS \| kube-proxy \| 网络 | `coredns-diagnosis` |
| Node NotReady | kubelet \| containerd \| 节点资源 | `grpc-connection-leak` \| `cross-layer-diagnosis` |
| `kubectl logs` 超时 | kube-apiserver \| etcd | `control-plane-diagnosis` \| `etcd-diagnosis` |
| `kubectl` 全量超时 + apiserver 反复 OOMKilled 重启 | 控制面 CPU/内存饱和 + list 风暴 | `control-plane-diagnosis` \| `memory-diagnosis` |
| etcd 频繁 `leader changed` + API 5xx | etcd 心跳超时（CPU 饱和 \| fsync 抖动） | `etcd-diagnosis` \| `disk-io-diagnosis` |

## 网络丢包分层诊断

| 层级 | 命令/工具 | 丢包特征 | 对应 Skill |
|------|----------|---------|-----------|
| **NIC 硬件** | `ethtool -S` rx_missed_errors | 网卡 ring buffer 溢出 | `softirq-starvation` |
| **软中断** | `/proc/softirqs` NET_RX 高 | ksoftirqd CPU 高，单核瓶颈 | `softirq-starvation` |
| **内核 backlog** | `/proc/net/softnet_stat` col3 | netdev_max_backlog 不足 | `kernel-parameter-drops` |
| **ARP/Conntrack** | dmesg 关键字 | 邻居表满 / 连接跟踪表满 | `arp-cache-diagnosis` / `conntrack-diagnosis` |
| **MTU** | ping 大包失败 | 分片失败、ICMP 被拦截 | `mtu-misconfig-diagnosis` |
| **TCP 监听队列** | `ss -lnt` Recv-Q ≥ Send-Q | somaxconn 或应用 accept 太慢 | `tcp-listen-overflow` |
| **TCP 缓冲区** | `ss -ti` rwin 很小 | rmem_max 或 tcp_rmem 不足 | `kernel-parameter-drops` |
| **应用层** | `ss -ti` retrans 高 | 代码 bug、连接泄漏 | `kubernetes-diagnosis` |

## 常见故障模式

> **注意**：Coordinator 通过 Argus 发现异常后**委派专家**使用以下工具和技能。Coordinator 自身不持有 `check_*` 工具。

### 系统层（委派 host-expert）
| 症状 | 可能根因 | 专家工具 | Skill |
|------|---------|---------|-------|
| load avg 高 + CPU util 低 + iowait 高 | 磁盘 IO 瓶颈 | get_os_cpu_info, get_os_block_5s | `disk-io-diagnosis` |
| 进程 RSS 持续增长 + swap 增加 | 内存泄漏 | get_os_mem_5s | `memory-diagnosis` |
| 磁盘 util 99% + await 高 + 进程 D 状态 | 存储性能瓶颈 | get_os_block_5s | `disk-io-diagnosis` |
| GPU 显存耗尽 + CUDA OOM | 显存不足或泄漏 | get_gpu_status_info, get_gpu_status_info | `gpu-diagnosis` |
| GPU 温度 >85°C + clock 降低 | 散热不足 | get_gpu_status_info | `gpu-diagnosis` |

### 网络层（委派 host-expert）
| 症状 | 可能根因 | Skill |
|------|---------|-------|
| TCP 重传率高 + Send-Q 堆积 | 网络拥塞 | `network-diagnosis` |
| `ethtool -S` rx_missed > 0 + ksoftirqd CPU 高 | 软中断饥饿 / Ring Buffer 溢 | `softirq-starvation` |
| dmesg: `arp_cache overflow` | ARP 表满 (gc_thresh3 不足) | `arp-cache-diagnosis` |
| dmesg: `nf_conntrack table full` | 连接跟踪表溢出 | `conntrack-diagnosis` |
| ping 小包通、大包不通 | MTU 不匹配 (CNI overlay) | `mtu-misconfig-diagnosis` |
| `ss -lnt` Recv-Q = Send-Q，连接间歇超时 | TCP accept 队列溢出 | `tcp-listen-overflow` |
| TCP 重传高但接口无丢包 | 内核参数配置不当 | `kernel-parameter-drops` |

### 容器/K8s 跨层（委派 host-expert 或 k8s-expert）
| 症状 | 可能根因 | Skill | 专家 |
|------|---------|-------|------|
| Node NotReady + 资源正常 | gRPC 死连接 (kubelet) | `grpc-connection-leak` | host-expert |
| Node NotReady + 磁盘 IO 高 | kubelet lease 超时 | `cross-layer-diagnosis` | host-expert |
| Node NotReady + kubelet 被 kill | 系统 OOM | `cross-layer-diagnosis` | host-expert |
| Pod 健康但被重启 | CPU 节流→探针超时 | `cross-layer-diagnosis` | host-expert |
| Pod 间跨节点通信失败 | ARP 表满 / Conntrack 满 | `arp-cache-diagnosis` / `conntrack-diagnosis` | host-expert |

### K8s 控制面 & 组件（委派 k8s-expert）
| 症状 | 可能根因 | Skill |
|------|---------|-------|
| `kubectl` 超时，`/healthz` 504 | API server OOM 或 CPU 不足 | `control-plane-diagnosis` |
| `kubectl` 超时，`/healthz/etcd` 失败 | etcd 不可达或慢 | `etcd-diagnosis` |
| TLS 证书过期 (`x509: expired`) | 证书到期 | `control-plane-diagnosis` |
| `etcdserver: mvcc: database space exceeded` | etcd DB > 2GB | `etcd-diagnosis` |
| Pod 卡在 ContainerCreating | 镜像拉取失败 \| containerd 异常 | `container-runtime-diagnosis` |
| DNS SERVFAIL 或 NXDOMAIN | CoreDNS RBAC \| Corefile 配置 | `coredns-diagnosis` |
| containerd `failed to create shim task` | runc/OCI 错误 | `container-runtime-diagnosis` |
| 控制面 CPU/内存打满 → etcd 选主 → apiserver OOM → 反复重启 | 资源饱和沿依赖链自持放大 | `control-plane-diagnosis` \| `etcd-diagnosis` |

## 控制面过载级联（资源饱和 → etcd 选主 → apiserver OOM → list 风暴）

控制面资源饱和不会线性劣化，而是沿依赖链形成**自持反馈环**：

```
控制面 CPU/内存饱和（apiserver 与 etcd 在同机争抢）
  → etcd follower 心跳/fsync 超时 → leader election（写入冻结）
  → apiserver 请求堆积、内存攀高 → OOMKilled（exitCode 137）
  → 客户端 watch 全断 → informer 冷启动全量 LIST（relist 风暴）
  → list 响应须在内存整包组装 + watch cache 重建 → CPU/内存再次冲高
  → apiserver 反复重启，控制面失去自愈能力
```

### 阶段识别

| 阶段 | 典型现象 | 关键证据 | 判据 / 易误判点 |
|------|---------|---------|----------------|
| ① 资源饱和 | `kubectl` 变慢、master 上 shell 卡顿 | CPU sys%/iowait 高、`container_cpu_cfs_throttled_seconds_total` 持续增长、MemAvailable 走低 | CPU **均值**不高但 throttled 增长 → 仍属 CPU 不足（limit 过低或 limits>requests 被限流） |
| ② etcd 选主 | `etcdserver: leader changed` / `no leader`、写请求 5xx、KCM `leaderelection lost` | `etcd_server_leader_changes_seen_total` 5min >3、WAL fsync p99 >1s、`etcd_server_heartbeat_send_failures_total` >0 | 选主是**结果**：先查 CPU 饱和/磁盘 fsync/同机 IO 争抢，再谈网络 |
| ③ apiserver OOM | 容器 `lastState.reason=OOMKilled`、exitCode 137、`/healthz` 504 | RSS 单调上升、`go_goroutines` 异常增长、`workqueue_adds_total` 陡增 | 扩内存只延后崩溃；须区分 list 峰值、goroutine 泄漏与正常缓存增长 |
| ④ list 风暴 | 每次重启后 LIST QPS 陡增、etcd 读延迟飙升、再被打爆 | `apiserver_request_total{verb="LIST"}` 按 user/resource 突增、`apiserver_current_inflight_requests` 触顶、watch cache 重建 | 重启本身即触发全量 relist，是结构性反馈；多实例同时重启成倍放大 |
| ⑤ 失去自愈 | apiserver 重启循环、Node 转 NotReady、控制器反复选主 | 两次重启间隔内 LIST 洪峰再次打满、`apiserver_flowcontrol_rejected_requests_total` 上升 | 在洪峰未削平前反复重启会加剧风暴（冷缓存 → 更多 etcd range scan） |

### 证据采集

| 观测面 | 采集方式（Coordinator 经 task 委派） | 关键信号 |
|--------|--------------------------------|---------|
| 控制面主机 | 集群 argus 专家（master 维度）+ 主机 argus 专家 | CPU util/sys/iowait、CFS throttled、MemAvailable、磁盘 util/await |
| apiserver | `get_k8s_system_pods`、`get_k8s_pod_restart_counts`、`get_k8s_pod_logs`、`get_k8s_resource_yaml`（启动参数） | 重启次数与 lastState、in-flight/APF/watch-cache 配置、实例间是否均衡 |
| etcd | `get_k8s_etcd_status`、`get_k8s_etcd_metrics`、`get_k8s_etcd_logs`、`get_k8s_etcd_check` | leader 变更次数、fsync/backend commit 延迟、slow apply、心跳失败、DB 大小与压缩 |
| 客户端 | `get_k8s_cluster_events`、审计日志（可采时） | 谁在 LIST：ServiceAccount、CNI agent、监控/Operator、批量 `kubectl -A` |

### 已知放大机制（真实复盘形态）

- **慢盘选主风暴**：etcd WAL fsync p99 远超 election timeout（默认 1000ms）→ 反复选主 → 每次选主冻结写入 → IO 积压 → 更多选主。
- **重客户端 LIST 打爆单实例**：CNI agent / 采集器对集群级资源全量 LIST，内存冲至数十 GB → OOM → 请求漂移到下一实例复现（多实例同时崩溃多为流量漂移，不是多个独立故障）。
- **实现缺陷叠加**：etcd 超时触发的请求失败路径若泄漏 goroutine/对象（如旧版 apiserver `DeleteCollection`），内存只增不减，最终 OOMKilled，并被控制器重启后的 ListAndWatch 二次放大。

### 分诊要点

- 先定时序再定因果：CPU 饱和 → etcd 选主 → apiserver OOM → LIST 风暴；同一分钟突变的两个指标可能是**独立根因**，应并行假设。
- 调低并发后集群恢复 → 瓶颈是「并发数 × 单请求资源消耗」；单请求变慢（etcd 慢使席位占用时间变长）同样推高队列与内存。
- 关键流量饥饿会反噬：`leader-election` / `node-high` / `system` 优先级饿死 → 控制器重启 → 全量 ListAndWatch → 过载加重。
- APF 对 CPU 保护有效、对内存保护弱：list 内存随对象数无上限增长，必须靠分页/流式/降低并发共同约束。

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
