#!/bin/bash
# collect_memory_diagnostics.sh — 内存深度诊断
# 用法: bash collect_memory_diagnostics.sh

set -euo pipefail

echo "========== 内存深度诊断 =========="
echo "主机名: $(hostname)"

echo ""
echo ">>> free — 内存总览"
free -h

echo ""
echo ">>> /proc/meminfo (关键行)"
grep -E "^(MemTotal|MemFree|MemAvailable|Buffers|Cached|SwapTotal|SwapFree|SwapCached|Dirty|Writeback|Slab|SReclaimable|SUnreclaim|PageTables|AnonPages|Mapped|Shmem|KernelStack)" /proc/meminfo 2>/dev/null

echo ""
echo ">>> vmstat — swap 换入换出 (si/so)"
vmstat 1 3 2>/dev/null | tail -4

echo ""
echo ">>> Top 10 RSS 进程"
ps aux 2>/dev/null | sort -rnk6 | head -11

echo ""
echo ">>> OOM Killer 事件 (dmesg)"
dmesg -T 2>/dev/null | grep -i "oom\|killed process\|out of memory" | tail -20 || echo "(no recent OOM events)"

echo ""
echo ">>> OOM 评分 (top 10)"
for pid in $(ps aux 2>/dev/null | sort -rnk6 | awk 'NR>1{print $2}' | head -10); do
  if [ -f "/proc/$pid/oom_score" ]; then
    name=$(cat "/proc/$pid/comm" 2>/dev/null || echo "?")
    score=$(cat "/proc/$pid/oom_score" 2>/dev/null)
    echo "  PID $pid ($name): oom_score=$score"
  fi
done

echo ""
echo ">>> Slab 内存 (/proc/slabinfo top 10)"
sort -rnk3 /proc/slabinfo 2>/dev/null | head -12 | column -t || echo "(slabinfo not readable)"

echo ""
echo ">>> 大页 (HugePages)"
grep -E "HugePages_Total|HugePages_Free|HugePages_Rsvd|Hugepagesize" /proc/meminfo 2>/dev/null

echo ""
echo ">>> Cgroup 顶层内存 (若存在)"
cat /sys/fs/cgroup/memory/memory.usage_in_bytes 2>/dev/null | awk '{printf "cgroup v1 total: %d MB\n", $1/1024/1024}' || echo "(cgroup v1 not available)"
cat /sys/fs/cgroup/memory.current 2>/dev/null | awk '{printf "cgroup v2 total: %d MB\n", $1/1024/1024}' || echo "(cgroup v2 not available)"

echo ""
echo "========== 内存诊断完成 =========="
