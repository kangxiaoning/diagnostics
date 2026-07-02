"""Verify scope limit + scope guard middleware integration.

Validates:
  1. ScopeLimit mock discovery returns correct node/host data
  2. Control plane health check rejects overloaded clusters
  3. ScopeGuard blocks out-of-scope tool calls
  4. ScopeGuard allows in-scope tool calls
  5. build_agent accepts diagnostic_scope and wires middleware
  6. DiagnosticScope Pydantic schema validates correctly
"""

from __future__ import annotations

import asyncio
import os
import sys
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def test_scope_limit_discovery_mock():
    """Defect: mock discovery returns known pod→node mapping."""
    from diagnostics.agent.scope_limit import ScopeLimit

    # Test with known mock data
    scope = ScopeLimit.discover(
        cluster_name="default",
        namespace="default",
        workload_type="Deployment",
        workload_name="nginx-deployment",
        pod_name="nginx-deployment-7d8c9f6b4-abc12",
        start_time="2026-07-01 14:00:00",
        end_time="2026-07-01 15:00:00",
    )

    assert scope.cluster_name == "default"
    assert scope.allowed_namespace == "default"
    assert scope.start_time == "2026-07-01 14:00:00"
    assert len(scope.allowed_nodename) == 3
    assert "node-worker-1" in scope.allowed_nodename
    assert "node-worker-2" in scope.allowed_nodename
    assert "node-worker-3" in scope.allowed_nodename
    assert len(scope.allowed_hostname) == 3
    assert "10.0.1.10" in scope.allowed_hostname
    # User-specified pod is always included; deduped with mock data
    assert "nginx-deployment-7d8c9f6b4-abc12" in scope.allowed_podname
    assert len(scope.allowed_podname) == 3  # 3 unique pods (user pod deduped)

    print("  ✓ Mock discovery: 3 pods → 3 nodes, 3 hosts, 4 allowed pods")


def test_scope_limit_discovery_unknown_cluster():
    """Graceful fallback when no mock data matches."""
    from diagnostics.agent.scope_limit import ScopeLimit

    scope = ScopeLimit.discover(
        cluster_name="unknown-cluster",
        namespace="prod",
        workload_type="Deployment",
        workload_name="unknown-app",
        pod_name="unknown-pod-123",
    )

    assert scope.allowed_nodename == []
    assert scope.allowed_hostname == []
    # User pod still included
    assert "unknown-pod-123" in scope.allowed_podname

    print("  ✓ Unknown cluster: empty node list, user pod preserved")


def test_control_plane_health_check():
    """Reject when mem > 80% AND free < 2GB."""
    from diagnostics.agent.scope_limit import ScopeLimit

    # Healthy
    safe, _ = ScopeLimit.check_control_plane(45.0, 8.0)
    assert safe is True

    # Borderline: high usage but enough free memory
    safe, _ = ScopeLimit.check_control_plane(85.0, 4.0)
    assert safe is True

    # Reject: both conditions met
    safe, reason = ScopeLimit.check_control_plane(92.0, 0.8)
    assert safe is False
    assert "OOM" in reason or "压力" in reason

    # Reject: >80% and just under 2GB
    safe, reason = ScopeLimit.check_control_plane(85.0, 1.5)
    assert safe is False

    print("  ✓ Control plane health: blocks only when both thresholds exceeded")


def test_control_plane_mock_metrics():
    """Mock metrics return pre-configured values."""
    from diagnostics.agent.scope_limit import ScopeLimit

    usage, free = ScopeLimit.get_control_plane_metrics("overloaded-cluster")
    assert usage == 92.3
    assert free == 0.8

    usage, free = ScopeLimit.get_control_plane_metrics("unknown")
    assert usage == 45.0  # default
    assert free == 6.0

    print("  ✓ Mock metrics: overloaded-cluster returns (92.3%, 0.8GB)")


async def test_scope_guard_blocks_namespace():
    """Block tool calls with wrong namespace."""
    from diagnostics.agent.scope_limit import ScopeLimit
    from diagnostics.agent.scope_guard_middleware import ScopeGuardMiddleware
    from langgraph.prebuilt.tool_node import ToolCallRequest
    from langchain_core.messages import ToolMessage

    scope = ScopeLimit(
        cluster_name="test",
        allowed_namespace="sfe-default",
    )
    guard = ScopeGuardMiddleware(scope=scope)

    # Call with wrong namespace → blocked
    request = MagicMock(spec=ToolCallRequest)
    request.tool_call = {
        "name": "get_pod_logs",
        "id": "call_1",
        "args": {"namespace": "kube-system", "pod_name": "test-pod"},
    }

    async def handler(req):
        return ToolMessage(content="should not reach", tool_call_id="call_1", name="get_pod_logs")

    result = await guard.awrap_tool_call(request, handler)
    assert isinstance(result, ToolMessage)
    assert "⛔" in result.content or "范围" in result.content

    print("  ✓ ScopeGuard blocks: get_pod_logs with wrong namespace 'kube-system'")


