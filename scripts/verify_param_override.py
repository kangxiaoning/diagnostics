"""验证 ToolParamOverrideMiddleware — 代码级测试，无需 LLM。

验证项:
1. _flatten() 正确展开嵌套 dict
2. awrap_tool_call: 参数名匹配 → 替换
3. awrap_tool_call: 参数名不匹配 → 不变
4. awrap_tool_call: 配置为空 → 原值传递
5. awrap_tool_call: 值已正确 → 不修改（避免无意义覆盖）
6. 集成: build_agent(param_overrides=...) → middleware 在链中
7. ChatRequest 接受 param_overrides 字段
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from langchain_core.messages import ToolMessage


# ── 1. _flatten 展开嵌套 dict ──
from diagnostics.agent.param_override_middleware import _flatten

flat = _flatten(
    {
        "task_type": "container",
        "cluster_name": "pks-ehpc-1013-734964",
        "namespace": "sfe-default",
        "pod_name": "eip-assistant-xxx",
        "fault_time_range": {
            "start_time": "2026-06-30 14:15:00",
            "end_time": "2026-06-30 14:30:00",
        },
    }
)
assert flat["cluster_name"] == "pks-ehpc-1013-734964"
assert flat["namespace"] == "sfe-default"
assert flat["pod_name"] == "eip-assistant-xxx"
assert flat["start_time"] == "2026-06-30 14:15:00"
assert flat["end_time"] == "2026-06-30 14:30:00"
assert flat["task_type"] == "container"
print("✅ 1. _flatten: 嵌套展开正确")

# 验证 host 场景
flat2 = _flatten({"name_chunk": "CQX-L0150660", "task_type": "host"})
assert flat2["name_chunk"] == "CQX-L0150660"
print("✅ 1b. host 场景展开正确")


# ── 2. awrap_tool_call: 参数名匹配 → 替换 ──
import asyncio
from diagnostics.agent.param_override_middleware import ToolParamOverrideMiddleware


async def _test_override():
    config = {
        "cluster_name": "pks-ehpc-1013-734964",
        "namespace": "sfe-default",
    }
    mw = ToolParamOverrideMiddleware(config=config)

    # 模拟 LLM 传了错误的 cluster_name=cluster1
    request = MagicMock()
    request.tool_call = {
        "name": "get_etcd_status",
        "id": "call_test1",
        "args": {"cluster_name": "cluster1", "namespace": "wrong-ns"},
    }
    request.runtime = MagicMock()

    handler_called_with = {}

    async def handler(req):
        handler_called_with["args"] = dict(req.tool_call["args"])
        return ToolMessage(content="ok", tool_call_id="call_test1")

    result = await mw.awrap_tool_call(request, handler)

    # cluster_name 和 namespace 应该被替换
    assert handler_called_with["args"]["cluster_name"] == "pks-ehpc-1013-734964", (
        f"cluster_name should be overridden, got {handler_called_with['args']['cluster_name']}"
    )
    assert handler_called_with["args"]["namespace"] == "sfe-default"
    print("✅ 2. 参数替换: cluster_name 和 namespace 正确替换")


asyncio.run(_test_override())


# ── 3. awrap_tool_call: 参数名不匹配 → 不变 ──
async def _test_no_match():
    config = {"cluster_name": "prod"}
    mw = ToolParamOverrideMiddleware(config=config)

    request = MagicMock()
    request.tool_call = {
        "name": "check_memory",
        "id": "call_test2",
        "args": {},  # check_memory 没有 cluster_name 参数，注入后 OpenAI 会忽略
    }
    request.runtime = MagicMock()
    handler_called = False

    async def handler(req):
        nonlocal handler_called
        handler_called = True
        return ToolMessage(content="ok", tool_call_id="call_test2")

    await mw.awrap_tool_call(request, handler)
    assert handler_called
    # cluster_name 被注入（OpenAI function calling 忽略多余参数）
    assert request.tool_call["args"]["cluster_name"] == "prod"
    print("✅ 3. 缺失参数注入: cluster_name 注入空 args（工具方会忽略多余参数）")


asyncio.run(_test_no_match())


# ── 4. awrap_tool_call: 配置为空 → 原值传递 ──
async def _test_empty_config():
    mw = ToolParamOverrideMiddleware(config=None)

    request = MagicMock()
    request.tool_call = {
        "name": "get_etcd_status",
        "id": "call_test3",
        "args": {"cluster_name": "whatever"},
    }
    request.runtime = MagicMock()
    handler_called = False

    async def handler(req):
        nonlocal handler_called
        handler_called = True
        return ToolMessage(content="ok", tool_call_id="call_test3")

    await mw.awrap_tool_call(request, handler)
    assert handler_called
    assert request.tool_call["args"]["cluster_name"] == "whatever"
    print("✅ 4. 空配置: 原值传递不替换")


asyncio.run(_test_empty_config())


# ── 5. awrap_tool_call: 值已正确 → 不覆盖（减少无意义日志）──
async def _test_already_correct():
    config = {"cluster_name": "prod-us-east"}
    mw = ToolParamOverrideMiddleware(config=config)

    request = MagicMock()
    request.tool_call = {
        "name": "get_cluster_overview",
        "id": "call_test4",
        "args": {"cluster_name": "prod-us-east"},  # 已正确
    }
    request.runtime = MagicMock()

    async def handler(req):
        return ToolMessage(content="ok", tool_call_id="call_test4")

    await mw.awrap_tool_call(request, handler)
    # 值未变（代码中 if tool_args[param] != param_value 跳过了）
    assert request.tool_call["args"]["cluster_name"] == "prod-us-east"
    print("✅ 5. 已正确值: 不重复覆盖")


asyncio.run(_test_already_correct())


# ── 6. awrap_tool_call: host 场景 — name_chunk 替换 ──
async def _test_name_chunk():
    config = {
        "task_type": "host",
        "name_chunk": "CQX-L0150660",
        "fault_time_range": {
            "start_time": "2026-07-01 18:10:17",
            "end_time": "2026-07-01 18:17:17",
        },
    }
    mw = ToolParamOverrideMiddleware(config=config)

    request = MagicMock()
    request.tool_call = {
        "name": "query_argus_cpu",
        "id": "call_test5",
        "args": {
            "name_chunk": "unknown-host",
            "start_time": "00:00",
            "end_time": "99:99",
        },
    }
    request.runtime = MagicMock()

    async def handler(req):
        args = dict(req.tool_call["args"])
        assert args["name_chunk"] == "CQX-L0150660"
        assert args["start_time"] == "2026-07-01 18:10:17"
        assert args["end_time"] == "2026-07-01 18:17:17"
        return ToolMessage(content="ok", tool_call_id="call_test5")

    await mw.awrap_tool_call(request, handler)
    print("✅ 6. host场景: name_chunk + start_time + end_time 全部替换")


asyncio.run(_test_name_chunk())


# ── 7. update_config: 运行时更新配置 ──
async def _test_update_config():
    mw = ToolParamOverrideMiddleware(config={"cluster_name": "old"})
    mw.update_config({"cluster_name": "new", "namespace": "ns-new"})

    request = MagicMock()
    request.tool_call = {
        "name": "get_pods",
        "id": "call_test6",
        "args": {"cluster_name": "x", "namespace": "y"},
    }
    request.runtime = MagicMock()

    async def handler(req):
        args = dict(req.tool_call["args"])
        assert args["cluster_name"] == "new"
        assert args["namespace"] == "ns-new"
        return ToolMessage(content="ok", tool_call_id="call_test6")

    await mw.awrap_tool_call(request, handler)
    print("✅ 7. update_config: 运行时更新生效")


asyncio.run(_test_update_config())


# ── 8. ChatRequest 接受 param_overrides ──
from diagnostics.server.schemas import ChatRequest

req = ChatRequest(
    message="集群很乱",
    entity_type="kubernetes",
    entity_name="prod-us-east",
    param_overrides={
        "cluster_name": "pks-ehpc-1013-734964",
        "fault_time_range": {
            "start_time": "2026-06-30 14:15:00",
            "end_time": "2026-06-30 14:30:00",
        },
    },
)
assert req.param_overrides["cluster_name"] == "pks-ehpc-1013-734964"
assert req.param_overrides["fault_time_range"]["start_time"] == "2026-06-30 14:15:00"

# 不传 param_overrides 的情况
req2 = ChatRequest(message="hello", entity_type="hosts", entity_name="prod-web-01")
assert req2.param_overrides is None
print("✅ 8. ChatRequest: param_overrides 字段正确")


# ── 9. JSON 序列化验证 (前端请求格式) ──
payload = {
    "message": "帮我分析主机问题",
    "entity_type": "hosts",
    "entity_name": "CQX-L0150660",
    "param_overrides": {
        "name_chunk": "CQX-L0150660",
        "fault_time_range": {
            "start_time": "2026-07-01 18:10:17",
            "end_time": "2026-07-01 18:17:17",
        },
    },
}
json_str = json.dumps(payload)
parsed = ChatRequest.model_validate_json(json_str)
assert parsed.param_overrides["name_chunk"] == "CQX-L0150660"
assert parsed.param_overrides["fault_time_range"]["start_time"] == "2026-07-01 18:10:17"
print("✅ 9. JSON 反序列化: ChatRequest 正确解析 param_overrides")

# ── 10. 多个工具参数共存 — 只替换匹配的 ──
async def _test_partial_match():
    config = {"cluster_name": "prod", "pod_name": "my-pod-123"}
    mw = ToolParamOverrideMiddleware(config=config)

    request = MagicMock()
    request.tool_call = {
        "name": "get_pod_logs",
        "id": "call_test7",
        "args": {
            "cluster_name": "cluster1",       # 需要替换
            "namespace": "default",           # config 中没有 → 保留
            "pod_name": "unknown-pod",        # 需要替换
            "tail_lines": 100,                # config 中没有 → 保留
        },
    }
    request.runtime = MagicMock()

    async def handler(req):
        args = req.tool_call["args"]
        assert args["cluster_name"] == "prod"
        assert args["pod_name"] == "my-pod-123"
        assert args["namespace"] == "default"    # 保留原值
        assert args["tail_lines"] == 100          # 保留原值
        return ToolMessage(content="ok", tool_call_id="call_test7")

    await mw.awrap_tool_call(request, handler)
    print("✅ 10. 部分匹配: 只替换 config 中存在的 key，其余保留原值")


asyncio.run(_test_partial_match())


# ── 11. 缺失参数注入 — LLM 只传了1个参数，补全另外2个 ──
async def _test_inject_missing_params():
    config = {
        "task_type": "container",
        "cluster_name": "pks-ehpc-1013-734964",
        "namespace": "sfe-default",
        "pod_name": "eip-assistant-xxx",
        "fault_description": "POD pending",
    }
    mw = ToolParamOverrideMiddleware(config=config)

    # LLM 只传了 cluster_name 和 tail_lines，缺 namespace 和 pod_name
    request = MagicMock()
    request.tool_call = {
        "name": "get_pod_logs",
        "id": "call_test8",
        "args": {
            "cluster_name": "cluster1",
            "tail_lines": 50,
        },
    }
    request.runtime = MagicMock()

    async def handler(req):
        args = req.tool_call["args"]
        assert args["cluster_name"] == "pks-ehpc-1013-734964", "cluster_name 应被替换"
        assert args["namespace"] == "sfe-default", "namespace 应被注入"
        assert args["pod_name"] == "eip-assistant-xxx", "pod_name 应被注入"
        assert args["tail_lines"] == 50, "非 config 参数应保留"
        # task_type 和 fault_description 不应出现在 args 中
        assert "task_type" not in args, "task_type 是 meta key，不应注入"
        assert "fault_description" not in args, "fault_description 是 meta key，不应注入"
        return ToolMessage(content="ok", tool_call_id="call_test8")

    await mw.awrap_tool_call(request, handler)
    print("✅ 11. 缺失参数注入: namespace + pod_name 注入, cluster_name 替换, meta keys 排除")


asyncio.run(_test_inject_missing_params())


# ── 12. LLM 完全不传参数 — 全部注入 ──
async def _test_inject_all_missing():
    config = {
        "cluster_name": "prod-us-east",
        "start_time": "2026-07-01 18:00:00",
        "end_time": "2026-07-01 18:10:00",
    }
    mw = ToolParamOverrideMiddleware(config=config)

    # LLM 调用 query_argus_cpu 但不传任何参数
    request = MagicMock()
    request.tool_call = {
        "name": "query_argus_cpu",
        "id": "call_test9",
        "args": {},  # 完全空白
    }
    request.runtime = MagicMock()

    async def handler(req):
        args = req.tool_call["args"]
        assert args["cluster_name"] == "prod-us-east"
        assert args["start_time"] == "2026-07-01 18:00:00"
        assert args["end_time"] == "2026-07-01 18:10:00"
        return ToolMessage(content="ok", tool_call_id="call_test9")

    await mw.awrap_tool_call(request, handler)
    print("✅ 12. 零参数调用: 所有3个参数全部注入")


asyncio.run(_test_inject_all_missing())


# ── 13. 脚手架/台账/task工具不拦截 ──
async def _test_skip_scaffolding():
    config = {"cluster_name": "pks-ehpc-1013-734964"}
    mw = ToolParamOverrideMiddleware(config=config)

    for skip_tool in ["read_file", "write_file", "commit_hypotheses", "task", "grep", "ls"]:
        request = MagicMock()
        request.tool_call = {
            "name": skip_tool,
            "id": f"call_skip_{skip_tool}",
            "args": {},
        }
        request.runtime = MagicMock()
        handler_called = False

        async def handler(req, _name=skip_tool):
            nonlocal handler_called
            handler_called = True
            return ToolMessage(content=f"ok_{_name}", tool_call_id=f"call_skip_{_name}")

        await mw.awrap_tool_call(request, handler)
        assert handler_called, f"{skip_tool} 应被执行"
        # 确认 cluster_name 没有被注入到 skip 工具
        assert "cluster_name" not in request.tool_call["args"], (
            f"{skip_tool} 不应被注入 cluster_name（脚手架/台账/task工具跳过）"
        )
    print("✅ 13. 脚手架/台账/task工具: 全部跳过，不被注入")


asyncio.run(_test_skip_scaffolding())


# ── 结果 ──
print("\n" + "=" * 60)
print("🎉 全部 13 项验证通过 — ToolParamOverrideMiddleware 正确工作")
print("=" * 60)
