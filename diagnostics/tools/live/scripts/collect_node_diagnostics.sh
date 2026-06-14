#!/bin/bash
# collect_node_diagnostics.sh — Kubernetes 节点综合诊断信息收集
# 用法: bash collect_node_diagnostics.sh <component> [since] [tail_lines]
# component: kubelet | kube-proxy | runtime | all
# runtime 需额外参数: bash collect_node_diagnostics.sh runtime <docker|containerd|kata|crio> [since]

set -euo pipefail

COMPONENT="${1:-all}"
SINCE="${3:-1 hour ago}"
TAIL="${4:-200}"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

run_script() {
  local name="$1"
  shift
  if [ -f "$SCRIPT_DIR/$name" ]; then
    bash "$SCRIPT_DIR/$name" "$@" 2>/dev/null || echo "[$name 执行失败]"
  else
    echo "[$name not found at $SCRIPT_DIR]"
  fi
}

case "$COMPONENT" in
  kubelet)
    run_script collect_kubelet_logs.sh "$SINCE" "$TAIL"
    ;;
  kube-proxy)
    run_script collect_kube_proxy_logs.sh "$SINCE" "$TAIL"
    ;;
  runtime)
    RUNTIME="${2:-containerd}"
    run_script collect_runtime_logs.sh "$RUNTIME" "$SINCE" "$TAIL"
    ;;
  all)
    echo "========== 节点综合诊断开始 =========="
    echo "主机名: $(hostname)"
    echo "时间: $(date -Iseconds)"
    echo "uptime: $(uptime)"
    echo ""

    echo ">>> kubelet"
    run_script collect_kubelet_logs.sh "$SINCE" "$TAIL"
    echo ""

    echo ">>> kube-proxy"
    run_script collect_kube_proxy_logs.sh "$SINCE" "$TAIL"
    echo ""

    # 自动检测容器运行时
    DETECTED_RUNTIME=""
    if systemctl is-active --quiet containerd 2>/dev/null; then
      DETECTED_RUNTIME="containerd"
    elif systemctl is-active --quiet docker 2>/dev/null; then
      DETECTED_RUNTIME="docker"
    elif systemctl is-active --quiet crio 2>/dev/null; then
      DETECTED_RUNTIME="crio"
    elif systemctl is-active --quiet kata-containers 2>/dev/null; then
      DETECTED_RUNTIME="kata"
    fi

    if [ -n "$DETECTED_RUNTIME" ]; then
      echo ">>> 容器运行时: $DETECTED_RUNTIME"
      run_script collect_runtime_logs.sh "$DETECTED_RUNTIME" "$SINCE" "$TAIL"
    else
      echo ">>> 容器运行时: 未检测到运行中的 runtime 服务"
    fi
    echo ""

    echo ">>> OOM 事件 (dmesg)"
    dmesg -T 2>/dev/null | grep -i "oom\|killed process" | tail -n 20 || echo "(dmesg OOM: none or dmesg not accessible)"

    echo ""
    echo ">>> 磁盘 inode/空间 (df -hTi)"
    df -hTi 2>/dev/null | grep -vE "tmpfs|devtmpfs|overlay" | head -20

    echo ""
    echo "========== 节点综合诊断完成 =========="
    ;;
  *)
    echo "用法: $0 <kubelet|kube-proxy|runtime|all> [runtime_type|since] [tail_lines]"
    echo ""
    echo "示例:"
    echo "  $0 all                                        # 收集全部组件日志 (最近1小时)"
    echo "  $0 kubelet \"2 hours ago\" 500                  # kubelet 日志, 最近2小时, 500行"
    echo "  $0 kube-proxy \"30 minutes ago\"               # kube-proxy 日志, 最近30分钟"
    echo "  $0 runtime docker \"1 hour ago\"               # docker 运行时日志"
    echo "  $0 runtime containerd \"3 hours ago\" 300       # containerd 日志"
    echo "  $0 runtime kata \"1 hour ago\"                  # kata 运行时日志"
    exit 1
    ;;
esac
