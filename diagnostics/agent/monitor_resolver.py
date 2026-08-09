"""Monitor name resolver — pre-resolution of Argus monitor_name.

实际环境：monitor_name 是**创建 agent 前**根据用户输入查询得到的——
- 单主机、专有集群单 Pod 场景：查询得到的 monitor_name 保存在
  ``param_overrides`` 中，供 host-argus-expert / k8s-argus-expert 的
  Argus 工具首参使用；
- Serverless 场景：monitor_name 保存在 RelationGraph（拓扑 mapping 的
  serverless_monitor_name / kmc_monitor_name / sci_monitor_name）中，
  由 topology 注入提供，不在本模块解析。

Mock 模式按同一时机（hostname 预解析之后、build_agent 之前）根据用户
输入解析 monitor_name 并写入 ``param_overrides["monitor_name"]``。
生产模式预留：实际查询逻辑未实现，透传不改变。
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)


def _resolve_mock(param_overrides: dict) -> dict:
    """Mock monitor_name resolution from user input.

    专有集群场景：monitor_name = f"monitor-{cluster_name}"
    单主机场景：monitor_name = f"monitor-{hostname}"（hostname 已由
    hostname_resolver 预解析写入 param_overrides）。
    """
    # Serverless 场景由 RelationGraph 提供 monitor_name，不在此解析
    if param_overrides.get("cluster_type") == "serverless":
        logger.debug(
            "Monitor resolver: serverless scene — monitor_name from RelationGraph, skipping"
        )
        return param_overrides

    cluster_name = param_overrides.get("cluster_name", "")
    hostname = param_overrides.get("hostname", "")

    if cluster_name:
        monitor_name = f"monitor-{cluster_name}"
    elif hostname:
        monitor_name = f"monitor-{hostname}"
    else:
        logger.debug(
            "Monitor resolver: no cluster_name/hostname in param_overrides, skipping"
        )
        return param_overrides

    logger.info(
        "Monitor resolver: cluster=%r hostname=%r → monitor_name=%r",
        cluster_name, hostname, monitor_name,
    )
    param_overrides["monitor_name"] = monitor_name
    return param_overrides


def _resolve_production(param_overrides: dict) -> dict:
    """Production monitor_name resolution via monitor query API (not yet implemented).

    When implemented, this would query the monitoring system by user input
    (cluster_name / hostname) to obtain the Argus monitor_name.  For now,
    falls through unchanged.
    """
    logger.debug("Monitor resolver: production mode — no monitor query API integration yet")
    return param_overrides


def resolve_monitor_name(param_overrides: dict | None) -> dict | None:
    """Pre-resolve Argus monitor_name from user input.

    Called before ``build_agent()`` in the stream handler (after hostname
    pre-resolution).  Only operates on host / dedicated-cluster scenarios;
    Serverless scenes keep their RelationGraph-provided monitor_name.

    Returns the enriched ``param_overrides`` dict (mutated in-place for
    convenience, but the return value is the canonical reference).
    """
    if not param_overrides:
        return param_overrides

    # Don't override if monitor_name is already explicitly provided
    if param_overrides.get("monitor_name"):
        logger.debug(
            "Monitor resolver: monitor_name already set to %r, skipping",
            param_overrides["monitor_name"],
        )
        return param_overrides

    mode = os.getenv("DIAGNOSTICS_MODE", "mock").lower()
    if mode == "production":
        return _resolve_production(param_overrides)
    return _resolve_mock(param_overrides)
