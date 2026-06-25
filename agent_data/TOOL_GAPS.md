# 工具缺口记录

---

### 2026-06-25 conntrack-diagnosis — 无法直接获取 sysctl 值和 conntrack 表统计

- **缺失项**：
  - `sysctl net.netfilter.nf_conntrack_max` 实际值
  - `sysctl net.netfilter.nf_conntrack_count` 当前条目数
  - `conntrack -C` 当前 conntrack 表大小
  - `conntrack -S` conntrack 统计（entries, found, new, invalid, ignore, drop, early_drop, error, search_restart）
  - `cat /proc/sys/net/netfilter/nf_conntrack_tcp_timeout_established` TCP 建立连接超时值
  - `conntrack -L` 按协议/状态分类统计（需 conntrack 工具）
- **诊断上下文**：验证 H1（nf_conntrack_max 耗尽）时，需要量化 conntrack 表的使用率（entries/max 百分比），确认是否接近饱和阈值。当前 check_network 工具未返回 conntrack 相关指标。
- **建议采集方式**：
  - `sysctl net.netfilter.nf_conntrack_max`
  - `sysctl net.netfilter.nf_conntrack_count`
  - `conntrack -C`
  - `conntrack -S`
  - `cat /proc/sys/net/netfilter/nf_conntrack_tcp_timeout_established`
- **优先级**：高（阻塞 conntrack 诊断的量化验证）

---

### 2026-06-25 conntrack-diagnosis — 无法获取 worker-5/worker-8 独立 dmesg 日志

- **缺失项**：远程节点 dmesg 日志（conntrack 相关条目）
- **诊断上下文**：需要确认 worker-5 和 worker-8 各自何时出现 `nf_conntrack: table full`，以判断是同时饱和还是先后饱和，辅助判断根因（是单节点异常还是集群规模导致）。
- **建议采集方式**：
  - `ssh worker-5 "dmesg | grep -i conntrack"`
  - `ssh worker-8 "dmesg | grep -i conntrack"`
  - 或通过 journald: `journalctl -k --grep=conntrack -n 50`
- **优先级**：中（提升根因判断精度，但不阻塞修复决策）

---

### 2026-06-25 conntrack-diagnosis — 无法获取 conntrack 表历史趋势

- **缺失项**：conntrack 条目数随时间变化的监控数据
- **诊断上下文**：需要确认 conntrack 条目增长趋势，判断是渐进式累积还是突增，辅助判断根因（连接泄漏 vs 正常业务增长）。
- **建议采集方式**：
  - Prometheus node_exporter `node_nf_conntrack_entries` 指标
  - 或自定义 cron + 时序数据库记录
  - 或 Grafana 历史图表
- **优先级**：中（用于预防性监控告警，不阻塞当前诊断）

---
