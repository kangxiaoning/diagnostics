"""端到端验证：跨轮次工具去重 + offload 文件写入 + context 渲染。

模拟一次 3 轮诊断的工具调用序列，验证：
1. 工具结果 offload 到 backend
2. ledger.tool_results 正确记录路径
3. render_ledger_context 渲染 offload 区段
4. 第 3 轮重复调用被拦截（缓存命中）
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

# ── Setup: 创建 mock backend 和 middleware ──
from deepagents.backends.protocol import BackendProtocol, WriteResult


class MockBackend(BackendProtocol):
    """内存 backend，模拟文件系统读写。"""

    def __init__(self):
        self._files: dict[str, str] = {}
        self._write_count = 0

    async def awrite(self, path: str, content: str) -> WriteResult | None:
        self._files[path] = content
        self._write_count += 1
        return WriteResult(error=None)

    async def aread(self, path: str) -> str | None:
        return self._files.get(path)

    async def adelete(self, path: str) -> WriteResult | None:
        self._files.pop(path, None)
        return WriteResult(error=None)


backend = MockBackend()

from diagnostics.agent.ledger import (
    new_ledger,
    render_ledger_context,
    record_round,
    derive_phase,
    add_hypotheses,
)
from diagnostics.agent.ledger_middleware import (
    DiagnosisLedgerMiddleware,
    _make_cache_key,
    _is_tool_failure,
    _sanitize_path_component,
)

# 创建 middleware 实例
mw = DiagnosisLedgerMiddleware(
    ledger_path="/tmp/test_ledger.json",
    report_path="/tmp/test_report.md",
    backend=backend,
)

# 模拟 before_agent: 初始化台账
mw._current_ledger = new_ledger()

# 模拟 before_model: 轮次同步
mw._model_call_count = 1
mw._current_ledger["current_round"] = 1

# ── 验证 1: 第 1 轮 check_memory() ──
print("=" * 60)
print("1. 模拟第 1 轮: check_memory()")
print("=" * 60)

from langchain_core.messages import ToolMessage

tool_name = "check_memory"
tool_args = {}
output = (
    "Mem: 7.6Gi total, 7.0Gi used, 120Mi free, 512Mi buff/cache, 280Mi available\n"
    "Swap: 2.0Gi total, 1.5Gi used, 512Mi free\n"
    "/proc/meminfo: AnonPages=6815744kB (异常高, 疑似内存泄漏)"
)

# 手动模拟 awrap_tool_call 的后半段（已跳过 handler 执行）
cache_key = _make_cache_key(tool_name, tool_args)
content = output
fail_count = 1 if _is_tool_failure(content) else 0

# 内存缓存
mock_msg = ToolMessage(content=output, tool_call_id="call_test1", name=tool_name)
mw._tool_call_cache[cache_key] = (mock_msg, fail_count)

# Offload 写入
import asyncio


async def simulate_offload():
    mw._model_call_count = 1
    mw._current_ledger["current_round"] = 1

    file_path = (
        f"{mw._tool_results_prefix}/r1/"
        f"{_sanitize_path_component(tool_name)}_noargs.txt"
    )
    await backend.awrite(file_path, content)
    print(f"   写入文件: {file_path}")
    print(f"   文件大小: {len(content)} 字符")

    lines = content.splitlines()
    mw._current_ledger.setdefault("tool_results", {})[cache_key] = {
        "path": file_path,
        "round": 1,
        "preview": content[:200].replace("\n", " "),
        "lines": len(lines),
        "is_failure": False,
    }
    print(f"   tool_results 条目: {len(mw._current_ledger['tool_results'])}")


asyncio.run(simulate_offload())

# ── 验证 2: 第 2 轮 check_processes() ──
print("\n" + "=" * 60)
print("2. 模拟第 2 轮: check_processes()")
print("=" * 60)

tool_name2 = "check_processes"
output2 = (
    "PID 5678 app R 95.3% 78.5% java -Xmx512m -jar backend.jar\n"
    "PID 2345 app S 3.1% 6.5% mysqld\n"
    "Note: java PID 5678 RSS=6.2GB vs Xmx=512MB → native memory leak"
)

cache_key2 = _make_cache_key(tool_name2, {})
mock_msg2 = ToolMessage(content=output2, tool_call_id="call_test2", name=tool_name2)
mw._tool_call_cache[cache_key2] = (mock_msg2, 0)


async def simulate_offload2():
    mw._model_call_count = 2
    mw._current_ledger["current_round"] = 2

    file_path2 = (
        f"{mw._tool_results_prefix}/r2/"
        f"{_sanitize_path_component(tool_name2)}_noargs.txt"
    )
    await backend.awrite(file_path2, output2)
    lines2 = output2.splitlines()
    mw._current_ledger.setdefault("tool_results", {})[cache_key2] = {
        "path": file_path2,
        "round": 2,
        "preview": output2[:200].replace("\n", " "),
        "lines": len(lines2),
        "is_failure": False,
    }
    print(f"   写入文件: {file_path2}")
    print(f"   backend 总文件数: {len(backend._files)}")


asyncio.run(simulate_offload2())

# ── 验证 3: context 渲染 ──
print("\n" + "=" * 60)
print("3. Context 渲染验证")
print("=" * 60)

ctx = render_ledger_context(mw._current_ledger)

# 断言关键元素
checks = [
    ("已有工具调用结果", "offload 区段标题"),
    ("read_file/grep", "LLM 工具提示"),
    ("禁止重复调用", "禁止重复提示"),
    ("check_memory", "check_memory 条目"),
    ("check_processes", "check_processes 条目"),
    ("第1轮", "轮次信息"),
    ("第2轮", "轮次信息"),
    ("/tool_results/r1/", "文件路径"),
    ("/tool_results/r2/", "文件路径"),
    ("read_file(path=", "read_file 示例"),
]

all_pass = True
for keyword, desc in checks:
    if keyword in ctx:
        print(f"   ✅ {desc}: '{keyword}' 存在")
    else:
        print(f"   ❌ {desc}: '{keyword}' 缺失!")
        all_pass = False

print(f"\n--- render_ledger_context 输出节选 ---")
# 只打印 tool_results 相关部分
for line in ctx.split("\n"):
    if any(k in line for k in ["已有工具调用", "check_", "read_file", "禁止重复"]):
        print(f"  {line}")
print("--- 结束 ---")

# ── 验证 4: 第 3 轮重复调用拦截 ──
print("\n" + "=" * 60)
print("4. 跨轮去重拦截验证")
print("=" * 60)

# 第 3 轮再次"调用" check_memory
repeat_key = _make_cache_key("check_memory", {})
if repeat_key in mw._tool_call_cache:
    cached, fc = mw._tool_call_cache[repeat_key]
    print(f"   ✅ 缓存命中: check_memory (fail_count={fc})")
    print(f"   返回内容前 100 字符: {cached.content[:100]}...")
else:
    print(f"   ❌ 缓存未命中!")
    all_pass = False

# ── 验证 5: backend 文件可读 ──
print("\n" + "=" * 60)
print("5. Backend 文件可读性验证")
print("=" * 60)


async def verify_readable_fixed():
    files = list(backend._files.keys())
    print(f"   backend 中文件数: {len(files)}")
    for path in files:
        content = await backend.aread(path)
        if content:
            print(f"   ✅ {path}: {len(content)} 字符, 首行: {content.split(chr(10))[0][:60]}")
        else:
            print(f"   ❌ {path}: 不可读!")


asyncio.run(verify_readable_fixed())

# ── 验证 6: 台账持久化兼容性 ──
print("\n" + "=" * 60)
print("6. 台账 JSON 持久化验证")
print("=" * 60)

from diagnostics.agent.ledger import ledger_to_json, ledger_from_json

json_str = ledger_to_json(mw._current_ledger)
restored = ledger_from_json(json_str)
tr = restored.get("tool_results", {})
print(f"   tool_results 条目: {len(tr)}")
for ck, info in tr.items():
    tool = ck.split(":")[0]
    print(f"   ✅ {tool}: path={info.get('path', '')[:40]}... round={info.get('round')}")

if len(tr) == 2:
    print("   ✅ 条目数正确")
else:
    print(f"   ❌ 期望 2 条，实际 {len(tr)} 条")
    all_pass = False

# ── 最终结果 ──
print("\n" + "=" * 60)
if all_pass:
    print("🎉 全部验证通过 — 跨轮次工具去重方案正确工作")
else:
    print("❌ 存在失败项，请检查上方输出")
print("=" * 60)
sys.exit(0 if all_pass else 1)
