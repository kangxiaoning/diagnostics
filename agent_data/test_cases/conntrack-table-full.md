# 验证案例：Conntrack 表满 → 节点失联 → Pod 驱逐

## 概述

这是一个**内核层跨层故障**案例，模拟 Kubernetes 集群中 `nf_conntrack` 表溢出导致 kubelet 心跳中断、节点 NotReady、Pod 被驱逐的连锁故障。用于验证假设驱动诊断（Hypothesis-Ledger）模式下：

1. 从模糊症状中识别 conntrack 饱和特征信号
2. 正确形成和排序假设（H1: conntrack 溢出 vs H2: 硬件故障 vs H3: etcd 故障）
3. 委派 expert 子代理执行深度验证
4. 区分直接证据（dmesg）和间接证据（DNS 超时）
5. 满足退出条件后生成结构化报告

---

## 用户输入（模拟运维工程师的初始描述）

> 集群 prod-us-east 中生产服务突然不可用，worker-5 和 worker-8 两个节点几分钟内先后变成 NotReady，上面运行的 30+ 个 Pod 被大量驱逐。运维反馈集群已稳定运行 8 个月，最近没有发布变更。初步查看 dmesg 发现 `nf_conntrack: table full, dropping packet` 警告，同时 CoreDNS 解析偶发超时。

---

## 真实根因（验证者已知，用于判断诊断结果正确性）

| 层级 | 组件 | 故障 | 根因 |
|------|------|------|------|
| **内核层** | nf_conntrack | conntrack 表溢出 | `nf_conntrack_max=131072` 默认值过低，服务网格 sidecar（envoy）和 api-gateway 产生大量短连接，8 个月累积后达到上限 |
| **集群层** | kubelet | worker-5/8 NotReady | conntrack 表满导致 kubelet 到 API Server 的 TCP 心跳包被内核丢弃，节点 lease 续约超时 |
| **应用层** | api-gateway + CoreDNS | DNS 超时 + CrashLoopBackOff | CoreDNS UDP 查询包被 conntrack 丢弃；api-gateway 依赖 DNS 解析后端服务，探针超时后 CrashLoopBackOff |

### 因果链

```
nf_conntrack_max=131072 (默认值)
  + envoy sidecar ESTAB 4500+
  + api-gateway ESTAB 5000+
  + 8个月累积连接不回收
  └─→ conntrack table full (131072/131072)
        ├─→ kubelet → API Server TCP 心跳包被内核丢弃
        │     └─→ worker-5/worker-8 lease 续约超时
        │           └─→ NodeController 5min 后驱逐 Pod
        │                 └─→ 30+ Pod 被驱逐
        ├─→ CoreDNS UDP 查询包被内核丢弃
        │     └─→ CoreDNS 解析偶发超时
        │           └─→ api-gateway 探针 DNS 解析失败
        │                 └─→ ReadinessProbeFailed → CrashLoopBackOff
        └─→ 网卡 TX dropped=3840 (软中断处理不过来的新增连接请求)
```

---

## 用到的 Scenario

设置场景：
```python
from diagnostics.tools.mock.scenarios import set_scenario
set_scenario("conntrack_table_full")
```

---

## 预期诊断路径

### Phase 1: UNDERSTAND — 理解故障

Agent 应首先并行采集系统概览信息：

| 工具 | 调用预期 | 关键发现 |
|------|----------|----------|
| `get_system_overview()` | **必须首调** | 集群节点数量、整体状态 |
| `check_kubernetes_nodes()` | 并行 | worker-5/8 NotReady，NetworkUnavailable=Unknown |
| `get_cluster_events()` | 可选 | ReadinessProbeFailed（DNS lookup timeout） |

### Phase 2: HYPOTHESIZE — 形成假设

Agent 应调用 `commit_hypotheses` 提交 ≥2 个假设（上限 3 个）：

