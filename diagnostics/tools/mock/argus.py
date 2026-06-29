"""Argus monitoring query tools — 1min granularity metrics for Coordinator.

Each tool returns a 10-min timeline for a specific subsystem.
Coordinator uses these to build a time-based fault profile BEFORE
delegating deep diagnosis to experts.

All tools accept 3 optional parameters:
- name_chunk: endpoint filter (hostname, Pod name, etc.)
- start_time / end_time: time range filter (ISO or HH:MM format)
"""

from langchain_core.tools import tool

from diagnostics.tools.mock.argus_data import (
    query_argus_cpu_metrics,
    query_argus_disk_metrics,
    query_argus_k8s_metrics,
    query_argus_memory_metrics,
    query_argus_network_metrics,
)
from diagnostics.tools.mock.scenarios import get_active_scenario


@tool
def query_argus_cpu(
    name_chunk: str = "",
    start_time: str = "",
    end_time: str = "",
) -> str:
    """Query Argus for 1min-granularity CPU metrics (10-min window).

    Args:
        name_chunk: Endpoint filter — hostname or partial name (e.g. "prod-web-01", "worker-3").
            Leave empty to return all available endpoints.
        start_time: Query start time (e.g. "15:00", "2026-06-29T15:00:00").
            Leave empty for default window.
        end_time: Query end time (e.g. "15:10", "2026-06-29T15:10:00").
            Leave empty for default window.

    Returns per-minute: CPU utilization%, load average, top process summary.
    Use this to identify CPU spike timing, iowait trends, and scheduling pressure.
    """
    return query_argus_cpu_metrics(get_active_scenario(), name_chunk, start_time, end_time)


@tool
def query_argus_memory(
    name_chunk: str = "",
    start_time: str = "",
    end_time: str = "",
) -> str:
    """Query Argus for 1min-granularity memory metrics (10-min window).

    Args:
        name_chunk: Endpoint filter — hostname or partial name (e.g. "prod-web-01", "worker-3").
            Leave empty to return all available endpoints.
        start_time: Query start time (e.g. "15:00", "2026-06-29T15:00:00").
            Leave empty for default window.
        end_time: Query end time (e.g. "15:10", "2026-06-29T15:10:00").
            Leave empty for default window.

    Returns per-minute: Mem%, Swap%, available memory, OOM events.
    Use this to identify memory pressure onset and OOM timeline.
    """
    return query_argus_memory_metrics(get_active_scenario(), name_chunk, start_time, end_time)


@tool
def query_argus_disk(
    name_chunk: str = "",
    start_time: str = "",
    end_time: str = "",
) -> str:
    """Query Argus for 1min-granularity disk IO metrics (10-min window).

    Args:
        name_chunk: Endpoint filter — hostname or partial name (e.g. "prod-web-01", "worker-3").
            Leave empty to return all available endpoints.
        start_time: Query start time (e.g. "15:00", "2026-06-29T15:00:00").
            Leave empty for default window.
        end_time: Query end time (e.g. "15:10", "2026-06-29T15:10:00").
            Leave empty for default window.

    Returns per-minute: disk util%, iops, await, throughput.
    Use this to identify IO saturation onset and bottleneck timing.
    """
    return query_argus_disk_metrics(get_active_scenario(), name_chunk, start_time, end_time)


@tool
def query_argus_network(
    name_chunk: str = "",
    start_time: str = "",
    end_time: str = "",
) -> str:
    """Query Argus for 1min-granularity network metrics (10-min window).

    Args:
        name_chunk: Endpoint filter — hostname or partial name (e.g. "prod-web-01", "worker-3").
            Leave empty to return all available endpoints.
        start_time: Query start time (e.g. "15:00", "2026-06-29T15:00:00").
            Leave empty for default window.
        end_time: Query end time (e.g. "15:10", "2026-06-29T15:10:00").
            Leave empty for default window.

    Returns per-minute: bandwidth, packet drops, TCP retransmit%, connection counts.
    Use this to identify packet loss onset, retransmit spikes, and bandwidth saturation.
    """
    return query_argus_network_metrics(get_active_scenario(), name_chunk, start_time, end_time)


@tool
def query_argus_kubernetes(
    name_chunk: str = "",
    start_time: str = "",
    end_time: str = "",
) -> str:
    """Query Argus for 1min-granularity K8s cluster metrics (10-min window).

    Args:
        name_chunk: Endpoint filter — cluster name, node name, or namespace
            (e.g. "prod-us-east", "worker-3"). Leave empty for cluster-wide view.
        start_time: Query start time (e.g. "15:00", "2026-06-29T15:00:00").
            Leave empty for default window.
        end_time: Query end time (e.g. "15:10", "2026-06-29T15:10:00").
            Leave empty for default window.

    Returns per-minute: node status changes, pod restart counts, API server latency,
    etcd metrics, DNS query latency.
    Use this to identify WHEN K8s anomalies began and their progression.
    """
    return query_argus_k8s_metrics(get_active_scenario(), name_chunk, start_time, end_time)
