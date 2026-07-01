"""验证 ToolParamOverrideMiddleware — 基于工具签名注入 + 覆盖。"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from langchain_core.messages import ToolMessage
from langchain_core.tools import tool as langchain_tool


# Mock tools
@langchain_tool
def get_pod_logs(cluster_name: str, namespace: str, pod_name: str, tail_lines: int = 200) -> str:
    """Get pod logs."""
    return "mock"

@langchain_tool
def get_etcd_status(cluster_name: str) -> str:
    """Get etcd status."""
    return "mock"

@langchain_tool
def check_memory() -> str:
    """Check memory."""
    return "mock"

@langchain_tool
def check_disk() -> str:
    """Check disk."""
    return "mock"

@langchain_tool
def query_argus_cpu(name_chunk: str = "", start_time: str = "", end_time: str = "") -> str:
    """Query Argus CPU."""
    return "mock"

MOCK = [get_pod_logs, get_etcd_status, check_memory, check_disk, query_argus_cpu]

from diagnostics.agent.param_override_middleware import _flatten, ToolParamOverrideMiddleware

# 1. _flatten
f = _flatten({"a":1, "b":{"c":2, "d":3}})
assert f["a"]==1 and f["c"]==2 and f["d"]==3
print("✅ 1. _flatten")

# 2. override
async def t2():
    mw = ToolParamOverrideMiddleware(tools=MOCK, config={"cluster_name":"pks-ehpc","namespace":"sfe"})
    req = MagicMock(); req.tool_call = {"name":"get_etcd_status","id":"c2","args":{"cluster_name":"c1","namespace":"ns1"}}
    req.runtime = MagicMock()
    async def h(r): return ToolMessage(content="ok",tool_call_id="c2")
    await mw.awrap_tool_call(req, h)
    a = req.tool_call["args"]
    assert a["cluster_name"]=="pks-ehpc" and a["namespace"]=="sfe"
    print("✅ 2. override")
asyncio.run(t2())

# 3. no match
async def t3():
    mw = ToolParamOverrideMiddleware(tools=MOCK, config={"cluster_name":"prod"})
    req = MagicMock(); req.tool_call = {"name":"check_memory","id":"c3","args":{}}
    req.runtime = MagicMock()
    async def h(r): return ToolMessage(content="ok",tool_call_id="c3")
    await mw.awrap_tool_call(req, h)
    assert req.tool_call["args"] == {}
    print("✅ 3. no match — no inject")
asyncio.run(t3())

# 4. empty config
async def t4():
    mw = ToolParamOverrideMiddleware(tools=MOCK, config=None)
    req = MagicMock(); req.tool_call = {"name":"get_etcd_status","id":"c4","args":{"cluster_name":"x"}}
    req.runtime = MagicMock()
    async def h(r): return ToolMessage(content="ok",tool_call_id="c4")
    await mw.awrap_tool_call(req, h)
    assert req.tool_call["args"]["cluster_name"]=="x"
    print("✅ 4. empty config pass-through")
asyncio.run(t4())

# 5. already correct
async def t5():
    mw = ToolParamOverrideMiddleware(tools=MOCK, config={"cluster_name":"pks"})
    req = MagicMock(); req.tool_call = {"name":"get_etcd_status","id":"c5","args":{"cluster_name":"pks"}}
    req.runtime = MagicMock()
    async def h(r): return ToolMessage(content="ok",tool_call_id="c5")
    await mw.awrap_tool_call(req, h)
    assert req.tool_call["args"]["cluster_name"]=="pks"
    print("✅ 5. already correct — no-op")
asyncio.run(t5())

# 6. host scenario
async def t6():
    mw = ToolParamOverrideMiddleware(tools=MOCK, config={
        "task_type":"host","name_chunk":"CQX-L0150660",
        "fault_time_range":{"start_time":"2026-07-01 18:10:17","end_time":"2026-07-01 18:17:17"},
    })
    req = MagicMock(); req.tool_call = {"name":"query_argus_cpu","id":"c6","args":{"name_chunk":"x","start_time":"a","end_time":"b"}}
    req.runtime = MagicMock()
    async def h(r):
        a = r.tool_call["args"]
        assert a["name_chunk"]=="CQX-L0150660" and a["start_time"]=="2026-07-01 18:10:17" and a["end_time"]=="2026-07-01 18:17:17"
        return ToolMessage(content="ok",tool_call_id="c6")
    await mw.awrap_tool_call(req, h)
    print("✅ 6. host scenario")
asyncio.run(t6())

# 7. update_config
async def t7():
    mw = ToolParamOverrideMiddleware(tools=MOCK, config={"cluster_name":"old"})
    mw.update_config({"cluster_name":"new"})
    req = MagicMock(); req.tool_call = {"name":"get_etcd_status","id":"c7","args":{"cluster_name":"x"}}
    req.runtime = MagicMock()
    async def h(r):
        assert r.tool_call["args"]["cluster_name"]=="new"
        return ToolMessage(content="ok",tool_call_id="c7")
    await mw.awrap_tool_call(req, h)
    print("✅ 7. update_config")
asyncio.run(t7())

# 8. ChatRequest
from diagnostics.server.schemas import ChatRequest
r = ChatRequest(message="x",entity_type="kubernetes",entity_name="p",param_overrides={"cluster_name":"c"})
assert r.param_overrides["cluster_name"]=="c"
assert ChatRequest(message="x",entity_type="hosts",entity_name="h").param_overrides is None
print("✅ 8. ChatRequest")

# 9. JSON round-trip
p = {"message":"x","entity_type":"kubernetes","entity_name":"p","param_overrides":{"cluster_name":"c","fault_time_range":{"start_time":"2026-06-30 14:15:00"}}}
pr = ChatRequest.model_validate_json(json.dumps(p))
assert pr.param_overrides["cluster_name"]=="c"
print("✅ 9. JSON round-trip")

# 10. partial match
async def t10():
    mw = ToolParamOverrideMiddleware(tools=MOCK, config={"cluster_name":"prod","pod_name":"my-pod"})
    req = MagicMock(); req.tool_call = {"name":"get_pod_logs","id":"c10","args":{"cluster_name":"c1","namespace":"ns1","pod_name":"p1","tail_lines":100}}
    req.runtime = MagicMock()
    async def h(r):
        a = r.tool_call["args"]
        assert a["cluster_name"]=="prod" and a["pod_name"]=="my-pod" and a["namespace"]=="ns1" and a["tail_lines"]==100 and len(a)==4
        return ToolMessage(content="ok",tool_call_id="c10")
    await mw.awrap_tool_call(req, h)
    print("✅ 10. partial match")
asyncio.run(t10())

# 11. scenario A: LLM传3参全错 → 仅替换3参
async def t11():
    mw = ToolParamOverrideMiddleware(tools=MOCK, config={
        "task_type":"container","cluster_name":"pks-ehpc","namespace":"sfe-default","pod_name":"eip-nginx",
        "fault_time_range":{"start_time":"2026-06-30 14:15:00","end_time":"2026-06-30 14:30:00"},
    })
    req = MagicMock(); req.tool_call = {"name":"get_pod_logs","id":"c11","args":{"cluster_name":"c1","namespace":"d","pod_name":"abc"}}
    req.runtime = MagicMock()
    async def h(r):
        a = r.tool_call["args"]
        assert a=={"cluster_name":"pks-ehpc","namespace":"sfe-default","pod_name":"eip-nginx"}
        return ToolMessage(content="ok",tool_call_id="c11")
    await mw.awrap_tool_call(req, h)
    print("✅ 11. scenario A: 3参全替换,start/end不注入")
asyncio.run(t11())

# 12. scenario B: LLM缺cluster_name → 替换+注入
async def t12():
    mw = ToolParamOverrideMiddleware(tools=MOCK, config={
        "task_type":"container","cluster_name":"pks-ehpc","namespace":"sfe-default","pod_name":"eip-nginx",
        "fault_time_range":{"start_time":"2026-06-30 14:15:00","end_time":"2026-06-30 14:30:00"},
    })
    req = MagicMock(); req.tool_call = {"name":"get_pod_logs","id":"c12","args":{"namespace":"d","pod_name":"abc"}}
    req.runtime = MagicMock()
    async def h(r):
        a = r.tool_call["args"]
        assert a=={"cluster_name":"pks-ehpc","namespace":"sfe-default","pod_name":"eip-nginx"}
        return ToolMessage(content="ok",tool_call_id="c12")
    await mw.awrap_tool_call(req, h)
    print("✅ 12. scenario B: 替换2参+注入cluster_name")
asyncio.run(t12())

# 13. skip scaffolding/ledger/task
async def t13():
    mw = ToolParamOverrideMiddleware(tools=MOCK, config={"cluster_name":"pks"})
    for tn in ["read_file","write_file","commit_hypotheses","task","grep","ls"]:
        req = MagicMock(); req.tool_call = {"name":tn,"id":f"c13_{tn}","args":{}}
        req.runtime = MagicMock()
        ok = False
        async def h(r): nonlocal ok; ok=True; return ToolMessage(content="ok",tool_call_id=f"c13_{tn}")
        await mw.awrap_tool_call(req, h)
        assert ok and "cluster_name" not in req.tool_call["args"]
    print("✅ 13. skip scaffolding/ledger/task")
asyncio.run(t13())

print(f"\n{'='*60}\n🎉 全部 13 项验证通过\n{'='*60}")
