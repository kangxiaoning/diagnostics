#!/bin/bash
# collect_linux_quick_check.sh — 60 秒快速诊断 (Brendan Gregg 方法论)
# 用法: bash collect_linux_quick_check.sh
# 参考: Linux Performance Analysis in 60,000 Milliseconds

set -euo pipefail

echo "========== Linux 快速诊断 =========="
echo "主机名: $(hostname)"
echo "时间: $(date -Iseconds)"

echo ""
echo ">>> 1. uptime — 系统负载"
uptime

echo ""
echo ">>> 2. dmesg — 内核错误 (最近 20 行)"
dmesg -T 2>/dev/null | tail -20 || dmesg | tail -20

echo ""
echo ">>> 3. vmstat — 虚拟内存统计 (采样 3 次, 间隔 1s)"
vmstat 1 3 2>/dev/null || vmstat 1 3

echo ""
echo ">>> 4. mpstat — 每 CPU 利用率"
mpstat -P ALL 1 1 2>/dev/null || echo "(mpstat not available)"

echo ""
echo ">>> 5. pidstat — 进程级统计 (top 5 CPU)"
pidstat 1 1 2>/dev/null | sort -rnk8 | head -6 || echo "(pidstat not available)"

echo ""
echo ">>> 6. iostat — 磁盘 IO 统计"
iostat -xz 1 1 2>/dev/null || iostat -x 1 1

echo ""
echo ">>> 7. free — 内存使用"
free -m

echo ""
echo ">>> 8. sar — 网络接口吞吐量"
sar -n DEV 1 1 2>/dev/null || echo "(sar not available)"

echo ""
echo ">>> 9. sar — TCP 重传"
sar -n TCP,ETCP 1 1 2>/dev/null || echo "(sar TCP not available)"

echo ""
echo ">>> 10. top — 进程快照 (batch mode, top 10)"
top -b -n 1 2>/dev/null | head -17 || echo "(top not available)"

echo ""
echo "========== 快速诊断完成 =========="
