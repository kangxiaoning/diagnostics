## 补充思维模型

诊断流程由系统指令中的假设驱动循环控制。以下模型用于辅助故障识别和假设形成：

- **USE 方法**：针对每种资源按利用率→饱和度→错误顺序检查。
- **分层诊断**：OS层 → 内核层 → 运行时层 → 集群层 → 应用层，下层问题向上传导。

## Argus 监控协作模型

Coordinator **不持有** `query_argus_*` 工具——这些工具仅 Argus 专家 subagent 可用，
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
| load avg 高 + CPU util 低 + iowait 高 | 磁盘 IO 瓶颈 | check_cpu, check_disk | `disk-io-diagnosis` |
| 进程 RSS 持续增长 + swap 增加 | 内存泄漏 | check_memory | `memory-diagnosis` |
| 磁盘 util 99% + await 高 + 进程 D 状态 | 存储性能瓶颈 | check_disk | `disk-io-diagnosis` |
| GPU 显存耗尽 + CUDA OOM | 显存不足或泄漏 | check_gpu_memory, check_gpu_utilization | `gpu-diagnosis` |
| GPU 温度 >85°C + clock 降低 | 散热不足 | check_gpu_health | `gpu-diagnosis` |

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
