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

### 2026-06-06 Spring Boot OOMKilled — limit 过小非泄漏
- **现象**：ACK 集群 java-backend Pod 频繁 OOMKilled 重启（每小时 10+ 次），memory limit=512Mi，用户不确定是泄漏还是 limit 太小
- **排查路径**：check_kubernetes_pods → check_kubernetes_nodes → check_memory → check_processes → memory-expert 深度分析
- **根因**：512Mi limit 过小。Spring Boot JVM 稳态 RSS ~623 MiB，超过 cgroup 限制触发 OOMKilled。非代码泄漏。
- **特征信号**：节点 MemoryPressure=False + swap=0 + Java RSS 稳定且 > limit + 进程状态 S（sleeping）→ 判定为 limit 不足而非泄漏

<!-- Last consolidation: 2026-06-07 13:19 -->
<!-- Total unique entries: 2, removed duplicates: 0 -->
