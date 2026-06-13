#!/bin/bash
# collect_runtime_logs.sh — 收集容器运行时日志
# 用法: bash collect_runtime_logs.sh <runtime> [since] [tail_lines]
# runtime: docker | containerd | kata | crio
# since:    journalctl --since 格式，默认 "1 hour ago"
# tail_lines: 保留最近行数，默认 200

set -euo pipefail

RUNTIME="${1:-}"
SINCE="${2:-1 hour ago}"
TAIL="${3:-200}"

if [ -z "$RUNTIME" ]; then
  echo "用法: $0 <docker|containerd|kata|crio> [since] [tail_lines]"
  exit 1
fi

echo "========== 容器运行时日志: $RUNTIME (since: $SINCE, tail: $TAIL) =========="

case "$RUNTIME" in
  docker)
    echo "--- docker.service ---"
    journalctl --since "$SINCE" -u docker.service --no-pager -n "$TAIL" 2>/dev/null || echo "(docker.service not found)"
    echo ""
    echo "--- dockerd 日志文件 (tail $TAIL) ---"
    if [ -f /var/log/dockerd.log ]; then
      tail -n "$TAIL" /var/log/dockerd.log 2>/dev/null || echo "(empty)"
    elif [ -f /var/log/docker.log ]; then
      tail -n "$TAIL" /var/log/docker.log 2>/dev/null || echo "(empty)"
    else
      # 尝试从 journald json 输出
      journalctl --since "$SINCE" -u docker -o json --no-pager 2>/dev/null | tail -n "$TAIL" || echo "(no docker logs found)"
    fi
    echo ""
    echo "--- docker info (关键行) ---"
    docker info 2>/dev/null | grep -E "Server Version|Storage Driver|Cgroup Driver|Runtimes|Containers (Running|Paused|Stopped)" || echo "(docker info failed)"
    ;;

  containerd)
    echo "--- containerd.service ---"
    journalctl --since "$SINCE" -u containerd.service --no-pager -n "$TAIL" 2>/dev/null || echo "(containerd.service not found)"
    echo ""
    echo "--- containerd 日志文件 ---"
    for f in /var/log/containerd.log /var/log/containerd/containerd.log; do
      if [ -f "$f" ]; then
        echo "[$f]"
        tail -n "$TAIL" "$f" 2>/dev/null
        break
      fi
    done
    echo ""
    echo "--- containerd 已知错误（含 OOM/超时/runc） ---"
    journalctl --since "$SINCE" -u containerd.service --no-pager 2>/dev/null \
      | grep -iE "error|timeout|oom|runc|failed|killed" | tail -n "$TAIL" || echo "(no matches)"
    ;;

  kata)
    echo "--- kata-containers.service ---"
    journalctl --since "$SINCE" -u kata-containers.service --no-pager -n "$TAIL" 2>/dev/null || echo "(kata-containers.service not found)"
    echo ""
    echo "--- kata-runtime 日志 ---"
    for f in /var/log/kata-containers/kata-runtime.log /var/log/kata-containers/*.log; do
      if [ -f "$f" ]; then
        echo "[$f]"
        tail -n "$TAIL" "$f" 2>/dev/null
      fi
    done
    echo ""
    echo "--- kata 已知错误（含 guest 超时/agent 错误/沙箱失败） ---"
    journalctl --since "$SINCE" -u kata-containers.service --no-pager 2>/dev/null \
      | grep -iE "error|timeout|guest|agent|sandbox|failed|qemu" | tail -n "$TAIL" || echo "(no matches)"
    ;;

  crio)
    echo "--- crio.service ---"
    journalctl --since "$SINCE" -u crio.service --no-pager -n "$TAIL" 2>/dev/null || echo "(crio.service not found)"
    echo ""
    echo "--- CRI-O 已知错误 ---"
    journalctl --since "$SINCE" -u crio.service --no-pager 2>/dev/null \
      | grep -iE "error|timeout|oom|failed|killed|CNI" | tail -n "$TAIL" || echo "(no matches)"
    ;;

  *)
    echo "不支持的 runtime: $RUNTIME (可选: docker, containerd, kata, crio)"
    exit 1
    ;;
esac

echo "========== $RUNTIME 日志收集完成 =========="