| 假设 | 概率 | 基础 |
|------|------|------|
| **H1**: nf_conntrack_max 上限耗尽 → kubelet 心跳包被丢弃 → 节点失联 | **≥80%** | 用户已提供 dmesg 直接证据 |
| **H2**: worker-5/8 硬件故障（网卡/磁盘/OOM）→ 节点失联 | ≤10% | 双节点同时硬件故障概率低 |
| **H3**: etcd NOSPACE → API Server 写入失败 → 节点心跳无法注册 | ≤10% | 用于排除控制面故障 |

### Phase 3: VERIFY — 验证假设

Agent 应选择 H1 作为主验证路径（`select_path`），然后并行采集验证证据：

**被调用的工具**：

| 工具 | 目的 | 预期发现 |
|------|------|----------|
| `check_network()` | 确认 conntrack 溢出直接证据 | dmesg: `nf_conntrack: table full (131072/131072 entries)`，8562 ESTAB 连接，TX dropped=3840 |
| `check_processes()` | 识别高连接来源进程 | api-gateway ESTAB 5000+，envoy sidecar ESTAB 4500+ |
| `check_cpu()` | 确认网络软中断开销 | sys% 45%（高 softirq） |
| `check_kubernetes_control_plane()` | 排除 H3（etcd 故障） | API Server Healthy，etcd 健康 |
| `check_etcd_health()` | 排除 H3（etcd 故障） | 3 成员均 Healthy，commit 延迟正常 |
| `get_coredns_logs()` | 确认 DNS 层面影响 | CoreDNS DNS 解析超时日志 |
| `check_kubernetes_pods()` | 确认应用层影响 | api-gateway CrashLoopBackOff（DNS 超时→探针失败） |

### Phase 4: 深度验证（委派 Expert）

Agent 应委派以下 expert 子代理：

| Expert | 委派的 Skill | 验证内容 |
|--------|-------------|----------|
| `network-expert` | `conntrack-diagnosis` | 执行 conntrack 诊断工作流：验证饱和度 → 评估集群影响 → 识别高连接源 → 检查 CoreDNS 影响 → 根因分类 |
| `k8s-cluster-expert` | `cross-layer-diagnosis` | 跨层确认：conntrack 溢出 → kubelet 心跳丢失 → 节点 NotReady → Pod 驱逐的完整因果链 |

### Phase 5: EVALUATE — 评估路径

Agent 应：

| 操作 | 工具 |
|------|------|
| 记录 H1 验证结果 | `record_finding` → H1 confirmed (p≥80%) |
| 排除 H2/H3 | `record_finding` → H2/H3 deprioritized |
| 确认根因并进入报告阶段 | 自动触发 REPORT phase |

### Phase 6: REPORT — 生成报告

满足退出条件（H1 confirmed p≥90%）后进入 REPORT 阶段，生成包含以下内容的诊断报告：

- 故障概述
- 证据链与时间线
- 根因分析（含排除的假设及原因）
- 影响范围
- 修复建议（P0 紧急修复：调优 conntrack 参数 + P1 长期改进：监控告警、CoreDNS 高可用）

---

## 验证检查清单

### Skills 覆盖验证

| Skill | 是否触发 | 触发条件 |
|-------|----------|----------|
| `conntrack-diagnosis` | ✓ | dmesg `nf_conntrack: table full` + DNS 超时 + 节点 NotReady |
| `cross-layer-diagnosis` | ✓ | 内核层 conntrack 满 → 集群层节点失联 → 应用层 Pod 驱逐 |
| `network-diagnosis` | ✓ | 网络丢包 + TCP 重传 + DNS 超时 |
| `control-plane-diagnosis` | ✓ | 排除 etcd/API Server 故障 |

### Tools 覆盖验证

