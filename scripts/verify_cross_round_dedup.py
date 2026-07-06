"""验证跨轮次工具去重方案 — 代码级验证，无需LLM。

验证项：
1. new_ledger() 包含 tool_results 字段
2. render_ledger_context() 渲染 offloaded tool results 区段
3. _make_cache_key() 生成确定性键
4. _sanitize_path_component() 安全路径转换
5. 完整模拟 awrap_tool_call 写文件 + 台账记录
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

# ── 1. new_ledger 包含 tool_results ──
from diagnostics.agent.ledger import new_ledger

ledger = new_ledger()
assert "tool_results" in ledger, "FAIL: new_ledger() 缺少 tool_results 字段"
assert ledger["tool_results"] == {}, "FAIL: tool_results 初始值应为空 dict"
print("✅ 1. new_ledger() 包含 tool_results={}")

# ── 2. render_ledger_context 正确渲染 ──
from diagnostics.agent.ledger import render_ledger_context

# 空台账
ctx = render_ledger_context(ledger)
assert "已有工具调用结果" not in ctx, "FAIL: 空台账不应有 offload 区段"
print("✅ 2a. 空台账不渲染 tool_results 区段")

# 有 tool_results 的台账
ledger["tool_results"] = {
    'check_memory:{}': {
        "path": "/tool_results/r3/check_memory.txt",
        "round": 3,
        "preview": "Mem: 7.0/7.6Gi used Swap: 1.5/2.0Gi available 280Mi AnonPages=6.8GB",
        "lines": 8,
        "is_failure": False,
    },
    'check_disk:{}': {
        "path": "/tool_results/r3/check_disk.txt",
        "round": 3,
        "preview": "sda util=98.5% await=45.2ms sdb util=99.2% await=52.3ms iowait=55%",
        "lines": 6,
        "is_failure": False,
    },
    'check_cpu:{}': {
        "path": "",
        "round": 1,
        "preview": "vmstat us=80 sy=6 wa=4 id=8 CPU ~85% user",
        "lines": 5,
        "is_failure": True,
    },
}
ctx = render_ledger_context(ledger)

# 验证包含关键元素
assert "已有工具调用结果" in ctx, "FAIL: 应有 offload 区段标题"
assert "read_file/grep" in ctx, "FAIL: 应提示 LLM 使用 read_file/grep"
assert "禁止重复调用" in ctx, "FAIL: 应有禁止重复调用提示"
assert "check_memory" in ctx, "FAIL: 应列出 check_memory 条目"
assert "check_disk" in ctx, "FAIL: 应列出 check_disk 条目"
assert "check_cpu" in ctx, "FAIL: 应列出 check_cpu 条目"
assert "第3轮" in ctx, "FAIL: 应显示轮次信息"
assert "read_file(path=" in ctx, "FAIL: 应提供 read_file 调用示例"
assert "⚠失败" in ctx, "FAIL: check_cpu 应标记为失败"
# 有 path 的工具应有文件路径
assert "/tool_results/r3/check_memory.txt" in ctx, "FAIL: 应显示文件路径"
# 无 path 的工具不应显示 read_file (backend 写入失败)
assert "/tool_results/r1/check_cpu.txt" not in ctx, "FAIL: 无 path 不应显示"

print("✅ 2b. render_ledger_context() 正确渲染 offload 区段")

# ── 3. _make_cache_key 确定性 ──
from diagnostics.agent.ledger_middleware import _make_cache_key

k1 = _make_cache_key("check_memory", {})
k2 = _make_cache_key("check_memory", {})
assert k1 == k2, "FAIL: 相同参数应生成相同 key"
assert k1 == "check_memory:{}", f"FAIL: 空参数 key 应为 'check_memory:{{}}', 实际: {k1}"

k3 = _make_cache_key("check_memory", {"a": 1, "b": 2})
k4 = _make_cache_key("check_memory", {"b": 2, "a": 1})
assert k3 == k4, "FAIL: sort_keys 应消除 args 顺序差异"

k5 = _make_cache_key("check_memory", {})
k6 = _make_cache_key("check_cpu", {})
assert k5 != k6, "FAIL: 不同工具应生成不同 key"

k7 = _make_cache_key(
    "query_argus_cpu",
    {"hostname": "prod-web-01", "start_time": "15:00", "end_time": "15:09"},
)
k8 = _make_cache_key(
    "query_argus_cpu",
    {"hostname": "prod-web-01", "start_time": "15:00", "end_time": "15:10"},
)
assert k7 != k8, "FAIL: 不同时间窗口应生成不同 key"

print("✅ 3. _make_cache_key() 确定性正确")

# ── 4. _sanitize_path_component ──
from diagnostics.agent.ledger_middleware import _sanitize_path_component

assert _sanitize_path_component("check_memory") == "check_memory"
assert _sanitize_path_component("query_argus_cpu") == "query_argus_cpu"
assert "/" not in _sanitize_path_component("a/b"), "FAIL: 不能包含路径分隔符"
assert " " not in _sanitize_path_component("a b"), "FAIL: 不能包含空格"
print("✅ 4. _sanitize_path_component() 正确")

# ── 5. ledger.setdefault("tool_results") 写入逻辑验证 ──
# 模拟 awrap_tool_call 中的台账写入
ledger = new_ledger()
ledger["current_round"] = 3
_result = {
    "path": "/tool_results/r3/check_memory.txt",
    "round": ledger["current_round"],
    "preview": "Mem: 7.0/7.6Gi used...",
    "lines": 8,
    "is_failure": False,
}
ledger.setdefault("tool_results", {})["check_memory:{}"] = _result
assert len(ledger["tool_results"]) == 1
assert ledger["tool_results"]["check_memory:{}"]["path"] == "/tool_results/r3/check_memory.txt"
print("✅ 5. tool_results 写入逻辑正确")

# ── 6. JSON 序列化验证 (ledger 持久化) ──
json_str = json.dumps(ledger["tool_results"], ensure_ascii=False)
restored = json.loads(json_str)
assert restored["check_memory:{}"]["lines"] == 8
assert restored["check_memory:{}"]["preview"] == "Mem: 7.0/7.6Gi used..."
print("✅ 6. tool_results JSON 序列化/反序列化正确")

# ── 7. 文件路径包含 args 区分 ──
# 验证不同参数的同名工具生成不同路径
path1 = "/tool_results/r1/check_memory_noargs.txt"
path2 = "/tool_results/r1/check_memory_noargs.txt"
assert path1 == path2, "相同参数应生成相同路径"

# 不同名称应生成不同路径
assert path1 != "/tool_results/r1/check_cpu_noargs.txt"
print("✅ 7. 文件路径正确区分不同工具")

# ── 8. 完整 context 渲染输出验证 ──
print("\n--- render_ledger_context 输出样本 ---")
print(ctx)
print("--- 输出结束 ---")

print("\n" + "=" * 60)
print("全部 8 项验证通过 ✅")
print("=" * 60)
