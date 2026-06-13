#!/bin/bash
# collect_process_diagnostics.sh — 进程深度诊断
# 用法: bash collect_process_diagnostics.sh [process_name_or_pid]

set -euo pipefail

TARGET="${1:-}"

echo "========== 进程深度诊断 =========="
echo "主机名: $(hostname)"

if [ -n "$TARGET" ]; then
  echo ""
  echo ">>> 指定进程信息: $TARGET"
  # Try as PID first, then as name pattern
  if [ -d "/proc/$TARGET" ]; then
    PID="$TARGET"
  else
    PID=$(pgrep -f "$TARGET" 2>/dev/null | head -1 || echo "")
  fi
  
  if [ -n "$PID" ] && [ -d "/proc/$PID" ]; then
    echo "  PID: $PID"
    echo "  Command: $(cat /proc/$PID/cmdline 2>/dev/null | tr '\0' ' ')"
    echo "  State: $(cat /proc/$PID/status 2>/dev/null | grep State)"
    echo "  Threads: $(cat /proc/$PID/status 2>/dev/null | grep Threads)"
    echo "  VmRSS: $(cat /proc/$PID/status 2>/dev/null | grep VmRSS)"
    echo "  VmSize: $(cat /proc/$PID/status 2>/dev/null | grep VmSize)"
    echo "  FD count: $(ls /proc/$PID/fd 2>/dev/null | wc -l)"
    echo ""
    echo "  IO stats:"
    cat /proc/$PID/io 2>/dev/null | head -8
    echo ""
    echo "  Limits:"
    cat /proc/$PID/limits 2>/dev/null | grep -E "open files|Max processes|Max locked|address space" | head -10
    echo ""
    echo "  Cgroup:"
    cat /proc/$PID/cgroup 2>/dev/null | head -5
  else
    echo "  (process not found)"
  fi
fi

echo ""
echo ">>> Top 10 CPU 进程"
ps aux 2>/dev/null | sort -rnk3 | head -11

echo ""
echo ">>> Top 10 内存进程"
ps aux 2>/dev/null | sort -rnk4 | head -11

echo ""
echo ">>> D 状态进程 (不可中断睡眠 — IO 阻塞)"
ps aux 2>/dev/null | awk 'NR==1 || $8 ~ /D/'

echo ""
echo ">>> Zombie 进程"
ps aux 2>/dev/null | awk 'NR==1 || $8 ~ /Z/'

echo ""
echo ">>> 进程总数与状态分布"
ps -eo stat 2>/dev/null | sort | uniq -c | sort -rn | head -10

echo ""
echo ">>> 系统调用统计 (strace -c, 5s 采样, 需 strace)"
if [ -n "${TARGET:-}" ] && [ -n "${PID:-}" ]; then
  timeout 5 strace -c -p "$PID" 2>/dev/null | tail -50 || echo "(strace not available or insufficient permissions)"
fi

echo ""
echo "========== 进程诊断完成 =========="
