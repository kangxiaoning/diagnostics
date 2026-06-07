# 验证案例：生产集群多层级联故障

## 概述

这是一个**多层叠加故障**案例，模拟真实生产中多个组件同时或连锁出现问题，用于全面验证项目的 skills、tools、expert agents 和 prompt 的诊断能力。该案例要求诊断系统能够：
1. 在多个信号混杂时正确分层定位
2. 按提示要求先规划再按步骤采集证据
3. 交叉验证多个工具的结果
4. 合理委派 expert 子代理做深度分析
5. 收束后生成结构化报告并完成自我进化

---

## 用户输入（模拟运维工程师的初始描述）

> 我们的生产 Kubernetes 集群（3 master + 5 worker）出了严重问题，从凌晨2点开始：
>
> 1. `kubectl get pods` 经常超时，要重试好几次才能成功
> 2. 核心业务 `api-gateway` deployment 的 Pod 在 CrashLoopBackOff，日志显示 OOMKilled Exit:137
> 3. `user-service` 偶尔出现 "connection timeout" 调用 `order-service`
> 4. 有好几个 Pod 报 DNS 解析失败：`nslookup: can't resolve 'order-service.default.svc.cluster.local'`
> 5. 其中一台 worker 节点 `worker-3` 显示 NotReady，上面的 Pod 全变成 Unknown 了
> 6. `api-gateway` 的内存 limit 设置的是 512Mi，但不知道实际需要用多少
>
> 集群版本 v1.28.5，使用 Calico CNI，containerd 运行时。以前没遇到过这种组合故障。

---

## 真实根因（验证者已知，用于判断诊断结果正确性）

这是一个**三因叠加**的多层故障：

| 层级 | 组件 | 故障 | 根因 |
|------|------|------|------|
| **控制面** | etcd | API Server 请求超时 | master-1 上 etcd 数据目录所在磁盘 `/var/lib/etcd` 被日志轮转占满 IO，fsync p99 延迟 > 200ms，etcd 响应缓慢导致 API Server `/healthz/etcd` 超时 |
| **节点层** | worker-3 kubelet | Node NotReady | worker-3 上运行了一个批处理 Java 进程（非 Pod，直接在主机上运行），RSS 达到 6.2GB，触发系统 OOM Killer 误杀了 kubelet |
| **应用层** | api-gateway + CoreDNS | Pod OOMKilled + DNS 间歇失败 | api-gateway Java 进程稳态 RSS ~650MiB 但 limit=512MiB，持续被 cgroup OOM killed；同时 worker-3 上的 CoreDNS Pod 因节点 NotReady 后只剩1个副本，DNS 查询负载集中后出现间歇超时 |

### 因果链

```
etcd 磁盘 IO 饱和
  └─→ API Server 响应变慢
        └─→ kubectl 超时、调度延迟

worker-3 主机 Java 占满内存
  └─→ 系统 OOM Killer 终止 kubelet
        └─→ worker-3 NotReady
              └─→ Pod 被驱逐/Unknown
              └─→ CoreDNS 副本数从 3 → 1（其中一个在 worker-3 上）
                    └─→ DNS 查询负载集中 → 间歇超时
                    └─→ 服务间 nslookup 失败

api-gateway memory limit=512MiB < 实际 RSS ~650MiB
  └─→ cgroup OOM → Exit:137 → CrashLoopBackOff
```

---

## 预期诊断路径（验证 prompt 是否被正确执行）

### Phase 1：规划（验证 write_todos + `<step>` 标签）

Agent 应该首先调用 `write_todos`，创建如下的任务清单：
```
1. 获取集群整体系统信息
2. 检查 Kubernetes 集群节点状态
3. 检查 api-gateway Pod 详情（OOMKilled 根因）
4. 检查 worker-3 NotReady 原因（主机层检查）
5. 检查 DNS 解析失败原因（CoreDNS 状态）
6. 委派专家深度分析各层问题
7. 生成诊断报告
```

### Phase 2：并行证据采集（验证并行调用规范）

Agent 应并行发起：
- `get_system_overview()`
- `check_kubernetes_nodes()`
- `check_kubernetes_pods("default")`

之后根据初步发现，再次并行发起深入检查：
- `check_memory()` — 检查 worker-3 的内存压力
- `check_disk()` — 检查 etcd 节点磁盘 IO
- `check_network()` — 检查 DNS 相关网络问题
- `check_processes()` — 检查 D 状态进程（etcd IO 阻塞）和 OOM 相关进程
- `check_cpu()` — 辅助确认 IO wait

### Phase 3：交叉验证

每个结论应有至少两个工具的证据支撑：

| 结论 | 工具1 证据 | 工具2 证据 |
|------|-----------|-----------|
| etcd 磁盘 IO 饱和 | `check_disk()` → util 99%+ await 高 | `check_processes()` → etcd 进程 D 状态 |
| worker-3 kubelet 被 OOM kill | `check_kubernetes_nodes()` → NotReady | `check_memory()` → dmesg OOM killer |
| api-gateway limit 不足 | `check_kubernetes_pods()` → Exit:137 | `check_memory()` → 节点内存正常（排除节点级） |
| CoreDNS 副本不足 | `check_kubernetes_pods()` → kube-system 副本数 | `check_network()` → DNS timeout 从 pod 内测试 |

