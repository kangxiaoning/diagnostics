#!/bin/bash
# check_gpu_xid_errors.sh — 检查 NVIDIA GPU Xid 错误（只读，无破坏性操作）
# 用法: bash check_gpu_xid_errors.sh [hours]
# hours: 检查最近 N 小时的 dmesg，默认 24

set -euo pipefail

HOURS="${1:-24}"

echo "========== GPU Xid 错误检查 (最近 ${HOURS}h) =========="
echo "主机名: $(hostname)"
echo "时间: $(date -Iseconds)"

if ! command -v nvidia-smi &>/dev/null; then
  echo "[ERROR] nvidia-smi not found."
  exit 1
fi

# Xid 错误码速查 (NVIDIA 官方分类)
# 硬件错误: 31, 32, 43, 44, 45, 48, 61, 62, 63, 64, 74, 79, 92, 94, 95
# 驱动错误: 13, 16, 17, 18, 25, 28, 38, 42, 46, 49, 56, 68, 80, 81, 82
# 应用错误: 4, 8, 12, 22, 31, 41, 45, 63, 64, 94, 95 (与硬件重叠的应用可触发错误)
# 用户错误: 7, 30, 34, 52, 53, 55, 57, 58, 59

echo ""
echo ">>> dmesg Xid 错误"
DMESG_OUT=$(dmesg -T 2>/dev/null | grep -i "xid\|NVRM:" | tail -n 50 || echo "")
if [ -z "$DMESG_OUT" ]; then
  echo "(无 Xid 错误)"
else
  echo "$DMESG_OUT"

  # 统计 Xid 错误码分布
  echo ""
  echo ">>> Xid 错误码分布"
  echo "$DMESG_OUT" | grep -oP 'Xid\s*[:\s]*\K\d+' | sort | uniq -c | sort -rn | while read count code; do
    case "$code" in
      31|32|43|44|45|48)  CAT="硬件错误" ;;
      61|62|63|64)         CAT="ECC 错误" ;;
      74|79)               CAT="NVLink 错误" ;;
      13|16|28|38|42)      CAT="驱动超时/挂起" ;;
      92|94|95)            CAT="PCIe 错误" ;;
      4|8|12|22|41)        CAT="应用错误(GPU 内存越界/非法操作)" ;;
      7|30|34|52|53|55|57|58|59) CAT="用户/配置错误" ;;
      *)                   CAT="其他" ;;
    esac
    echo "  Xid $code ($CAT): 出现 $count 次"
  done
fi

# ── ECC 错误计数器 ──
echo ""
echo ">>> ECC 易失性错误 (本次运行)"
nvidia-smi --query-gpu=index,name,ecc.errors.corrected.volatile.total,ecc.errors.uncorrected.volatile.total --format=csv 2>/dev/null

echo ""
echo "========== Xid 错误检查完成 =========="
