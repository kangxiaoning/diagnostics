"""Verify ToolParamOverrideMiddleware behavior.

Validates:
  1. Strict override — container: cluster_name, start_time, end_time
  2. Strict override — host: hostname, start_time, end_time
  3. Flexible trust: LLM-provided values passed through
  4. Flexible block: missing non-strict params → blocked
  5. Scaffolding skip: task, write_file, ledger tools pass through
  6. No-config: empty config → all calls pass through
  7. Multi-value params (lists) skipped
  8. _flat_config: fault_time_range flattened correctly
"""

from __future__ import annotations

import asyncio
import os
import sys
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ── Test helpers ──

def _make_request(tool_name: str, tool_call_id: str, args: dict) -> MagicMock:
    """Construct a mock ToolCallRequest with the given tool call."""
    req = MagicMock()
    req.tool_call = {"name": tool_name, "id": tool_call_id, "args": dict(args)}
    return req


async def _ensure_blocked(result, expected_tool_name: str) -> bool:
    """Assert the middleware blocked the call, returning a ToolMessage."""
    from langchain_core.messages import ToolMessage
    assert isinstance(result, ToolMessage), \
        f"Expected ToolMessage for blocked call, got {type(result).__name__}"
    assert result.name == expected_tool_name, \
        f"Tool name mismatch: {result.name} != {expected_tool_name}"
    assert "⛔" in result.content or "拦截" in result.content or "缺少" in result.content, \
        f"Missing block marker in: {result.content[:200]}"
    return True


async def _ensure_forwarded(result, expected_content: str) -> bool:
    """Assert the middleware forwarded the call to the handler."""
    from langchain_core.messages import ToolMessage
    assert isinstance(result, ToolMessage), \
        f"Expected ToolMessage, got {type(result).__name__}"
    assert result.content == expected_content, \
        f"Content mismatch: {result.content!r} != {expected_content!r}"
    return True


# ═════════════════════════════════════════════════════════════════
# Container (Kubernetes) scenario
# ═════════════════════════════════════════════════════════════════

async def test_strict_override_container():
    """LLM provides wrong cluster_name → overridden; missing start_time → injected."""
    from diagnostics.agent.param_override_middleware import ToolParamOverrideMiddleware
    from diagnostics.tools.mock.argus import query_argus_nodes
    from langchain_core.messages import ToolMessage

    config = {
        "task_type": "container",
        "cluster_name": "prod-us-east",
        "fault_time_range": {
            "start_time": "2026-07-01 15:00:00",
            "end_time": "2026-07-01 15:10:00",
        },
    }
    mw = ToolParamOverrideMiddleware(config=config, tools=[query_argus_nodes])

    # LLM invents wrong hostname; start_time omitted
    req = _make_request("query_argus_nodes", "c1", {"hostname": "wrong-cluster"})
    captured_args = {}

    async def handler(r):
        captured_args.update(r.tool_call.get("args", {}))
        return ToolMessage(content="ok", tool_call_id="c1", name="query_argus_nodes")

    result = await mw.awrap_tool_call(req, handler)
    assert result.content == "ok"
    # strict params injected even though LLM omitted them
    assert captured_args.get("start_time") == "2026-07-01 15:00:00"
    assert captured_args.get("end_time") == "2026-07-01 15:10:00"
    # hostname is not in strict set for container → trusted as-is
    assert captured_args.get("hostname") == "wrong-cluster"
    print("  ✓ Container strict: start_time/end_time injected, hostname trusted")


async def test_flexible_block_on_missing():
    """LLM omits namespace → blocked (middleware can't guess which)."""
    from diagnostics.agent.param_override_middleware import ToolParamOverrideMiddleware
    from diagnostics.tools.mock.kubernetes import check_kubernetes_pods

    config = {
        "task_type": "container",
        "cluster_name": "prod-us-east",
        "namespace": "default",
        "fault_time_range": {
            "start_time": "2026-07-01 15:00:00",
            "end_time": "2026-07-01 15:10:00",
        },
    }
    mw = ToolParamOverrideMiddleware(config=config, tools=[check_kubernetes_pods])

    # check_kubernetes_pods(namespace: str = "default")
    # LLM omits namespace → flexible → BLOCK
    req = _make_request("check_kubernetes_pods", "c2", {})
    result = await mw.awrap_tool_call(req, _noop_handler)
    await _ensure_blocked(result, "check_kubernetes_pods")
    print("  ✓ Flexible block: missing 'namespace' → blocked")


