from __future__ import annotations

import shlex
import subprocess

from langchain_core.tools import tool


MAX_OUTPUT_CHARS = 20_000


def run_command(argv: list[str], timeout: int = 12, max_lines: int | None = None) -> str:
    try:
        completed = subprocess.run(
            argv,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError:
        return f"command not found: {argv[0]}"
    except subprocess.TimeoutExpired as exc:
        output = (exc.stdout or "") + (exc.stderr or "")
        return f"timeout after {timeout}s\n{output[:MAX_OUTPUT_CHARS]}"

    output = (completed.stdout or "") + (completed.stderr or "")
    if max_lines is not None:
        output = "\n".join(output.splitlines()[:max_lines])
    output = output.strip() or "(no output)"
    if len(output) > MAX_OUTPUT_CHARS:
        output = output[:MAX_OUTPUT_CHARS] + "\n...[truncated]"
    return f"$ {shlex.join(argv)}\nexit_code={completed.returncode}\n{output}"


@tool
def run_diagnostic_profile(profile: str) -> str:
    """Run a safe, predefined Linux performance diagnostic profile.

    Valid profiles: overview, cpu, memory, io, network, processes_cpu,
    processes_memory, containers.
    """
    profiles: dict[str, list[tuple[list[str], int, int | None]]] = {
        "overview": [
            (["uname", "-a"], 5, None),
            (["uptime"], 5, None),
            (["date"], 5, None),
        ],
        "cpu": [
            (["lscpu"], 8, None),
            (["vmstat", "1", "5"], 8, None),
            (["mpstat", "-P", "ALL", "1", "3"], 8, None),
        ],
        "memory": [
            (["free", "-h"], 5, None),
            (["vmstat", "-s"], 5, None),
            (["cat", "/proc/meminfo"], 5, 80),
        ],
        "io": [
            (["iostat", "-xz", "1", "3"], 10, None),
            (["df", "-hT"], 8, None),
            (["cat", "/proc/diskstats"], 5, 80),
        ],
        "network": [
            (["ss", "-s"], 5, None),
            (["ip", "-s", "link"], 8, None),
            (["cat", "/proc/net/dev"], 5, None),
        ],
        "processes_cpu": [
            (
                ["ps", "-eo", "pid,ppid,user,stat,pcpu,pmem,comm,args", "--sort=-pcpu"],
                8,
                30,
            ),
        ],
        "processes_memory": [
            (
                ["ps", "-eo", "pid,ppid,user,stat,pcpu,pmem,comm,args", "--sort=-pmem"],
                8,
                30,
            ),
        ],
        "containers": [
            (["docker", "ps", "--no-trunc"], 8, 40),
            (["docker", "stats", "--no-stream"], 8, 40),
        ],
    }
    normalized = profile.strip().lower()
    if normalized not in profiles:
        return f"unknown profile: {profile}. Valid profiles: {', '.join(profiles)}"

    chunks = [run_command(argv, timeout, max_lines) for argv, timeout, max_lines in profiles[normalized]]
    return "\n\n".join(chunks)


@tool
def read_proc_file(path: str) -> str:
    """Read a whitelisted Linux /proc file useful for performance diagnosis."""
    allowed = {
        "/proc/cpuinfo",
        "/proc/loadavg",
        "/proc/meminfo",
        "/proc/pressure/cpu",
        "/proc/pressure/io",
        "/proc/pressure/memory",
        "/proc/stat",
        "/proc/swaps",
        "/proc/uptime",
        "/proc/vmstat",
    }
    normalized = path.strip()
    if normalized not in allowed:
        return f"refused: {path}. Allowed files: {', '.join(sorted(allowed))}"
    return run_command(["cat", normalized], timeout=5, max_lines=160)


@tool
def explain_available_diagnostics() -> str:
    """List the built-in diagnostic profiles and what each one samples."""
    return (
        "overview: uname, uptime, date\n"
        "cpu: lscpu, vmstat, mpstat\n"
        "memory: free, vmstat -s, /proc/meminfo\n"
        "io: iostat, df, /proc/diskstats\n"
        "network: ss, ip -s link, /proc/net/dev\n"
        "processes_cpu: top CPU processes via ps\n"
        "processes_memory: top memory processes via ps\n"
        "containers: docker ps and docker stats when Docker is installed"
    )


def get_linux_diagnostic_tools() -> list:
    return [
        run_diagnostic_profile,
        read_proc_file,
        explain_available_diagnostics,
    ]
