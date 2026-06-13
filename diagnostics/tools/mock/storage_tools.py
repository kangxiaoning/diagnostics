"""Disk/Storage diagnostic tool."""

from langchain_core.tools import tool

from diagnostics.tools.data import disk_data
from diagnostics.tools.scenarios import _active_scenario


@tool
def check_disk() -> str:
    """Check disk IO performance, utilization, latency (iostat), and filesystem usage (df)."""
    return disk_data(_active_scenario)
