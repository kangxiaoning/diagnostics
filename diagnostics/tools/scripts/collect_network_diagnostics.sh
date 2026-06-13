#!/bin/bash
# collect_network_diagnostics.sh — 网络深度诊断
# 用法: bash collect_network_diagnostics.sh

set -euo pipefail

echo "========== 网络深度诊断 =========="
echo "主机名: $(hostname)"

echo ""
echo ">>> 接口信息 (ip addr)"
ip addr show 2>/dev/null | grep -E "^[0-9]|inet |link/" | head -20

echo ""
echo ">>> 接口统计与错误"
ip -s link show 2>/dev/null | grep -A4 -E "^[0-9]" | head -30

echo ""
echo ">>> 接口错误详情 (ethtool -S)"
for iface in $(ls /sys/class/net/ 2>/dev/null | grep -v lo); do
  ethtool -S "$iface" 2>/dev/null | grep -iE "err|drop|miss|discard|overflow|fifo|collision" | head -8 | while read line; do
    echo "  $iface: $line"
  done
done

echo ""
echo ">>> TCP 连接状态统计 (ss)"
ss -s 2>/dev/null

echo ""
echo ">>> CLOSE_WAIT / TIME_WAIT 连接 (top 10)"
ss -tan 2>/dev/null | awk '{print $1}' | sort | uniq -c | sort -rn | head -10

echo ""
echo ">>> TCP 重传统计 (netstat -s)"
netstat -s 2>/dev/null | grep -iE "retrans|timeout|reset|failed|drop" | head -15 || ss -ti 2>/dev/null | grep -c "retrans"

echo ""
echo ">>> 路由表"
ip route show 2>/dev/null | head -20

echo ""
echo ">>> DNS 配置"
cat /etc/resolv.conf 2>/dev/null

echo ""
echo ">>> Conntrack 连接跟踪"
conntrack_count=$(cat /proc/sys/net/netfilter/nf_conntrack_count 2>/dev/null || echo 0)
conntrack_max=$(cat /proc/sys/net/netfilter/nf_conntrack_max 2>/dev/null || echo 0)
echo "  conntrack: $conntrack_count / $conntrack_max ($(awk "BEGIN {printf \"%.0f\", $conntrack_count*100/$conntrack_max}" 2>/dev/null || echo 0)%)"

echo ""
echo ">>> 监听端口 (ss -tlnp)"
ss -tlnp 2>/dev/null | head -25

echo ""
echo "========== 网络诊断完成 =========="
