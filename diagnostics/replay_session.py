#!/usr/bin/env python3
"""真实场景复跑与验收脚本（session replay & acceptance）。

用途
----
1. 从 capture 抓包中提取某次诊断会话的原始请求参数（用户消息 + 诊断参数），
   生成可直接 POST 到 /api/chat/stream 的请求 JSON。
2. 发起复跑（后台 curl），或仅生成请求文件供手动触发。
3. 复跑完成后，对比新旧台账/日志，输出验收报告：
   - 轮次/耗时/批次/拦截次数/重复委派等效率指标对比
   - 假设终态分布（5 态语义检查）
   - 指定验收点（如"无换批拒绝"）逐项 PASS/FAIL

使用
----
  # 1) 提取 0721 会话参数生成请求文件
  uv run python diagnostics/replay_session.py extract capture/2026-0721-01 -o /tmp/replay.json

  # 2) 发起复跑（需服务已启动）
  uv run python diagnostics/replay_session.py run /tmp/replay.json --wait 900

  # 3) 会话结束后对比验收（baseline 为原会话 ledger）
  uv run python diagnostics/replay_session.py assess \\
      --baseline agent_data/traces/2026-07-21-002231-9cd030a1-ledger.json \\
      --new agent_data/traces/2026-07-21-093357-84f454ca-ledger.json

  # 一步完成（extract + run + assess）
  uv run python diagnostics/replay_session.py full capture/2026-0721-01 \\
      --baseline-ledger agent_data/traces/2026-07-21-002231-9cd030a1-ledger.json

依赖
----
- 服务：http://localhost:8000（run/full 模式需已启动）
- 日志：log/diagnostics_<today>.log（assess 提取门控/拦截事件）
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
API = "http://localhost:8000/api/chat/stream"


# ── extract ──────────────────────────────────────────────────────────

def extract_request(capture_dir: Path, out: Path | None) -> dict:
    """从 capture 抓包 1-request.json 提取用户消息与诊断参数。

    抓包中 user 消息形如：
      ## 诊断参数
      - 集群: prod-us-east
      - 主机名: worker-4
      ...
      ---
      用户问题: <message>
    """
    req_file = capture_dir / "output" / "1-request.json"
    if not req_file.exists():
        req_file = capture_dir / "1-request.json"
    with open(req_file) as f:
        data = json.load(f)

    user_msg = ""
    for m in data.get("messages", []):
        if m.get("role") != "user":
            continue
        content = m.get("content", "")
        if isinstance(content, list):
            content = " ".join(
                c.get("text", "") if isinstance(c, dict) else str(c)
                for c in content
            )
        user_msg = content
        break
    if not user_msg:
        sys.exit(f"未在 {req_file} 找到 user 消息")

    params: dict = {}
    message = user_msg
    if "## 诊断参数" in user_msg and "用户问题:" in user_msg:
        param_block, _, tail = user_msg.partition("---")
        message = tail.split("用户问题:", 1)[1].strip()
        for line in param_block.splitlines():
            line = line.strip().lstrip("- ")
            if ":" not in line:
                continue
            key, _, val = line.partition(":")
            params[key.strip()] = val.strip()

    # 故障时间 "2026-07-01 15:00:00 ~ 2026-07-01 15:10:00"
    start_t = end_t = ""
    if "故障时间" in params:
        parts = [p.strip() for p in params["故障时间"].split("~")]
        if len(parts) == 2:
            start_t, end_t = parts

    wl_type = wl_name = ""
    if "工作负载" in params:
        wl = params["工作负载"]
        if "/" in wl:
            wl_type, wl_name = wl.split("/", 1)
        else:
            wl_name = wl

    request = {
        "message": message,
        "entity_type": "kubernetes" if params.get("集群") else (
            "hosts" if params.get("主机名") and not params.get("命名空间") else ""),
        "entity_name": params.get("集群", params.get("主机名", "")),
        "param_overrides": {
            "cluster_name": params.get("集群", ""),
            "hostname": params.get("主机名", ""),
            "namespace": params.get("命名空间", ""),
            "workload_type": wl_type,
            "workload_name": wl_name,
            "pod_name": params.get("Pod", ""),
            "fault_time_range": {"start_time": start_t, "end_time": end_t},
        },
    }
    # 清理空值
    request["param_overrides"] = {
        k: v for k, v in request["param_overrides"].items()
        if v not in ("", {}, None)
    }

    text = json.dumps(request, ensure_ascii=False, indent=2)
    if out:
        out.write_text(text + "\n")
        print(f"请求已写入 {out}")
        print(f"  message: {message[:60]}...")
        print(f"  entity:  {request['entity_type']}/{request['entity_name']}")
    else:
        print(text)
    return request


# ── run ──────────────────────────────────────────────────────────────

def run_replay(req_file: Path, wait: int, sse_log: Path) -> str:
    """后台发起复跑，等待完成，返回 session 开始时间戳（用于日志过滤）。"""
    started_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    cmd = (
        f"nohup curl -sN -X POST {API} "
        f"-H 'Content-Type: application/json' "
        f"-d @{req_file} > {sse_log} 2>&1 &"
    )
    subprocess.run(["zsh", "-c", cmd], check=True)
    print(f"复跑已发起（SSE → {sse_log}）")
    if wait <= 0:
        print("未指定 --wait，请手动等待会话结束后执行 assess。")
        return started_at

    log_file = ROOT / "log" / f"diagnostics_{datetime.now():%Y-%m-%d}.log"
    deadline = time.time() + wait
    while time.time() < deadline:
        time.sleep(15)
        try:
            tail = subprocess.run(
                ["tail", "-c", "8000", str(log_file)],
                capture_output=True, text=True,
            ).stdout
        except Exception:
            continue
        if "Stream completed" in tail or "Result saved to" in tail:
            print("会话已完成。")
            return started_at
    print(f"等待超时（{wait}s），会话可能仍在运行。")
    return started_at


# ── assess ───────────────────────────────────────────────────────────

def _load_ledger(path: Path) -> dict:
    with open(path) as f:
        return json.load(f)


def _metrics(ledger: dict) -> dict:
    hyps = ledger.get("hypotheses", {})
    statuses: dict[str, int] = {}
    deleg_total = 0
    deleg_max_per_hyp = 0
    for h in hyps.values():
        st = h.get("status", "?")
        statuses[st] = statuses.get(st, 0) + 1
        deleg_total += h.get("_delegate_count", 0)
        deleg_max_per_hyp = max(deleg_max_per_hyp, h.get("_delegate_count", 0))

    # 兼容旧台账（无 _delegate_count 字段）：从 rounds 统计 task 委派
    if deleg_total == 0:
        for rd in ledger.get("rounds", []):
            deleg_total += len(rd.get("delegated_experts", []) or [])
            if "task" in rd.get("tools_called", []) and not rd.get("delegated_experts"):
                deleg_total += 1

    # root_causes 兼容：confirmed 假设即确认根因（旧台账安全阀强结时
    # root_causes 可能为空但 confirmed 假设存在）
    n_causes = len(ledger.get("root_causes", []))
    if n_causes == 0:
        n_causes = sum(1 for h in hyps.values() if h.get("status") == "confirmed")

    return {
        "rounds": ledger.get("current_round", 0),
        "batches": ledger.get("_root_commit_count", 0),
        "hypotheses": len(hyps),
        "statuses": statuses,
        "deferred": sum(1 for h in hyps.values() if h.get("deferred")),
        "deleg_total": deleg_total,
        "deleg_max_per_hyp": deleg_max_per_hyp,
        "root_causes": n_causes,
        # _forced_terminal 有两种语义：安全阀强结（Safety 日志）与
        # 正常 finalize（Phase guard 在退出条件满足后 finalize 未决
        # 假设）。仅凭标记无法区分，需结合日志 Safety 事件判定。
        "forced_terminal": bool(ledger.get("_forced_terminal")),
    }


def assess(baseline_path: Path, new_path: Path,
           log_file: Path | None, session_start: str) -> int:
    base = _metrics(_load_ledger(baseline_path))
    new = _metrics(_load_ledger(new_path))

    print("=" * 64)
    print("复跑验收报告")
    print("=" * 64)
    print(f"{'指标':<24}{'原会话':>12}{'复跑':>12}{'变化':>14}")
    print("-" * 64)

    def row(name: str, b, n, good_when_lower: bool = True):
        delta = ""
        if isinstance(b, (int, float)) and isinstance(n, (int, float)) and b:
            pct = (n - b) / b * 100
            arrow = "↓" if n < b else ("↑" if n > b else "=")
            mark = ""
            if good_when_lower:
                mark = " ✓" if n <= b else " ✗"
            delta = f"{arrow}{abs(pct):.0f}%{mark}"
        print(f"{name:<24}{b!s:>12}{n!s:>12}{delta:>14}")

    row("总轮次", base["rounds"], new["rounds"])
    row("根假设批次", base["batches"], new["batches"])
    row("委派总次数", base["deleg_total"], new["deleg_total"])
    row("单假设最大委派", base["deleg_max_per_hyp"], new["deleg_max_per_hyp"])
    row("假设数", base["hypotheses"], new["hypotheses"], False)
    row("已确认根因", base["root_causes"], new["root_causes"], False)

    print("-" * 64)
    print(f"原会话状态分布: {base['statuses']}  deferred={base['deferred']}")
    print(f"复跑  状态分布: {new['statuses']}  deferred={new['deferred']}")

    # ── 日志事件统计（门控/拦截/安全阀）──
    # "安全阀强结"以日志 Safety 事件为准——台账 _forced_terminal 标记
    # 在正常 finalize（Phase guard）时也会置位，不能单独作为判据。
    events = {"换批被拒": 0, "write_file 拦截": 0, "价值门控拦截": 0,
              "安全阀强制": 0}
    if log_file and log_file.exists() and session_start:
        hhmmss = session_start.split(" ")[-1][:5]  # HH:MM
        day_prefix = session_start.split(" ")[0]
        with open(log_file) as f:
            for line in f:
                if not line.startswith(day_prefix):
                    continue
                if line[11:16] < hhmmss:
                    continue
                if "仅当前批根假设" in line or "批次预算已用尽" in line:
                    events["换批被拒"] += 1
                elif "暂时无法生成诊断报告" in line:
                    events["write_file 拦截"] += 1
                elif "委派被信息增益门控拦截" in line:
                    events["价值门控拦截"] += 1
                elif "Safety:" in line and "forced REPORT" in line:
                    events["安全阀强制"] += 1
    print("-" * 64)
    print("过程事件（复跑会话）:")
    for k, v in events.items():
        mark = "✓" if v == 0 else f"✗×{v}"
        print(f"  {k:<16}{mark}")

    # ── 验收判定 ──
    checks = [
        ("轮次不超过原会话", new["rounds"] <= base["rounds"]),
        ("无换批被拒", events["换批被拒"] == 0),
        ("无 write_file 拦截", events["write_file 拦截"] == 0),
        ("单假设委派≤2", new["deleg_max_per_hyp"] <= 2),
        ("有确认根因", new["root_causes"] >= 1),
    ]
    print("-" * 64)
    failed = 0
    for name, ok in checks:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
        failed += 0 if ok else 1
    print("=" * 64)
    print(f"总计: {len(checks) - failed}/{len(checks)} 通过")
    return 1 if failed else 0


# ── main ─────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_ex = sub.add_parser("extract", help="从 capture 提取复跑请求")
    p_ex.add_argument("capture_dir", type=Path)
    p_ex.add_argument("-o", "--out", type=Path)

    p_run = sub.add_parser("run", help="发起复跑")
    p_run.add_argument("request", type=Path)
    p_run.add_argument("--wait", type=int, default=0,
                       help="等待会话完成的秒数（0=不等待）")
    p_run.add_argument("--sse-log", type=Path,
                       default=Path("/tmp/replay_sse.log"))

    p_as = sub.add_parser("assess", help="对比验收")
    p_as.add_argument("--baseline", type=Path, required=True)
    p_as.add_argument("--new", type=Path, required=True)
    p_as.add_argument("--log", type=Path,
                      default=ROOT / "log" / f"diagnostics_{datetime.now():%Y-%m-%d}.log")
    p_as.add_argument("--session-start", default="",
                      help="复跑开始时间 'YYYY-MM-DD HH:MM:SS'（日志过滤；默认不过滤）")

    p_full = sub.add_parser("full", help="extract + run + assess 一步完成")
    p_full.add_argument("capture_dir", type=Path)
    p_full.add_argument("--baseline-ledger", type=Path, required=True)
    p_full.add_argument("--wait", type=int, default=1200)
    p_full.add_argument("--new-ledger", type=Path, default=None,
                        help="复跑台账路径（默认取 traces 目录最新文件）")

    args = ap.parse_args()

    if args.cmd == "extract":
        extract_request(args.capture_dir, args.out)
    elif args.cmd == "run":
        run_replay(args.request, args.wait, args.sse_log)
    elif args.cmd == "assess":
        sys.exit(assess(args.baseline, args.new, args.log,
                        args.session_start))
    elif args.cmd == "full":
        req = extract_request(args.capture_dir,
                              Path("/tmp/replay_request.json"))
        started = run_replay(Path("/tmp/replay_request.json"),
                             args.wait, Path("/tmp/replay_sse.log"))
        new_ledger = args.new_ledger
        if new_ledger is None:
            traces = sorted((ROOT / "agent_data" / "traces").glob("*-ledger.json"),
                            key=lambda p: p.stat().st_mtime)
            if not traces:
                sys.exit("未找到复跑台账文件")
            new_ledger = traces[-1]
        print(f"复跑台账: {new_ledger}")
        sys.exit(assess(args.baseline_ledger, new_ledger,
                        ROOT / "log" / f"diagnostics_{datetime.now():%Y-%m-%d}.log",
                        started))


if __name__ == "__main__":
    main()
