"""Mock GPU diagnostic tools — health, memory, and utilization.

One-to-one correspondence: live/gpu.py
"""

from langchain_core.tools import tool

from diagnostics.tools.mock.data import (
    gpu_health_data,
    gpu_memory_data,
    gpu_utilization_data,
)
from diagnostics.tools.mock.scenarios import get_active_scenario


@tool
def check_gpu_health() -> str:
    """Check GPU temperature, power, throttling status, ECC errors, and PCIe link.

    Returns nvidia-smi style output with health indicators.
    """
    return gpu_health_data(get_active_scenario())


@tool
def check_gpu_memory() -> str:
    """Check GPU memory usage, per-process allocation, and OOM indicators.

    Returns GPU memory breakdown and process-level allocation details.
    """
    return gpu_memory_data(get_active_scenario())


@tool
def check_gpu_utilization() -> str:
    """Check GPU core utilization, SM activity, memory bandwidth, and encoder/decoder usage.

    Returns detailed GPU utilization metrics for performance analysis.
    """
    return gpu_utilization_data(get_active_scenario())