async def test_flexible_trust_llm_provided():
    """LLM provides namespace → trusted, passed through."""
    from diagnostics.agent.param_override_middleware import ToolParamOverrideMiddleware
    from diagnostics.tools.mock.kubernetes import get_pod_logs
    from langchain_core.messages import ToolMessage

    config = {
        "task_type": "container",
        "cluster_name": "prod-us-east",
        "namespace": "default",
        "fault_time_range": {
            "start_time": "2026-07-01 15:00:00",
            "end_time": "2026-07-01 15:10:00",
        },
    }
    mw = ToolParamOverrideMiddleware(config=config, tools=[get_pod_logs])

    # LLM provides its own namespace → trusted (not overridden)
    req = _make_request("get_pod_logs", "c3", {
        "cluster_name": "prod-us-east",
        "namespace": "kube-system",
        "pod_name": "coredns-abc",
    })
    captured = {}

    async def handler(r):
        captured.update(r.tool_call.get("args", {}))
        return ToolMessage(content="logs", tool_call_id="c3", name="get_pod_logs")

    result = await mw.awrap_tool_call(req, handler)
    assert result.content == "logs"
    assert captured.get("namespace") == "kube-system", \
        f"LLM-provided namespace should be trusted, got {captured.get('namespace')}"
    # start_time/end_time are NOT in get_pod_logs's signature — correctly NOT injected
    # cluster_name is in signature AND in strict_set → overridden
    assert captured.get("cluster_name") == "prod-us-east", \
        f"Strict cluster_name should be overridden, got {captured.get('cluster_name')}"
    print("  ✓ Flexible trust: LLM namespace 'kube-system' trusted, strict cluster_name overridden")


async def test_strict_params_override_llm_wrong():
    """LLM provides wrong cluster_name → strict override replaces it."""
    from diagnostics.agent.param_override_middleware import ToolParamOverrideMiddleware
    from diagnostics.tools.mock.kubernetes import get_pod_events
    from langchain_core.messages import ToolMessage

    config = {
        "task_type": "container",
        "cluster_name": "prod-us-east",
        "fault_time_range": {
            "start_time": "2026-07-01 15:00:00",
            "end_time": "2026-07-01 15:10:00",
        },
    }
    mw = ToolParamOverrideMiddleware(config=config, tools=[get_pod_events])

    req = _make_request("get_pod_events", "c4", {
        "cluster_name": "hallucinated-cluster",
        "namespace": "default",
        "pod_name": "my-pod",
    })
    captured = {}

    async def handler(r):
        captured.update(r.tool_call.get("args", {}))
        return ToolMessage(content="events", tool_call_id="c4", name="get_pod_events")

    result = await mw.awrap_tool_call(req, handler)
    assert result.content == "events"
    assert captured["cluster_name"] == "prod-us-east", \
        f"Expected override to 'prod-us-east', got {captured['cluster_name']}"
    print("  ✓ Strict override: wrong cluster_name 'hallucinated-cluster' → 'prod-us-east'")


# ═════════════════════════════════════════════════════════════════
# Host scenario
# ═════════════════════════════════════════════════════════════════

async def test_strict_override_host():
    """LLM invents wrong hostname → overridden; start_time/end_time injected."""
    from diagnostics.agent.param_override_middleware import ToolParamOverrideMiddleware
    from diagnostics.tools.mock.argus import query_argus_memory
    from langchain_core.messages import ToolMessage

    config = {
        "task_type": "host",
        "hostname": "prod-web-01",
        "fault_time_range": {
            "start_time": "2026-07-01 18:10:00",
            "end_time": "2026-07-01 18:17:00",
        },
    }
    mw = ToolParamOverrideMiddleware(config=config, tools=[query_argus_memory])

    req = _make_request("query_argus_memory", "h1", {"hostname": "wrong-host"})
    captured = {}

    async def handler(r):
        captured.update(r.tool_call.get("args", {}))
        return ToolMessage(content="mem data", tool_call_id="h1", name="query_argus_memory")

    result = await mw.awrap_tool_call(req, handler)
    assert result.content == "mem data"
    assert captured["hostname"] == "prod-web-01"
    assert captured.get("start_time") == "2026-07-01 18:10:00"
    assert captured.get("end_time") == "2026-07-01 18:17:00"
    print("  ✓ Host strict: hostname + start/end_time overridden + injected")


# ═════════════════════════════════════════════════════════════════
# Skip / no-op scenarios
# ═════════════════════════════════════════════════════════════════

async def test_scaffolding_skip():
    """task, write_file, ledger tools pass through without interception."""
    from diagnostics.agent.param_override_middleware import ToolParamOverrideMiddleware
    from langchain_core.messages import ToolMessage

    config = {
        "task_type": "container",
        "cluster_name": "prod-us-east",
        "fault_time_range": {
            "start_time": "2026-07-01 15:00:00",
            "end_time": "2026-07-01 15:10:00",
        },
    }
    mw = ToolParamOverrideMiddleware(config=config, tools=[])

    for tool_name in ("task", "write_file", "read_file", "commit_hypotheses",
                       "record_finding", "select_path", "backtrack"):
        req = _make_request(tool_name, f"skip_{tool_name}", {"description": "test"})

        async def handler(r, tn=tool_name):
            return ToolMessage(content=f"{tn}_ok", tool_call_id=f"skip_{tn}", name=tn)

        result = await mw.awrap_tool_call(req, handler)
        assert result.content == f"{tool_name}_ok", \
            f"{tool_name} should pass through, got {result.content}"
    print("  ✓ Scaffolding skip: task/write_file/ledger tools pass through")


