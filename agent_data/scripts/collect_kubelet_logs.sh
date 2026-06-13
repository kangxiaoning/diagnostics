#!/bin/bash
# collect_kubelet_logs.sh — 收集 kubelet 日志
# 用法: bash collect_kubelet_logs.sh [since] [tail_lines] [filter]
# since:     journalctl --since 格式，默认 "1 hour ago"
# tail_lines:保留最近行数，默认 200
# filter:    grep 过滤关键词 (如 PLEG, OOM, error)，默认过滤关键错误

set -euo pipefail

SINCE="${1:-1 hour ago}"
TAIL="${2:-200}"
FILTER="${3:-error|PLEG|OOM|timeout|failed|evict|oom|NotReady|Unknown|watch|lease}"

echo "========== kubelet 日志 (since: $SINCE, tail: $TAIL, filter: ${FILTER:0:50}...) =========="

echo "--- kubelet.service 状态 ---"
systemctl status kubelet --no-pager -l 2>/dev/null | head -20 || echo "(kubelet.service not found or systemctl failed)"

echo ""
echo "--- kubelet 关键错误日志 ---"
journalctl --since "$SINCE" -u kubelet --no-pager 2>/dev/null \
  | grep -iE "$FILTER" | tail -n "$TAIL" || echo "(no matching entries)"

echo ""
echo "--- kubelet 最近全部日志 ---"
journalctl --since "$SINCE" -u kubelet --no-pager -n "$TAIL" 2>/dev/null || echo "(kubelet log not available)"

echo ""
echo "--- kubelet 配置关键项 ---"
if command -v kubelet &>/dev/null; then
  kubelet --version 2>/dev/null || echo "(version unknown)"
fi
# 尝试从进程参数或配置文件获取关键配置
cat /var/lib/kubelet/config.yaml 2>/dev/null | grep -E "maxPods|evictionHard|evictionSoft|kubeReserved|systemReserved|cgroupDriver|containerRuntimeEndpoint|nodeStatusUpdateFrequency" || echo "(config not readable or not found)"

echo "========== kubelet 日志收集完成 =========="
