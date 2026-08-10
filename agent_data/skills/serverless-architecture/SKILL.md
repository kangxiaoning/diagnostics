---
name: serverless-architecture
description: Serverless 集群（k8s-on-k8s）静态架构与拓扑知识：KMC 控制面 / SCI 数据面分离、命名规则（控制面 ns `<k8s_name>-<master_id>`、工作负载 ns `burst-ns-<master_id>`、SCI Pod `burst-<ns>-<pod>`）、RelationGraph 视图（serverless/kmc/sci 三层 + 第 4 视图 etcd_views 共享 etcd；注入层派生 host_views/host_nodes）；诊断 Serverless 故障前先读本技能建立目标定位。
---

# Serverless 集群架构与拓扑（诊断知识）

> 静态架构知识：诊断 Serverless 集群前先理解本架构，再结合会话注入的
> RelationGraph 实例视图定位目标。v0.4.0 起共享组件（API Gateway/Group1-xxx/
> IPAM/coreDNS/vpc-cni）与每集群控制面（scheduler/virtualNode/vpc-cni-controller）
> 及共享 etcd 均纳入实例视图条目（场景故障目标必在拓扑中），本技能承载组件
> 角色与故障面分类，实例位置以会话注入的 RelationGraph 为准。

## 1. 总体定位（k8s-on-k8s 控制面/数据面分离）

| 集群/设施 | 角色 | 说明 |
|-----------|------|------|
| **Serverless 逻辑集群** | 面向用户的抽象 | 不运行任何容器；用户仅感知逻辑资源（Deployment/Service/ConfigMap…） |
| **KMC 物理集群** | 控制面承载 | 每逻辑集群控制面（apiserver/controller-manager/scheduler/virtualNode/vpc-cni-controller）以 Deployment 形态集中在一个命名空间 `<k8s_name>-<master_id>`（virtualNode 为基于 virtual-kubelet 自研的虚拟节点 agent）；另承载共享组件（API Gateway、Group1-xxx、IPAM、coreDNS） |
| **SCI 物理集群** | 工作负载执行 | 仅运行 Pod（无 Deployment 等控制器）；每节点部署自研 VPC-CNI 插件为 Pod 分配固定 IP |
| **etcd 集群（独立物理机 5 节点）** | 统一状态存储 | 所有逻辑集群共享；控制面组件经 etcd-proxy（sidecar）访问，key 前缀隔离 |

**核心结论**：多逻辑集群共享 KMC 控制面资源、共享 etcd、共享 SCI —— 共享组件故障影响面大（多集群同时异常）；单集群故障多为该集群控制面组件或租户工作负载问题。

## 2. 交付与命名规则

Serverless 由 KMC 交付，基于名为 `k8s` 的 CRD 创建，交付产生 `k8s_name`（`k8s` 资源的 `name`）与 `master_id`：

| 资源 | 命名规则 |
|------|---------|
| KMC 中控制面 namespace | `<k8s_name>-<master_id>`（如 `cluster-szg20251118-d8690ec9-sj4u0gkj2f`） |
| SCI 中工作负载 namespace | `burst-ns-<master_id>`（如 `burst-ns-sj4u0gkj2f`） |
| SCI 中工作负载 Pod 名 | `burst-<serverless_namespace>-<serverless_pod_name>`（如 `burst-property-insurance-individual-namespace-deploy-2062722397220888577-59f9489f6d-797x2`） |

控制面 Pod 与 KMC/SCI 自身 static Pod（`kube-system`）无此前缀规则。`k8s_name`/`master_id` 由会话注入的拓扑映射（`mapping`）提供。

## 3. 共享组件与网络

| 组件 | 位置 | 职责 |
|------|------|------|
| API Gateway | KMC `kube-system` | SCI API 认证代理，供 virtualNode 调用 |
| Group1-xxx | KMC `kmc-ingress`（DaemonSet） | 与 SCI Pod 的双向隧道，转发 logs/exec/metrics（不依赖 Pod IP 直连） |
| IPAM Controller | KMC `kmc-system` | 管理 IP 池，分配固定 IP，与 SCI CNI 协同 |
| coreDNS | 逻辑集群 KMC 控制面命名空间；KMC/SCI 自身 `kube-system` | 逻辑集群 DNS 服务（Service 解析）；各物理集群自身 DNS |
| vpc-cni-controller | KMC 控制面命名空间 | 每逻辑集群的 VPC-CNI 控制面，与 SCI 节点 CNI 插件及 IPAM 协同维护固定 IP |
| 自研 VPC-CNI | SCI `kube-system`（DaemonSet） | 为 SCI 节点 Pod 分配固定 IP、配置网络 |

网络要点：无 NetworkPolicy；不同集群/租户分配不重叠 IP 池实现地址空间隔离；固定 IP 策略支持 Pod 重建后 IP 保留（基于 owner/UID 复用）。

## 4. 故障面 → 负责专家 → 排查入口

| 故障面 | 典型症状 | 排查 |
|--------|---------|------|
| ① 逻辑集群控制面组件 | API 超时/拒绝、协调停滞、固定 IP 分配异常 | KMC 控制面命名空间（`<k8s_name>-<master_id>`）Deployment/Pod（含 vpc-cni-controller）、etcd-proxy sidecar |
| ② 共享 etcd | 多集群同时异常、写超时、watch 中断 | 5 节点 etcd 健康/leader/延迟 |
| ③ 共享辅助组件 | virtualNode→SCI 断（API Gateway）、logs/exec 失败（Group1-xxx）、新 Pod 无 IP（IPAM） | `kube-system`/`kmc-ingress`/`kmc-system` 组件状态 |
| ④ SCI 工作负载 + VPC-CNI | Pod Pending/ContainerCreating、IP 冲突、网络不通、频繁重启 | SCI 节点 CNI、Pod 固定 IP、IPAM 绑定记录、KMC 侧 vpc-cni-controller |
| ⑤ virtualNode 同步链路 | 逻辑状态 ≠ 物理状态 | 对比逻辑集群视图与 SCI 实际 Pod 状态 |
| ⑥ 物理节点 | 节点资源压力、NotReady | 节点 CPU/内存/磁盘/网络（host_name） |

## 5. 症状 → 排查路径速查

| 症状 | 排查路径 |
|------|---------|
| Pod 一直 Pending / ContainerCreating | virtualNode 正常？→ API Gateway 可达？→ vpc-cni-controller / IPAM 分配 IP？→ SCI 节点 CNI 运行？ |
| `kubectl logs` 失败但 Pod Running | Group1-xxx 隧道（KMC `kmc-ingress` → SCI 节点） |
| 单集群 API 超时 | 该集群 apiserver/etcd-proxy（KMC 控制面命名空间） |
| 多集群同时异常 | 共享 etcd / KMC 节点资源 / 共享组件（API Gateway、IPAM） |
| 网络不通 / IP 冲突 | VPC-CNI 与 IPAM 固定 IP 记录；KMC 侧 vpc-cni-controller 状态 |