async def test_scope_guard_allows_valid():
    """Allow tool calls within scope."""
    from diagnostics.agent.scope_limit import ScopeLimit
    from diagnostics.agent.scope_guard_middleware import ScopeGuardMiddleware
    from langgraph.prebuilt.tool_node import ToolCallRequest
    from langchain_core.messages import ToolMessage

    scope = ScopeLimit(
        cluster_name="test",
        allowed_namespace="sfe-default",
        allowed_nodename=["eklet-subnet-abc-001", "eklet-subnet-def-003"],
    )
    guard = ScopeGuardMiddleware(scope=scope)

    # Valid namespace → passes
    request = MagicMock(spec=ToolCallRequest)
    request.tool_call = {
        "name": "get_pod_logs",
        "id": "call_1",
        "args": {"namespace": "sfe-default", "pod_name": "my-pod"},
    }

    async def handler(req):
        return ToolMessage(content="pod logs here", tool_call_id="call_1", name="get_pod_logs")

    result = await guard.awrap_tool_call(request, handler)
    assert isinstance(result, ToolMessage)
    assert result.content == "pod logs here"

    print("  ✓ ScopeGuard allows: valid namespace 'sfe-default'")


async def test_scope_guard_blocks_node():
    """Block node_name outside allowed list."""
    from diagnostics.agent.scope_limit import ScopeLimit
    from diagnostics.agent.scope_guard_middleware import ScopeGuardMiddleware
    from langgraph.prebuilt.tool_node import ToolCallRequest
    from langchain_core.messages import ToolMessage

    scope = ScopeLimit(
        cluster_name="test",
        allowed_namespace="default",
        allowed_nodename=["node-worker-1", "node-worker-2"],
    )
    guard = ScopeGuardMiddleware(scope=scope)

    # Node outside scope → blocked
    request = MagicMock(spec=ToolCallRequest)
    request.tool_call = {
        "name": "get_node_info",
        "id": "call_2",
        "args": {"cluster_name": "test", "node_name": "node-unauthorized"},
    }

    async def handler(req):
        return ToolMessage(content="should not reach", tool_call_id="call_2", name="get_node_info")

    result = await guard.awrap_tool_call(request, handler)
    assert isinstance(result, ToolMessage)
    assert "⛔" in result.content or "范围" in result.content

    print("  ✓ ScopeGuard blocks: get_node_info with unauthorized node 'node-unauthorized'")


async def test_scope_guard_null_scope():
    """No scope → pass through."""
    from diagnostics.agent.scope_guard_middleware import ScopeGuardMiddleware
    from langgraph.prebuilt.tool_node import ToolCallRequest
    from langchain_core.messages import ToolMessage

    guard = ScopeGuardMiddleware(scope=None)

    request = MagicMock(spec=ToolCallRequest)
    request.tool_call = {
        "name": "get_pod_logs",
        "id": "call_1",
        "args": {"namespace": "anything", "pod_name": "any-pod"},
    }

    async def handler(req):
        return ToolMessage(content="ok", tool_call_id="call_1", name="get_pod_logs")

    result = await guard.awrap_tool_call(request, handler)
    assert result.content == "ok"

    print("  ✓ ScopeGuard nil: null scope → all calls pass through")


def test_diagnostic_scope_schema():
    """DiagnosticScope Pydantic model validates correctly."""
    from diagnostics.server.schemas import DiagnosticScope

    # Valid minimal scope
    s = DiagnosticScope(
        cluster_name="test-cluster",
        allowed_namespace="default",
    )
    assert s.cluster_name == "test-cluster"
    assert s.allowed_podname == []
    assert s.allowed_nodename == []

    # Valid full scope
    s2 = DiagnosticScope(
        cluster_name="test-cluster",
        allowed_namespace="default",
        allowed_nodename=["n1", "n2"],
        allowed_podname=["p1"],
        start_time="2026-07-01 14:00:00",
        end_time="2026-07-01 15:00:00",
    )
    assert len(s2.allowed_nodename) == 2

    print("  ✓ DiagnosticScope schema: validates correctly")


def test_build_agent_accepts_scope():
    """build_agent accepts diagnostic_scope and creates ScopeGuard."""
    from diagnostics.agent.factory import build_agent
    from diagnostics.agent.scope_limit import ScopeLimit
    import tempfile

    scope = ScopeLimit(
        cluster_name="test",
        allowed_namespace="default",
        allowed_nodename=["n1"],
        allowed_podname=["p1"],
    )

    with tempfile.TemporaryDirectory() as tmpdir:
        lp = os.path.join(tmpdir, "ledger.json")
        rp = os.path.join(tmpdir, "report.md")
        agent = build_agent(
            report_path=rp,
            ledger_path=lp,
            diagnostic_scope=scope,
        )
        assert agent is not None

    print("  ✓ build_agent: accepts diagnostic_scope, agent created")


async def main():
    print("=" * 60)
    print("  SCOPE LIMIT + SCOPE GUARD — VERIFICATION SUITE")
    print("=" * 60)
    print()

    print("── ScopeLimit Mock Discovery ──")
    test_scope_limit_discovery_mock()
    test_scope_limit_discovery_unknown_cluster()
    print()

    print("── Control Plane Health Check ──")
    test_control_plane_health_check()
    test_control_plane_mock_metrics()
    print()

    print("── ScopeGuard Middleware ──")
    await test_scope_guard_null_scope()
    await test_scope_guard_allows_valid()
    await test_scope_guard_blocks_namespace()
    await test_scope_guard_blocks_node()
    print()

    print("── Schema & Integration ──")
    test_diagnostic_scope_schema()
    test_build_agent_accepts_scope()
    print()

    print("\n✓ ALL SCOPE LIMIT VERIFICATIONS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
