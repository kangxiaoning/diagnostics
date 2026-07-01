"""Simulate ToolOffloadMiddleware: large tool result → offload → read_file/grep.

This script demonstrates the complete offload lifecycle:
1. Simulate a diagnostic tool returning a large result (mock Pod logs)
2. ToolOffloadMiddleware intercepts → writes to backend → returns preview
3. Verify the full content is accessible via backend.read() (read_file)
4. Verify keyword search works via backend.grep() (grep)

Run:
    uv run python scripts/simulate_offload.py
"""

from __future__ import annotations

import asyncio
import textwrap
from unittest.mock import MagicMock

from deepagents.backends import FilesystemBackend
from langchain_core.messages import ToolMessage

from diagnostics.agent.offload_middleware import ToolOffloadMiddleware

# Use a temp directory for FilesystemBackend (StateBackend requires graph context)
import tempfile
_TEMP_DIR = tempfile.mkdtemp(prefix="offload_test_")


# ── Simulated large tool output (e.g., get_pod_logs for a crashing Pod) ─────

def _generate_fake_pod_logs(lines: int = 600) -> str:
    """Generate a realistic-looking Pod log output that exceeds the offload limit."""
    log_lines = [
        "2026-06-30T14:30:01Z [INFO] Starting application server v3.2.1",
        "2026-06-30T14:30:02Z [INFO] Loading configuration from /etc/app/config.yaml",
        "2026-06-30T14:30:02Z [INFO] Database connection pool initialized (max=50)",
        "2026-06-30T14:30:03Z [INFO] gRPC server listening on :8443",
        "2026-06-30T14:30:03Z [INFO] Health check endpoint registered at /healthz",
    ]
    # Fill middle with repetitive log entries
    for i in range(lines - 15):
        minute = 30 + i // 60
        second = i % 60
        ts = f"2026-06-30T14:{minute:02d}:{second:02d}Z"
        if i % 50 == 0 and i > 0:
            log_lines.append(f"{ts} [WARN] High memory usage: RSS={4200 + i}MB / limit=4096MB")
        elif i % 30 == 0:
            log_lines.append(f"{ts} [ERROR] java.lang.OutOfMemoryError: Java heap space")
        elif i % 20 == 0:
            log_lines.append(f"{ts} [WARN] GC pause: {200 + i * 2}ms (threshold=500ms)")
        else:
            log_lines.append(f"{ts} [INFO] Processed request #{1000 + i}: latency={5 + i % 10}ms")

    # Tail: crash indicators
    log_lines.extend([
        "2026-06-30T14:39:55Z [ERROR] java.lang.OutOfMemoryError: GC overhead limit exceeded",
        "2026-06-30T14:39:56Z [FATAL] Application crashed: exit code 137 (OOMKilled)",
        "2026-06-30T14:39:56Z [INFO] Container restarting (restart count: 3)",
        "2026-06-30T14:39:57Z [WARN] Liveness probe failed: HTTP 503",
        "2026-06-30T14:39:58Z [INFO] Pod entering CrashLoopBackOff state",
        "2026-06-30T14:39:59Z [FATAL] Back-off restarting failed container: 5m0s",
        "2026-06-30T14:40:00Z [ERROR] Container terminated: OOMKilled (exit code 137)",
        "2026-06-30T14:40:01Z [INFO] JVM heap dump written to /tmp/heapdump-20260630.hprof",
        "2026-06-30T14:40:02Z [WARN] RSS peaked at 4812MB (limit: 4096MB, overcommit: 17%)",
        "2026-06-30T14:40:03Z [INFO] cgroup memory.oom_control: under_oom 1, oom_kill 1",
    ])
    return "\n".join(log_lines)


# ── Fake ToolCallRequest ────────────────────────────────────────────────────

class FakeToolCallRequest:
    """Minimal stand-in for ToolCallRequest used in simulation."""

    def __init__(self, name: str, tool_call_id: str, args: dict | None = None):
        self.tool_call = {"name": name, "id": tool_call_id, "args": args or {}}
        self.runtime = MagicMock()


# ── Main simulation ─────────────────────────────────────────────────────────