| Tool | 调用预期 |
|------|----------|
| `get_system_overview()` | **必须首调** |
| `check_kubernetes_nodes()` | 检测 worker-5/8 NotReady + NetworkUnavailable |
| `check_network()` | 检测 conntrack 溢出 dmesg + 高连接数 + 丢包 |
| `check_processes()` | 识别高连接进程（api-gateway 5000+ / envoy 4500+） |
| `check_cpu()` | sys% 45% 网络软中断确认 |
| `check_kubernetes_control_plane()` | 排除控制面故障 |
| `check_etcd_health()` | 排除 etcd 故障（H3） |
| `get_coredns_logs()` | DNS 超时日志确认 |
| `check_kubernetes_pods()` | api-gateway CrashLoopBackOff + CoreDNS 副本状态 |
| `get_cluster_events()` | 确认驱逐事件 |
| `commit_hypotheses` | 提交初始假设 |
| `select_path` | 选择 H1 验证路径 |
| `record_finding` | 记录验证结论 |
| `task` (委派 expert) | network-expert + k8s-cluster-expert 深度验证 |

### Hypothesis-Ledger 验证

| 规范 | 验证点 |
|------|--------|
| 假设数量 ≤ 3 | commit_hypotheses 是否提交 ≤3 个假设 |
| 概率分配合理 | H1>80%，H2/H3≤10% |
| 单路径 DFS 探索 | 是否先验证 H1 再考虑 H2/H3 |
| 交叉验证 | 每个结论是否由 ≥2 个工具佐证 |
| 根因确认 | H1 是否达到 p≥80% 确认阈值 |
| 报告生成 | 确认后是否自动进入 REPORT 阶段 |
| 报告完整性 | 是否包含证据链、根因分析、排除假设、修复建议 |

---

## 预期诊断结论（参考答案）

```
根因：
  nf_conntrack_max 默认值过低（131072）+ 服务网格/envoy 产生大量连接
  → conntrack 表满 (131072/131072)
  → 内核丢弃 kubelet 到 API Server 的心跳包
  → worker-5/worker-8 lease 续约超时 → Node NotReady
  → NodeController 驱逐 30+ Pod

排除的假设：
  - H2 (硬件故障): 双节点同时故障概率低，且 dmesg 明确指向 conntrack
  - H3 (etcd NOSPACE): etcd 健康检查正常，commit 延迟 2.5-3.1ms

证据链：
  1. dmesg: nf_conntrack: table full, dropping packet (直接证据)
  2. ss: 8562 ESTAB connections, TX dropped=3840
  3. processes: api-gateway ESTAB 5000+, envoy sidecar ESTAB 4500+
  4. k8s nodes: worker-5/8 NotReady (NetworkUnavailable=Unknown)
  5. control plane: API Server/etcd 全部 Healthy (排除 H3)
  6. CoreDNS: DNS 解析超时日志 (conntrack 丢弃 UDP 查询)

影响范围：
  - 受影响节点: worker-5, worker-8 (2/7)
  - 受影响 Pod: 30+ 被驱逐
  - 受影响服务: api-gateway CrashLoopBackOff, CoreDNS 副本从 3 降至 1

修复建议：
  P0:
  1. 调优 nf_conntrack_max=524288
  2. 降低 nf_conntrack_tcp_timeout_established=1800 (从默认 5 天降至 2 小时)
  P1:
  1. 部署 Prometheus node_exporter 监控 node_nf_conntrack_entries
  2. CoreDNS 增加 podAntiAffinity 分散到不同节点
  3. 评估迁移到 eBPF (bypass conntrack for pod-to-pod traffic)
```

---

## 使用方式

1. 启动诊断服务并切换场景：
   ```python
   from diagnostics.tools.mock.scenarios import set_scenario
   set_scenario("conntrack_table_full")
   ```

2. 将"用户输入"段落的内容粘贴到诊断界面聊天框，开始诊断。

3. 观察 Agent 是否能：
   - 识别 conntrack 特征信号（dmesg + 高连接数 + 多节点同时失联）
   - 正确形成假设并分配概率
   - 委派 network-expert 执行 conntrack-diagnosis skill
   - 区分直接证据（dmesg）和间接证据（DNS 超时）
   - 确认 H1 后排除 H2/H3
   - 生成包含证据链、影响范围、分级修复建议的完整报告
