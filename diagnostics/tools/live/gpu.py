"""GPU live diagnostic tools — production-aligned surface (5 tools).

These tools are placeholder implementations that map to shell scripts
in diagnostics/tools/scripts/ for actual data collection.

One-to-one correspondence: mock/gpu.py
"""

from __future__ import annotations

from langchain_core.tools import tool


def _live(what: str) -> str:
    return f"[LIVE] {what} — execute collection script via remote shell on target GPU node."


@tool
def get_gpu_pcie_info() -> str:
    """获取主机GPU PCIe信息：链路速率、带宽、拓扑。"""
    return _live("GPU PCIe topology & link status (nvidia-smi -q PCIE)")


@tool
def get_gpu_driver_info() -> str:
    """获取GPU驱动信息：驱动版本、CUDA 版本、驱动加载状态。"""
    return _live("GPU driver info (nvidia-smi / cat /proc/driver/nvidia/version)")


@tool
def get_gpu_mod_info() -> str:
    """获取GPU模块信息：nvidia 内核模块加载状态与版本。"""
    return _live("GPU kernel module info (lsmod | grep nvidia / modinfo)")


@tool
def get_gpu_status_info() -> str:
    """获取GPU当前详细信息：健康（温度/功耗/限速/ECC/PCIe）、显存、利用率。"""
    return _live("GPU full status (nvidia-smi -q)")


@tool
def get_gpu_dmesg_info() -> str:
    """获取GPU错误日志：Xid 错误、NVRM 告警（来自节点 dmesg）。"""
    return _live("dmesg | grep -i 'xid|nvrm'")