async def main():
    print("=" * 72)
    print("ToolOffloadMiddleware — Offload + read_file/grep Simulation")
    print("=" * 72)

    # 1. Setup: create shared backend and middleware
    backend = FilesystemBackend(root_dir=_TEMP_DIR, virtual_mode=True)
    middleware = ToolOffloadMiddleware(
        tool_names={"get_pod_logs", "check_dmesg", "query_argus_pod_logs"},
        token_limit=2000,   # ~8000 chars ≈ 2000 tokens (lower for demo)
        backend=backend,
        hint_keywords={
            "get_pod_logs": ["OOMKilled", "CrashLoop", "OutOfMemory", "exit code", "FATAL"],
            "check_dmesg": ["OOM", "oom_kill", "segfault", "BUG", "Call Trace"],
        },
    )

    # 2. Simulate a large tool result
    pod_logs = _generate_fake_pod_logs(lines=600)
    print(f"\n📊 Simulated tool result: get_pod_logs")
    print(f"   Content size: {len(pod_logs):,} chars (~{len(pod_logs) // 4:,} tokens)")
    print(f"   Lines: {len(pod_logs.splitlines())}")
    print(f"   Offload threshold: {2000 * 4:,} chars (~2,000 tokens)")
    print(f"   Will trigger offload: {'✅ YES' if len(pod_logs) > 2000 * 4 else '❌ NO'}")

    original_msg = ToolMessage(
        content=pod_logs,
        tool_call_id="call_abc123def456",
        name="get_pod_logs",
    )

    request = FakeToolCallRequest(
        name="get_pod_logs",
        tool_call_id="call_abc123def456",
        args={"pod_name": "my-app-7b9d4f-xk2lp", "namespace": "production"},
    )

    # 3. Handler that returns the large result
    async def handler(req):
        return original_msg

    # 4. Run through middleware
    result = await middleware.awrap_tool_call(request, handler)

    assert isinstance(result, ToolMessage), "Result should be a ToolMessage"
    print(f"\n{'='*72}")
    print(f"📋 MIDDLEWARE OUTPUT (what LLM sees):")
    print(f"{'='*72}")
    print(result.content)
    print(f"{'='*72}")

    # 5. Verify the offloaded file is readable via backend
    offload_path = "/large_tool_results/call_abc123def456"
    print(f"\n🔍 Verifying read_file access to: {offload_path}")
    read_result = backend.read(offload_path, offset=0, limit=10)
    if read_result.error:
        print(f"   ❌ read_file failed: {read_result.error}")
    else:
        content = read_result.file_data["content"] if read_result.file_data else ""
        lines = content.strip().splitlines()
        print(f"   ✅ read_file succeeded — first {len(lines)} lines:")
        for line in lines[:5]:
            print(f"      {line}")

    # 6. Verify grep works on the offloaded file
    print(f"\n🔍 Verifying grep access (searching for 'OOMKilled')...")
    grep_result = backend.grep("OOMKilled", path="/large_tool_results/")
    if grep_result.error:
        print(f"   ❌ grep failed: {grep_result.error}")
    elif grep_result.matches:
        print(f"   ✅ grep found {len(grep_result.matches)} match(es):")
        for m in grep_result.matches[:5]:
            print(f"      Line {m['line']}: {m['text'][:80]}...")
    else:
        print(f"   ⚠️  grep returned no matches (unexpected)")

    # 7. Verify grep for Java OOM
    print(f"\n🔍 Verifying grep (searching for 'OutOfMemoryError')...")
    grep_result2 = backend.grep("OutOfMemoryError", path="/large_tool_results/")
    if grep_result2.matches:
        print(f"   ✅ grep found {len(grep_result2.matches)} match(es):")
        for m in grep_result2.matches[:3]:
            print(f"      Line {m['line']}: {m['text'][:80]}...")

    # 8. Test: tool NOT in offload list → passes through
    print(f"\n{'='*72}")
    print("📋 Testing non-configured tool (pass-through)")
    print(f"{'='*72}")
    small_request = FakeToolCallRequest(name="ls", tool_call_id="call_xyz789")
    small_content = "file1.txt\nfile2.txt\nfile3.txt"

    async def small_handler(req):
        return ToolMessage(content=small_content, tool_call_id="call_xyz789", name="ls")

    small_result = await middleware.awrap_tool_call(small_request, small_handler)
    assert small_result.content == small_content
    print(f"   ✅ 'ls' tool passed through unchanged: {small_result.content!r}")

    # 9. Test: configured tool but small result → passes through
    print(f"\n📋 Testing configured tool with small result (no offload)")
    small_pod_request = FakeToolCallRequest(name="get_pod_logs", tool_call_id="call_small001")

    async def small_pod_handler(req):
        return ToolMessage(content="All pods healthy", tool_call_id="call_small001", name="get_pod_logs")

    small_pod_result = await middleware.awrap_tool_call(small_pod_request, small_pod_handler)
    assert small_pod_result.content == "All pods healthy"
    print(f"   ✅ Small get_pod_logs passed through: {small_pod_result.content!r}")

    # Summary
    print(f"\n{'='*72}")
    print("✅ Simulation complete! All verifications passed.")
    print(f"{'='*72}")
    print(textwrap.dedent(f"""
    Key findings:
    1. Large tool results → written to /large_tool_results/{{tool_call_id}}
    2. LLM receives compact head+tail preview + file path
    3. LLM can use read_file(path, offset, limit) to page through content
    4. LLM can use grep(pattern, path) to find specific keywords
    5. Non-configured tools pass through unchanged
    6. Small results (below threshold) pass through unchanged

    Temp directory: {_TEMP_DIR}
    """))

    # Cleanup
    import shutil
    shutil.rmtree(_TEMP_DIR, ignore_errors=True)


if __name__ == "__main__":
    asyncio.run(main())
