#!/bin/bash
# collect_disk_diagnostics.sh — 磁盘 IO 深度诊断
# 用法: bash collect_disk_diagnostics.sh

set -euo pipefail

echo "========== 磁盘 IO 深度诊断 =========="
echo "主机名: $(hostname)"

echo ""
echo ">>> df — 磁盘空间"
df -hT | grep -vE "tmpfs|devtmpfs|overlay|snap" | head -20

echo ""
echo ">>> df — inode 使用"
df -i | grep -vE "tmpfs|devtmpfs|overlay" | head -20

echo ""
echo ">>> iostat — 磁盘利用率 (%util)、队列长度 (avgqu-sz)、延迟 (await/svctm)"
iostat -xz 1 2 2>/dev/null | grep -v "^$" | tail -30
if [ $? -ne 0 ]; then
  iostat -x 1 2 2>/dev/null | tail -30
fi

echo ""
echo ">>> /proc/diskstats — 磁盘读写 IO 总量与耗时"
grep -E "sd[a-z] |vd[a-z] |nvme|xvd" /proc/diskstats 2>/dev/null | head -10

echo ""
echo ">>> lsblk — 块设备与挂载"
lsblk -o NAME,SIZE,TYPE,MOUNTPOINT,FSTYPE 2>/dev/null | head -20

echo ""
echo ">>> 磁盘调度器"
for d in /sys/block/sd*/queue/scheduler /sys/block/vd*/queue/scheduler /sys/block/nvme*/queue/scheduler; do
  [ -f "$d" ] && echo "$(dirname $(dirname $d)): $(cat $d)"
done

echo ""
echo ">>> 文件系统类型与挂载选项"
mount | grep -E "^/dev" | head -20

echo ""
echo ">>> 大量写入进程 (top 5 by write_bytes via /proc)"
for pid in $(ls /proc 2>/dev/null | grep -E '^[0-9]+$' | head -200); do
  wb=$(awk '/write_bytes/ {print $2}' "/proc/$pid/io" 2>/dev/null || echo 0)
  [ "$wb" -gt 0 ] 2>/dev/null && echo "$pid $wb"
done | sort -rnk2 | head -5 | while read pid bytes; do
  name=$(cat "/proc/$pid/comm" 2>/dev/null || echo "?")
  echo "  $name (PID $pid): $(numfmt --to=iec $bytes 2>/dev/null || echo ${bytes}B) written"
done

echo ""
echo "========== 磁盘 IO 诊断完成 =========="
