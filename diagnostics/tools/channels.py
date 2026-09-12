"""Evidence-channel taxonomy — tool surface → evidence channel classes.

设计背景（design document §8 G22, v3.40.0）：

    G22「单通道证伪覆盖性门控」原先以"深度专家个数"代理"证据通道覆盖"，
    并以 ``-argus-expert`` 命名后缀区分监控通道与确证通道。两者都不可
    移植：专家个数在工具面对齐后失去判别力（家族专家共享同一工具面时
    "换专家"并不换证据源），命名后缀则随部署改名失效（生产环境工具名
    可与本地不同）。

    本模块把"证据通道"变成**由装配期工具面派生的环境无关事实**：

      · 类别定义集中在此（纯函数，不依赖任何 mock 包）；
      · 装配期由各 subagent 的工具集派生 ``{expert: [channel, ...]}``
        并冻结进台账（与 ``scene_experts`` 同法）；
      · 判据只比较**通道类别集合的差异**，不认识任何专家命名约定。

    类别刻意取粗粒度（按数据源语义），以容忍生产改名与工具增删：

    ============  ==================================================
    metrics       指标时序（Argus 家族，全部 ``*argus*`` 工具）
    k8s_events    K8s 事件流（``*event*``）
    k8s_logs      容器/组件日志（``*log*``）
    k8s_state     对象状态/描述/清单与共享组件状态（其余 k8s 面）
    host_os       主机 OS 层（``get_os_*``）
    gpu           GPU 层（``*gpu*``）
    topology      拓扑查询（``*topology*``）
    unknown       未识别工具（**不参与**等价判断，见 ``effective_channels``）
    ============  ==================================================

    语义依据：诊断推理中的"覆盖性"取决于**该观测通道能否取到该证据**
    （缺失证据只有在"搜索充分且具备检测能力"时才可作反证）；同一数据源
    的多个专家读取的是相关证据，不构成独立通道。
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Any

METRICS = "metrics"
K8S_EVENTS = "k8s_events"
K8S_LOGS = "k8s_logs"
K8S_STATE = "k8s_state"
HOST_OS = "host_os"
GPU = "gpu"
TOPOLOGY = "topology"
UNKNOWN = "unknown"

CHANNEL_LABELS: dict[str, str] = {
    METRICS: "指标时序",
    K8S_EVENTS: "K8s 事件流",
    K8S_LOGS: "容器/组件日志",
    K8S_STATE: "对象状态与清单",
    HOST_OS: "主机 OS 层",
    GPU: "GPU 层",
    TOPOLOGY: "拓扑查询",
    UNKNOWN: "未识别工具",
}

# 共享组件与 K8s 面的工具名前缀（语义比对后均落在 k8s_state 通道）。
_K8S_PREFIXES = ("get_k8s", "describe_k8s", "check_k8s", "list_k8s",
                 "explain_k8s", "get_pod_", "get_apigateway", "get_group1",
                 "get_ipam", "get_elb", "get_vpc")


def channels_of_tool(name: str) -> frozenset[str]:
    """单个工具名 → 证据通道类别（环境无关的纯函数）。

    匹配顺序（具体优先）：指标 → 拓扑 → GPU → 主机 → 事件 → 日志 →
    K8s 面/共享组件 → 未识别。容忍生产改名（按子串语义而非精确名）。
    """
    n = str(name or "").strip().lower()
    if not n:
        return frozenset({UNKNOWN})
    if "argus" in n:
        return frozenset({METRICS})
    if "topology" in n:
        return frozenset({TOPOLOGY})
    if "gpu" in n:
        return frozenset({GPU})
    if n.startswith("get_os_") or n.startswith("get_os") or n.startswith("query_os"):
        return frozenset({HOST_OS})
    if "event" in n:
        return frozenset({K8S_EVENTS})
    if "log" in n:
        return frozenset({K8S_LOGS})
    if n.startswith(_K8S_PREFIXES):
        return frozenset({K8S_STATE})
    return frozenset({UNKNOWN})


def _tool_name(tool: Any) -> str:
    """工具对象或字符串 → 工具名。"""
    return str(getattr(tool, "name", "") or (tool if isinstance(tool, str) else ""))


def channels_of_tools(tools: Iterable[Any]) -> frozenset[str]:
    """一组工具（对象或名）→ 通道类别集合。"""
    channels: set[str] = set()
    for tool in tools or ():
        channels |= channels_of_tool(_tool_name(tool))
    return frozenset(channels)


def effective_channels(channels: Iterable[str]) -> frozenset[str]:
    """剔除 ``unknown`` 后的有效通道（未识别工具不应参与等价判断）。"""
    return frozenset(c for c in (channels or ()) if c != UNKNOWN)


def is_metric_channel(channels: Iterable[str]) -> bool:
    """是否仅指标通道（监控层）——确证层判定用，替代命名后缀判定。"""
    effective = effective_channels(channels)
    return bool(effective) and effective <= {METRICS}


def channel_labels(channels: Iterable[str]) -> list[str]:
    """通道类别的中文标签（回执/日志可读）。"""
    return [CHANNEL_LABELS.get(c, c) for c in sorted(effective_channels(channels))]


def expert_channels_from_configs(
    configs: Sequence[dict[str, Any]],
) -> dict[str, list[str]]:
    """装配期派生 ``{专家名: [通道类别, ...]}``（冻结进台账的事实快照）。

    只登记"有效通道"非空的专家；无有效通道的专家不出现在快照中，
    判据侧按"未登记"处理（fail-open，不产生拦截）。
    """
    out: dict[str, list[str]] = {}
    for cfg in configs or ():
        name = str(cfg.get("name") or "")
        if not name:
            continue
        effective = effective_channels(channels_of_tools(cfg.get("tools") or ()))
        if effective:
            out[name] = sorted(effective)
    return out
