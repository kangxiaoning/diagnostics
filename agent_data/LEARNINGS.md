# 历史诊断经验库

本文档由 Agent 在每次诊断后自动更新，记录可复用的诊断模式和经验。

---

### 2026-06-06 Spring Boot OOMKilled — limit 过小非泄漏
- **现象**：ACK 集群 java-backend Pod 频繁 OOMKilled 重启（每小时 10+ 次），memory limit=512Mi，用户不确定是泄漏还是 limit 太小
- **排查路径**：check_kubernetes_pods → check_kubernetes_nodes → check_memory → check_processes → memory-expert 深度分析
- **根因**：512Mi limit 过小。Spring Boot JVM 稳态 RSS ~623 MiB，超过 cgroup 限制触发 OOMKilled。非代码泄漏。
- **特征信号**：节点 MemoryPressure=False + swap=0 + Java RSS 稳定且 > limit + 进程状态 S（sleeping）→ 判定为 limit 不足而非泄漏

<!-- Last consolidation: 2026-06-07 13:19 -->
<!-- Total unique entries: 1, removed duplicates: 0 -->
