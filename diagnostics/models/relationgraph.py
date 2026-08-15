"""RelationGraph topology data models (design document, code reference).

The RelationGraph is the instance-level topology returned by the environment
topology query: ClusterMapping + three layer views (serverless / kmc / sci).
It is a read-only fact injected into the diagnosis context; the state machine
never stores it as ledger state.

This module is the single source of truth for the model classes, referenced by
the mock topology implementation and available to real deployments (contract
validation) after migrating the query implementation.
"""

from __future__ import annotations

from enum import Enum
from typing import List, Optional

from pydantic import BaseModel, Field


class ClusterType(str, Enum):
    """物理集群类型枚举"""
    KMC = "KMC"
    SCI = "SCI"


class ObjectRef(BaseModel):
    """通用资源引用（逻辑资源：控制面虚拟组件或用户资源）"""
    kind: str
    namespace: str
    name: str


class PodNodeRef(BaseModel):
    """物理 Pod 引用（含节点 IP 与主机名）"""
    namespace: str
    name: str
    node_name: str          # 节点 IP（HostIP，来自 K8s；KMC/SCI 节点为 30 网段）
    host_name: str = "placeholder_host_name"
    pod_id: Optional[str] = None   # Serverless Pod 固定 IP（10 网段，用户可见；非 Pod 级资源为 None）


class PhysicalResourceRef(BaseModel):
    """KMC 物理资源引用（Deployment 或 Pod）"""
    kind: str
    namespace: str
    name: str
    node_name: Optional[str] = None
    host_name: Optional[str] = None


class ClusterMapping(BaseModel):
    """集群映射 + 监控名 + 交付元数据（k8s_name/master_id 源自 k8s CRD）"""
    serverless_cluster: str
    kmc_cluster: str
    sci_cluster: str
    serverless_monitor_name: str = "placeholder_serverless_monitor"
    kmc_monitor_name: str = "placeholder_kmc_monitor"
    sci_monitor_name: str = "placeholder_sci_monitor"
    k8s_name: str = "placeholder_k8s_name"
    master_id: str = "placeholder_master_id"


class ServerlessResourceViewItem(BaseModel):
    """第1层：Serverless 资源视角"""
    resource: ObjectRef
    physical_cluster_type: ClusterType
    physical_pods: List[PodNodeRef] = Field(default_factory=list)
    is_control_plane: bool = False
    control_plane_role: Optional[str] = None


class KMCResourceViewItem(BaseModel):
    """第2层：KMC 资源视角"""
    kmc_resource: PhysicalResourceRef
    owner_kmc_deployment: Optional[ObjectRef] = None
    serverless_resource: Optional[ObjectRef] = None
    is_kmc_control_plane: bool = False
    kmc_control_plane_role: Optional[str] = None


class SCIResourceViewItem(BaseModel):
    """第3层：SCI 资源视角"""
    sci_pod: PodNodeRef
    serverless_resource: Optional[ObjectRef] = None
    is_sci_control_plane: bool = False
    sci_control_plane_role: Optional[str] = None


class EtcdMemberRef(BaseModel):
    """共享 etcd member 引用（systemd 主机服务，无 K8s namespace/pod 概念）。

    标识 = ETCD_NAME + 节点 IP + 物理机名；etcd 工具以 host_name 为参数，
    配置（ETCD_DATA_DIR/ETCD_LISTEN_*）与日志（journalctl）由工具内部解析
    （design document §6A.2：拓扑只给身份，深度解析下沉工具层）。
    """
    name: str           # ETCD_NAME（Raft member 标识）
    node_name: str      # 节点 IP（peer/client 监听地址所在）
    host_name: str      # 物理机名（systemd 单元所在主机；etcd tools 主参数）


class EtcdMemberViewItem(BaseModel):
    """第4层：共享 etcd 视图（systemd 二进制主机服务，无 namespace/pod 概念）"""
    etcd_member: EtcdMemberRef  # name=ETCD_NAME, node_name=节点 IP, host_name=物理机
    cluster: str = "shared_etcd"     # 跨集群共享设施标记
    roles: List[str] = Field(default_factory=list)  # e.g. ["shared_etcd"]


class RelationGraph(BaseModel):
    """三层关联统一容器（+ etcd_views 第 4 视图：共享 etcd）"""
    mapping: ClusterMapping
    serverless_views: List[ServerlessResourceViewItem] = Field(default_factory=list)
    kmc_views: List[KMCResourceViewItem] = Field(default_factory=list)
    sci_views: List[SCIResourceViewItem] = Field(default_factory=list)
    etcd_views: List[EtcdMemberViewItem] = Field(default_factory=list)
