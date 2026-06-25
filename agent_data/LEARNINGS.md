# 历史诊断经验库

本文档由 Agent 在每次诊断后自动更新，记录可复用的诊断模式和经验。

---

### 2026-06-06 跨层故障链 — etcd 延迟/节点宕机 → API Server 超时 → DNS 中断 → OOMKilled 级联

- **现象**：kubectl get pods 超时 + worker-3 NotReady + CoreDNS 间歇性解析失败 + api-gateway OOMKilled
- **排查路径**：get_system_overview → check_kubernetes_nodes → check_kubernetes_control_plane → 委派 k8s-node-expert + k8s-control-plane-expert + memory-expert + DNS 分析
- **根因**：etcd 延迟抖动（凌晨备份窗口）或 worker-3 硬件故障触发级联。CoreDNS 单副本将局部故障放大为全局故障。api-gateway OOMKilled 是独立容量问题。
- **特征信号**：
  - kubectl 超时但控制面 Healthy → etcd 延迟而非组件故障
  - worker-3 完全不在节点列表中 → 节点宕机而非 NotReady
  - CoreDNS 单副本 + 重启历史 → DNS 单点故障
  - OOMKilled 与凌晨故障时间重合但无因果关系 → 独立问题
- **关键教训**：生产集群的单点故障（CoreDNS 单副本）会将可隔离的故障放大为全局故障。诊断时需区分"触发层"和"放大层"。

---

### 2026-06-14 prod-cluster — conntrack 表满 → 节点消失 → Pod 大量驱逐

- **现象**：worker-5/worker-8 节点几分钟内先后 NotReady 后从集群列表消失，30+ Pod 被驱逐，CoreDNS 解析偶发超时
- **排查路径**：get_cluster_overview → check_kubernetes_nodes → check_kubernetes_control_plane → check_network → check_etcd_health → 委派 network-expert + k8s-cluster-expert
- **根因**：worker-5/worker-8 主机 `nf_conntrack_max` 上限耗尽，内核丢弃 kubelet 心跳包导致节点失联
- **特征信号**：
  - dmesg 中 `nf_conntrack: table full, dropping packet`（直接证据）
  - 节点从 API server 节点列表中完全消失（非 NotReady）
  - CoreDNS 解析偶发超时（DNS UDP 包被内核丢弃）
  - 控制面全部 Healthy + etcd NOSPACE 为 WARNING 级别 → 排除控制面故障
- **关联复发**：与 2026-06-16 prod-us-east conntrack 故障模式一致，同一根因复发
- **关键教训**：集群稳定运行 8 个月后连接数累积可触发 conntrack 满；节点消失（非 NotReady）是 conntrack 满的严重级联结果；etcd NOSPACE 告警需独立处理，与 conntrack 满无直接因果

### 2026-06-16 prod-us-east — conntrack 表满 → 节点脱离集群 → Pod 大量驱逐（复发，修复待执行）

- **现象**：worker-5/worker-8 节点几分钟内先后 NotReady 后脱离集群，30+ Pod 被驱逐，生产服务不可用
- **排查路径**：get_cluster_overview → check_kubernetes_nodes → check_kubernetes_control_plane → check_etcd_health → check_network → 委派 network-expert（conntrack-diagnosis）+ k8s-cluster-expert（cross-layer-diagnosis）
- **根因**：`nf_conntrack_max` 默认值过低 + `nf_conntrack_tcp_timeout_established` 默认 5 天 → 连接缓慢累积 8 个月后耗尽
- **特征信号**：
  - dmesg 中 `nf_conntrack: table full, dropping packet`（直接证据）
  - 节点完全脱离集群（非 NotReady）
  - CoreDNS 解析偶发超时（DNS UDP 包被 conntrack 丢弃）
  - 控制面 Healthy + etcd NOSPACE WARNING → 排除控制面故障
- **关联复发**：与 2026-06-14 prod-cluster conntrack 故障模式一致，同一根因在不同集群复发
- **关键教训**：生产集群部署时必须提前调优 conntrack 参数（nf_conntrack_max ≥ 524288，tcp_timeout ≤ 7200），不能依赖默认值；连接数累积是渐进式故障，监控告警需在达到 80% 时触发
- **修复状态**：conntrack 参数修复和 etcd NOSPACE 处理待执行

---

### 2026-06-25 prod-us-east — conntrack 表满 → 节点 NotReady → Pod 大量驱逐（第三次复发，修复仍待执行）

- **现象**：worker-5/worker-8 节点几分钟内先后 NotReady，30+ Pod 被驱逐，生产服务不可用
- **排查路径**：get_system_overview → list_clusters → get_cluster_overview → check_kubernetes_nodes → check_network → check_processes → get_coredns_logs → check_etcd_health → check_kubernetes_pods → get_node_conditions → commit_hypotheses → 委派 network-expert → get_etcd_metrics → get_etcd_logs → get_cluster_events → record_finding × 3
- **根因**：`nf_conntrack_max` 上限耗尽（与 2026-06-14 prod-cluster 和 2026-06-16 prod-us-east 同一根因）
- **特征信号**：dmesg `nf_conntrack: table full` + 两个节点同时 NotReady + etd NOSPACE WARNING + 集群运行 8 个月无变更
- **关联复发**：同一集群 prod-us-east 第三次出现 conntrack 表满故障（2026-06-16、2026-06-25），修复措施仍未执行
- **关键教训**：conntrack 参数修复是 P0 优先级，不应反复复发；etcd NOSPACE 需独立处理；诊断工具无法直接获取 sysctl 值（工具缺口）

---

### 2026-06-06 Spring Boot OOMKilled — limit 过小非泄漏
- **现象**：ACK 集群 java-backend Pod 频繁 OOMKilled 重启（每小时 10+ 次），memory limit=512Mi，用户不确定是泄漏还是 limit 太小
- **排查路径**：check_kubernetes_pods → check_kubernetes_nodes → check_memory → check_processes → memory-expert 深度分析
- **根因**：512Mi limit 过小。Spring Boot JVM 稳态 RSS ~623 MiB，超过 cgroup 限制触发 OOMKilled。非代码泄漏。
- **特征信号**：节点 MemoryPressure=False + swap=0 + Java RSS 稳定且 > limit + 进程状态 S（sleeping）→ 判定为 limit 不足而非泄漏

<!-- Last consolidation: 2026-06-07 13:19 -->
<!-- Total unique entries: 2, removed duplicates: 0 -->
