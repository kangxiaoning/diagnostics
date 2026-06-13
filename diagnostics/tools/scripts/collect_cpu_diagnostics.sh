#!/bin/bash
# collect_cpu_diagnostics.sh — CPU 深度诊断 (USE 方法)
# 用法: bash collect_cpu_diagnostics.sh [since_seconds]

set -euo pipefail
SINCE="${1:-600}"

echo "========== CPU 深度诊断 =========="
echo "主机名: $(hostname)"

echo ""
echo ">>> CPU 型号与拓扑"
lscpu 2>/dev/null | grep -E "Model name|CPU\(s\)|Thread|Core|Socket|NUMA" | head -10

echo ""
echo ">>> load average (1m/5m/15m)"
uptime

echo ""
echo ">>> vmstat — r/b 列 (运行/阻塞进程数)"
vmstat 1 3 2>/dev/null | tail -5

echo ""
echo ">>> mpstat — 每核使用率 (%usr/%sys/%iowait/%idle)"
mpstat -P ALL 1 1 2>/dev/null | grep -v "^$" | tail -20

echo ""
echo ">>> 中断分布 (/proc/interrupts)"
grep -E "CPU|RES|LOC|NMI|eth|ens|enp|mlx" /proc/interrupts 2>/dev/null | head -10

echo ""
echo ">>> 软中断 (/proc/softirqs)"
head -15 /proc/softirqs 2>/dev/null

echo ""
echo ">>> CPU 频率 / 节流"
grep -E "MHz|throttle" /proc/cpuinfo 2>/dev/null | head -5
cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq 2>/dev/null | awk '{printf "CPU0 current freq: %.0f MHz\n", $1/1000}' || echo "(cpufreq not accessible)"

echo ""
echo ">>> D 状态进程 (不可中断睡眠 — 通常是 IO 阻塞)"
ps aux 2>/dev/null | awk '$8 ~ /D/' | head -10 || echo "(no D-state processes)"

echo ""
echo ">>> 上下文切换速率 (pidstat)"
pidstat -w 1 1 2>/dev/null | tail -10 || echo "(pidstat -w not available)"

echo ""
echo ">>> 调度器延迟 (perf sched, 需 root)"
perf sched record -a -g -- sleep 2 2>/dev/null && perf sched latency -p 2>/dev/null | head -15 || echo "(perf sched requires root/perf_event_paranoid=-1)"

echo ""
echo "========== CPU 诊断完成 =========="