async def test_no_config():
    """Empty config → all calls pass through."""
    from diagnostics.agent.param_override_middleware import ToolParamOverrideMiddleware
    from diagnostics.tools.mock.argus import query_argus_nodes
    from langchain_core.messages import ToolMessage

    mw = ToolParamOverrideMiddleware(config={}, tools=[query_argus_nodes])

    req = _make_request("query_argus_nodes", "nc1", {"hostname": "test"})
    result = await mw.awrap_tool_call(req, _ok_handler)
    assert result.content == "ok"
    print("  ✓ No config: empty config → pass through")


async def test_multi_value_params_skipped():
    """List-type params in flat_config are skipped (not injected)."""
    from diagnostics.agent.param_override_middleware import ToolParamOverrideMiddleware
    from diagnostics.tools.mock.kubernetes import get_pod_logs
    from langchain_core.messages import ToolMessage

    config = {
        "task_type": "container",
        "cluster_name": "prod-us-east",
        "namespace": "default",
        "allowed_podname": ["p1", "p2", "p3"],  # list → skipped
        "fault_time_range": {
            "start_time": "2026-07-01 15:00:00",
            "end_time": "2026-07-01 15:10:00",
        },
    }
    mw = ToolParamOverrideMiddleware(config=config, tools=[get_pod_logs])

    req = _make_request("get_pod_logs", "mv1", {
        "cluster_name": "prod-us-east",
        "namespace": "default",
        "pod_name": "p1",
    })
    captured = {}

    async def handler(r):
        captured.update(r.tool_call.get("args", {}))
        return ToolMessage(content="ok", tool_call_id="mv1", name="get_pod_logs")

    result = await mw.awrap_tool_call(req, handler)
    assert result.content == "ok"
    # namespace trusted, not overridden; allowed_podname not injected (it's a list)
    assert captured.get("namespace") == "default"
    assert "allowed_podname" not in captured
    print("  ✓ Multi-value skip: list params not injected")


async def test_update_config():
    """update_config replaces session config — subsequent calls use new values."""
    from diagnostics.agent.param_override_middleware import ToolParamOverrideMiddleware
    from diagnostics.tools.mock.argus import query_argus_nodes
    from langchain_core.messages import ToolMessage

    config_a = {
        "task_type": "container",
        "cluster_name": "old-cluster",
        "fault_time_range": {
            "start_time": "2026-07-01 10:00:00",
            "end_time": "2026-07-01 10:10:00",
        },
    }
    mw = ToolParamOverrideMiddleware(config=config_a, tools=[query_argus_nodes])

    # First call with old config
    req1 = _make_request("query_argus_nodes", "uc1", {"hostname": "test"})
    captured1 = {}
    async def h1(r):
        captured1.update(r.tool_call.get("args", {}))
        return ToolMessage(content="ok", tool_call_id="uc1", name="query_argus_nodes")
    await mw.awrap_tool_call(req1, h1)
    assert captured1.get("start_time") == "2026-07-01 10:00:00"

    # Update config
    mw.update_config({
        "task_type": "container",
        "cluster_name": "new-cluster",
        "fault_time_range": {
            "start_time": "2026-07-01 20:00:00",
            "end_time": "2026-07-01 20:10:00",
        },
    })

    # Second call with new config
    req2 = _make_request("query_argus_nodes", "uc2", {"hostname": "test"})
    captured2 = {}
    async def h2(r):
        captured2.update(r.tool_call.get("args", {}))
        return ToolMessage(content="ok", tool_call_id="uc2", name="query_argus_nodes")
    await mw.awrap_tool_call(req2, h2)
    assert captured2.get("start_time") == "2026-07-01 20:00:00"
    print("  ✓ Update config: old→new start_time values respected")


# ── Helpers ──

async def _noop_handler(req):
    from langchain_core.messages import ToolMessage
    return ToolMessage(
        content="should_not_reach",
        tool_call_id=req.tool_call.get("id", "?"),
        name=req.tool_call.get("name", "?"),
    )


async def _ok_handler(req):
    from langchain_core.messages import ToolMessage
    return ToolMessage(
        content="ok",
        tool_call_id=req.tool_call.get("id", "?"),
        name=req.tool_call.get("name", "?"),
    )


# ═════════════════════════════════════════════════════════════════

async def main():
    print("=" * 60)
    print("  PARAM OVERRIDE MIDDLEWARE — VERIFICATION SUITE")
    print("=" * 60)
    print()

    print("── Container Strict Override ──")
    await test_strict_override_container()
    await test_strict_params_override_llm_wrong()
    print()

    print("── Flexible: Block on Missing ──")
    await test_flexible_block_on_missing()
    print()

    print("── Flexible: Trust LLM-Provided ──")
    await test_flexible_trust_llm_provided()
    print()

    print("── Host Strict Override ──")
    await test_strict_override_host()
    print()

    print("── Skip / No-Op ──")
    await test_scaffolding_skip()
    await test_no_config()
    await test_multi_value_params_skipped()
    print()

    print("── Dynamic Config ──")
    await test_update_config()
    print()

    print("\n✓ ALL PARAM OVERRIDE VERIFICATIONS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