### Phase 4：委派 Expert 子代理（验证 task 工具调用）

应委派以下 expert：
- `disk-io-expert` — 深入分析 etcd 磁盘 IO 对控制面的影响
- `memory-expert` — 分析 worker-3 OOM 和 api-gateway cgroup OOM 的区别
- `kubernetes-expert` — 分析 Pod 状态和节点失联的关联
- `network-expert` — 分析 DNS 失败是否来自 CoreDNS 负载还是 conntrack

### Phase 5：收束与报告（验证 write_file /diagnosis_report.md）

满足以下条件后收束：
- 三个根因都已确认并有交叉验证
- 不再继续采集无关数据
- 调用 `write_file` 写入报告
- 委派 `report-writer` 生成最终报告

### Phase 6：自我进化（验证 LEARNINGS.md 更新）

报告写完后应执行：
1. `read_file /agent_data/LEARNINGS.md`
2. 判断是否有重复条目
3. 追加或更新经验条目（这是一个复合场景，应为多层故障新增模式）

---

## 验证检查清单

### Skills 覆盖验证

| Skill | 是否触发 | 触发条件 |
|-------|----------|----------|
| `kubernetes-diagnosis` | ✓ | Pod OOMKilled, CrashLoopBackOff |
| `control-plane-diagnosis` | ✓ | kubectl 超时 → etcd/API server |
| `etcd-diagnosis` | ✓ | API server 慢 → fsync 延迟 |
| `coredns-diagnosis` | ✓ | DNS 解析失败 |
| `memory-diagnosis` | ✓ | OOM Killer + cgroup OOM |
| `disk-io-diagnosis` | ✓ | etcd 磁盘 IO 饱和 |
| `cross-layer-diagnosis` | ✓ | 主机 OOM → kubelet kill → Node NotReady |
| `network-diagnosis` | ✓ | DNS 超时 + 服务间连接失败 |
| `container-runtime-diagnosis` | ✓ | `check_processes()` containerd D 状态 |

### Tools 覆盖验证

| Tool | 调用预期 |
|------|----------|
| `get_system_overview()` | **必须首调** |
| `check_kubernetes_nodes()` | 检测 NotReady + 条件 |
| `check_kubernetes_pods()` | 检测 OOMKilled + CrashLoopBackOff + CoreDNS 副本 |
| `check_memory()` | OOM dmesg + 节点内存压力 |
| `check_disk()` | etcd 磁盘 IO 饱和 |
| `check_network()` | DNS 解析 / 连接问题 |
| `check_processes()` | D 状态进程 / OOM 进程 |
| `check_cpu()` | IO wait 确认 |

### Prompt 规范验证

| 规范 | 验证点 |
|------|--------|
| `<step>` 标签 | 每次工具调用前是否输出 |
| `write_todos` | 开始时是否规划完整步骤 |
| 并行调用 | 无依赖的工具是否并行（如 check_memory + check_disk + check_network）|
| 交叉验证 | 每个结论是否由 ≥2 个工具佐证 |
| 中期反思 | 收集到关键证据后是否评估假设 |
| 委派 expert | 是否使用 task 工具委派相应领域专家 |
| 报告收束 | 是否写入 /diagnosis_report.md 后停止 |
| 自我进化 | 是否更新 LEARNINGS.md |

---

## 预期诊断结论（参考答案）

```
根因：
1. 【控制面】master-1 etcd 磁盘 IO 饱和（%util > 99%，fsync > 200ms）
   → API Server /healthz/etcd 超时 → kubectl 操作缓慢
   建议：将 etcd 数据目录迁移到独立 SSD；限制日志轮转 IO

2. 【节点】worker-3 主机 Java 进程占用内存 6.2GB RSS
   → 系统 OOM Killer 终止 kubelet → 节点 NotReady
   建议：将主机进程迁移到 Pod 并设置 memory limit；设置 kubelet OOM score adj=-999

3. 【应用】api-gateway memory limit 512Mi < 实际 RSS 650MiB
   → cgroup OOMKilled → CrashLoopBackOff
   建议：将 limits.memory 提高到 1Gi

连锁影响：
- worker-3 NotReady → CoreDNS 副本从 3 降至 1
  → DNS 负载集中 → 间歇超时
  → 服务间 nslookup 失败
  建议：设置 CoreDNS podAntiAffinity 分散节点；恢复后扩容到 3 副本
```

---

## 使用方式

将此案例描述作为用户输入，粘贴到诊断系统的聊天界面，观察 Agent 是否能：

1. 正确规划诊断步骤
2. 分层定位三个独立根因
3. 识别 etcd 和 CoreDNS 之间的因果链
4. 区分节点级 OOM 和 cgroup OOM
5. 给出可操作的建议而非纯理论分析
6. 完成后更新 LEARNINGS.md
