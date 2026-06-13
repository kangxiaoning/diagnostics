#!/bin/bash
# collect_kube_proxy_logs.sh — 收集 kube-proxy 日志
# 用法: bash collect_kube_proxy_logs.sh [since] [tail_lines]
# since:     journalctl --since 格式，默认 "1 hour ago"
# tail_lines:保留最近行数，默认 200

set -euo pipefail

SINCE="${1:-1 hour ago}"
TAIL="${2:-200}"

echo "========== kube-proxy 日志 (since: $SINCE, tail: $TAIL) =========="

echo "--- kube-proxy 关键错误 ---"
journalctl --since "$SINCE" -u kube-proxy --no-pager 2>/dev/null \
  | grep -iE "error|timeout|failed|connection refused|iptables|ipvs|endpoint|Setting endpoints" \
  | tail -n "$TAIL" || echo "(no matching entries)"

echo ""
echo "--- kube-proxy 最近全量日志 ---"
journalctl --since "$SINCE" -u kube-proxy --no-pager -n "$TAIL" 2>/dev/null || echo "(kube-proxy log not available)"

echo ""
echo "--- kube-proxy 模式检测 ---"
# 从日志推断 iptables/ipvs 模式
if journalctl --since "$SINCE" -u kube-proxy --no-pager 2>/dev/null | grep -qi "ipvs"; then
  echo "mode: likely ipvs"
elif journalctl --since "$SINCE" -u kube-proxy --no-pager 2>/dev/null | grep -qi "iptables"; then
  echo "mode: likely iptables"
fi

echo ""
echo "--- kube-proxy 进程状态 ---"
ps aux 2>/dev/null | grep "[k]ube-proxy" | head -3 || echo "(kube-proxy process not found)"

echo "========== kube-proxy 日志收集完成 =========="
