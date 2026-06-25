## 补充思维模型

诊断流程由系统指令中的假设驱动循环控制。以下模型用于辅助故障识别和假设形成：

- **USE 方法**：针对每种资源按利用率→饱和度→错误顺序检查。
- **分层诊断**：OS层 → 内核层 → 运行时层 → 集群层 → 应用层，下层问题向上传导。

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

### 系统层
| 症状 | 可能根因 | 优先检查工具 | Skill |
|------|---------|------------|-------|
| load avg 高 + CPU util 低 + iowait 高 | 磁盘 IO 瓶颈 | check_cpu, check_disk, check_processes | `disk-io-diagnosis` |
| 进程 RSS 持续增长 + swap 增加 | 内存泄漏 | check_memory, check_processes | `memory-diagnosis` |
| 磁盘 util 99% + await 高 + 进程 D 状态 | 存储性能瓶颈 | check_disk, check_cpu | `disk-io-diagnosis` |
| GPU 显存耗尽 + CUDA OOM | 显存不足或泄漏 | check_gpu_memory, check_gpu_utilization | `gpu-diagnosis` |
| GPU 温度 >85°C + clock 降低 | 散热不足 | check_gpu_health | `gpu-diagnosis` |

### 网络层
| 症状 | 可能根因 | Skill |
|------|---------|-------|
| TCP 重传率高 + Send-Q 堆积 | 网络拥塞 | `network-diagnosis` |
| `ethtool -S` rx_missed > 0 + ksoftirqd CPU 高 | 软中断饥饿 / Ring Buffer 溢 | `softirq-starvation` |
| dmesg: `arp_cache overflow` | ARP 表满 (gc_thresh3 不足) | `arp-cache-diagnosis` |
| dmesg: `nf_conntrack table full` | 连接跟踪表溢出 | `conntrack-diagnosis` |
| ping 小包通、大包不通 | MTU 不匹配 (CNI overlay) | `mtu-misconfig-diagnosis` |
| `ss -lnt` Recv-Q = Send-Q，连接间歇超时 | TCP accept 队列溢出 | `tcp-listen-overflow` |
| TCP 重传高但接口无丢包 | 内核参数配置不当 | `kernel-parameter-drops` |

### 容器/K8s 跨层
| 症状 | 可能根因 | Skill |
|------|---------|-------|
| Node NotReady + 资源正常 | gRPC 死连接 (kubelet) | `grpc-connection-leak` |
| Node NotReady + 磁盘 IO 高 | kubelet lease 超时 | `cross-layer-diagnosis` |
| Node NotReady + kubelet 被 kill | 系统 OOM | `cross-layer-diagnosis` |
| Pod 健康但被重启 | CPU 节流→探针超时 | `cross-layer-diagnosis` |
| Pod 间跨节点通信失败 | ARP 表满 / Conntrack 满 | `arp-cache-diagnosis` / `conntrack-diagnosis` |

### K8s 控制面 & 组件
| 症状 | 可能根因 | Skill |
|------|---------|-------|
| `kubectl` 超时，`/healthz` 504 | API server OOM 或 CPU 不足 | `control-plane-diagnosis` |
| `kubectl` 超时，`/healthz/etcd` 失败 | etcd 不可达或慢 | `etcd-diagnosis` |
| TLS 证书过期 (`x509: expired`) | 证书到期 | `control-plane-diagnosis` |
| `etcdserver: mvcc: database space exceeded` | etcd DB > 2GB | `etcd-diagnosis` |
| Pod 卡在 ContainerCreating | 镜像拉取失败 \| containerd 异常 | `container-runtime-diagnosis` |
| DNS SERVFAIL 或 NXDOMAIN | CoreDNS RBAC \| Corefile 配置 | `coredns-diagnosis` |
| containerd `failed to create shim task` | runc/OCI 错误 | `container-runtime-diagnosis` |
