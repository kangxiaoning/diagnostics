#!/bin/bash
# collect_gpu_diagnostics.sh — NVIDIA GPU 节点诊断信息收集（只读，无破坏性操作）
# 用法: bash collect_gpu_diagnostics.sh [gpu_index] [since]
# gpu_index: 指定 GPU 索引 (0-based)，默认 0，传 "all" 查询所有 GPU
# since:     过滤最近 N 秒的 dmesg 日志，默认 3600 (1小时)

set -euo pipefail

GPU_IDX="${1:-all}"
DMESG_SINCE="${2:-3600}"

echo "========== GPU 诊断 (GPU: $GPU_IDX, dmesg since: ${DMESG_SINCE}s) =========="
echo "主机名: $(hostname)"
echo "时间: $(date -Iseconds)"

# ── 检查 nvidia-smi ──
if ! command -v nvidia-smi &>/dev/null; then
  echo "[ERROR] nvidia-smi not found. Is NVIDIA driver installed?"
  exit 1
fi

# ── GPU 基础信息 ──
echo ""
echo ">>> GPU 列表与驱动版本"
nvidia-smi --query-gpu=index,name,driver_version,vbios_version,pcie.link.gen.current,pcie.link.width.current --format=csv,noheader 2>/dev/null

# ── GPU 健康状态 ──
echo ""
echo ">>> GPU 健康状态 (温度/功耗/风扇/ECC/节流)"
if [ "$GPU_IDX" = "all" ]; then
  nvidia-smi --query-gpu=index,temperature.gpu,temperature.memory,power.draw,power.limit,fan.speed,utilization.gpu,utilization.memory,clocks.sm,clocks.mem,pstate,clocks_throttle_reasons.active,ecc.errors.corrected.volatile.total,ecc.errors.uncorrected.volatile.total --format=csv 2>/dev/null
else
  nvidia-smi -i "$GPU_IDX" --query-gpu=index,temperature.gpu,temperature.memory,power.draw,power.limit,fan.speed,utilization.gpu,utilization.memory,clocks.sm,clocks.mem,pstate,clocks_throttle_reasons.active,ecc.errors.corrected.volatile.total,ecc.errors.uncorrected.volatile.total --format=csv 2>/dev/null
fi

# ── 显存使用详情 ──
echo ""
echo ">>> 显存使用"
if [ "$GPU_IDX" = "all" ]; then
  nvidia-smi --query-gpu=index,memory.total,memory.used,memory.free --format=csv 2>/dev/null
else
  nvidia-smi -i "$GPU_IDX" --query-gpu=index,memory.total,memory.used,memory.free --format=csv 2>/dev/null
fi

# ── GPU 进程列表 ──
echo ""
echo ">>> GPU 进程 (PID/显存/用户/进程名)"
if [ "$GPU_IDX" = "all" ]; then
  nvidia-smi --query-compute-apps=pid,gpu_name,used_memory,process_name --format=csv 2>/dev/null
else
  nvidia-smi -i "$GPU_IDX" --query-compute-apps=pid,gpu_name,used_memory,process_name --format=csv 2>/dev/null
fi

# ── GPU 错误 (dmesg) ──
echo ""
echo ">>> dmesg GPU 相关错误 (最近 ${DMESG_SINCE}s)"
dmesg -T 2>/dev/null \
  | awk -v since="$(date -d "-${DMESG_SINCE} seconds" '+%s' 2>/dev/null || echo 0)" \
    '{
      ts=$1" "$2" "$3
      cmd="date -d \""ts"\" +%s 2>/dev/null"
      cmd|getline epoch
      close(cmd)
      if(epoch+0>=since) print
    }' \
  | grep -iE "nvidia|nvrm|xid|gpu|nv_" | tail -n 50 || echo "(dmesg GPU: no matches or dmesg not readable)"

# ── 备选：journalctl 中的 NVIDIA 错误 ──
echo ""
echo ">>> journalctl NVIDIA/kernel 相关错误"
journalctl -k --no-pager 2>/dev/null \
  | grep -iE "nvidia|nvrm|xid|gpu" | tail -n 30 || echo "(no journalctl GPU entries)"

# ── nvidia-persistenced 状态 ──
echo ""
echo ">>> nvidia-persistenced 状态"
systemctl status nvidia-persistenced --no-pager -l 2>/dev/null | head -10 || echo "(nvidia-persistenced not found)"

# ── GPU 拓扑 (NVLink / PCIe) ──
echo ""
echo ">>> GPU 拓扑 (P2P / NVLink)"
nvidia-smi topo -m 2>/dev/null | head -20 || echo "(nvidia-smi topo not available)"

echo ""
echo "========== GPU 诊断完成 =========="
