"""Live Kubernetes diagnostic tools — execute kubectl commands against real clusters."""

from __future__ import annotations

import logging
import os
import subprocess

from langchain_core.tools import tool

logger = logging.getLogger(__name__)

# ── Cluster server mapping ──────────────────────────────────────────
# Format: DIAGNOSTICS_K8S_SERVERS="prod-cluster=https://1.2.3.4:6443,staging=https://5.6.7.8:6443"
_cluster_servers: dict[str, str] = {}


def _load_cluster_servers() -> dict[str, str]:
    global _cluster_servers
    if _cluster_servers:
        return _cluster_servers
    raw = os.getenv("DIAGNOSTICS_K8S_SERVERS", "")
    if not raw:
        return {}
    for pair in raw.split(","):
        pair = pair.strip()
        if "=" in pair:
            name, url = pair.split("=", 1)
            _cluster_servers[name.strip()] = url.strip().rstrip("/")
    logger.info("Loaded %d K8s cluster server(s): %s",
                len(_cluster_servers), list(_cluster_servers.keys()))
    return _cluster_servers


def _get_apiserver_url(cluster_name: str) -> str:
    servers = _load_cluster_servers()
    url = servers.get(cluster_name)
    if not url:
        raise ValueError(
            f"Unknown cluster '{cluster_name}'. "
            f"Known clusters: {list(servers.keys())}. "
            "Set DIAGNOSTICS_K8S_SERVERS env var."
        )
    return url


def _kubectl(cluster_name: str, args: list[str],
             timeout: int = 30, input_text: str | None = None) -> str:
    server_url = _get_apiserver_url(cluster_name)
    cmd = [
        "kubectl", "--server", server_url,
        "--insecure-skip-tls-verify", *args,
    ]
    logger.info("[cluster=%s] kubectl %s", cluster_name, " ".join(args))
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout,
            input=input_text,
            env={**os.environ, "KUBECONFIG": "/dev/null"},
        )
        if result.returncode != 0:
            return f"[kubectl error (exit {result.returncode})]\n{result.stderr.strip()}"
        return result.stdout.strip()
    except subprocess.TimeoutExpired:
        return f"[kubectl timed out after {timeout}s]"
    except FileNotFoundError:
        return "[kubectl not found on PATH]"


# ═══════════════ Discovery ═══════════════

@tool
def list_clusters() -> str:
    """List all configured Kubernetes cluster names. Call this FIRST."""
    servers = _load_cluster_servers()
    if not servers:
        return "No clusters configured. Set DIAGNOSTICS_K8S_SERVERS env var."
    return "\n".join(f"- `{n}` → {u}" for n, u in servers.items())


@tool
def get_namespaces(cluster_name: str) -> str:
    """List all namespaces in a cluster. Call before tools that need namespace."""
    return _kubectl(cluster_name, ["get", "namespaces", "-o", "wide"], timeout=10)


# ═══════════════ Overview ═══════════════

@tool
def get_cluster_overview(cluster_name: str) -> str:
    """Get high-level cluster overview: nodes, non-running pods, namespace count."""
    nodes = _kubectl(cluster_name, ["get", "nodes", "-o", "wide"])
    pods = _kubectl(cluster_name, ["get", "pods", "--all-namespaces",
                     "--field-selector=status.phase!=Running,status.phase!=Succeeded",
                     "-o", "wide"])
    ns_count = _kubectl(cluster_name, ["get", "namespaces", "--no-headers"])
    parts = [
        f"## Cluster: {cluster_name}",
        f"### Nodes\n{nodes}",
        f"### Non-Running Pods\n{pods or '(all Running or Succeeded)'}",
        f"### Namespace count\n{len(ns_count.splitlines()) if ns_count else 0}",
    ]
    return "\n\n".join(parts)


# ═══════════════ Pod Tools ═══════════════

@tool
def get_pod_logs(cluster_name: str, namespace: str,
                 pod_name: str, tail_lines: int = 200) -> str:
    """Retrieve recent pod logs."""
    return _kubectl(cluster_name, [
        "logs", pod_name, "-n", namespace,
        f"--tail={tail_lines}", "--timestamps",
    ], timeout=20)


@tool
def get_pod_logs_since(cluster_name: str, namespace: str,
                       pod_name: str, minutes: int = 5) -> str:
    """Retrieve pod logs from the last N minutes."""
    return _kubectl(cluster_name, [
        "logs", pod_name, "-n", namespace,
        f"--since={minutes}m", "--timestamps",
    ], timeout=20)


@tool
def get_pod_logs_lines(cluster_name: str, namespace: str,
                       pod_name: str, head_lines: int = 50) -> str:
    """Retrieve the last N lines of pod logs."""
    return _kubectl(cluster_name, [
        "logs", pod_name, "-n", namespace,
        "--tail", str(head_lines),
    ], timeout=20)


@tool
def get_pod_previous_logs(cluster_name: str, namespace: str,
                          pod_name: str, tail_lines: int = 200) -> str:
    """Retrieve logs from the PREVIOUS (crashed) container instance."""
    return _kubectl(cluster_name, [
        "logs", pod_name, "-n", namespace,
        "--previous", f"--tail={tail_lines}", "--timestamps",
    ], timeout=20)


@tool
def describe_pod(cluster_name: str, namespace: str, pod_name: str) -> str:
    """Get full describe output for a pod."""
    return _kubectl(cluster_name, [
        "describe", "pod", pod_name, "-n", namespace,
    ], timeout=15)


@tool
def get_pod_events(cluster_name: str, namespace: str, pod_name: str) -> str:
    """Get events related to a specific pod."""
    return _kubectl(cluster_name, [
        "get", "events", "-n", namespace,
        "--field-selector", f"involvedObject.name={pod_name}",
        "--sort-by=.lastTimestamp",
    ], timeout=15)


@tool
def get_cluster_events(cluster_name: str, namespace: str = "") -> str:
    """Get recent cluster-wide or namespace-scoped events (last 30)."""
    args = ["get", "events", "--sort-by=.lastTimestamp"]
    if namespace:
        args.extend(["-n", namespace])
    else:
        args.append("--all-namespaces")
    raw = _kubectl(cluster_name, args, timeout=15)
    lines = raw.splitlines()
    if len(lines) > 31:
        return lines[0] + "\n" + "\n".join(lines[-30:]) + f"\n... (last 30 of {len(lines)-1})"
    return raw


# ═══════════════ Node Tools ═══════════════

@tool
def get_node_info(cluster_name: str, node_name: str) -> str:
    """Get detailed node information."""
    return _kubectl(cluster_name, ["describe", "node", node_name], timeout=15)


# ═══════════════ Resource Usage ═══════════════

@tool
def get_pod_resource_usage(cluster_name: str, namespace: str = "") -> str:
    """Get pod CPU/Memory usage (requires metrics-server)."""
    args = ["top", "pods"]
    if namespace:
        args.extend(["-n", namespace])
    else:
        args.append("--all-namespaces")
    return _kubectl(cluster_name, args, timeout=20)


@tool
def get_node_resource_usage(cluster_name: str) -> str:
    """Get node CPU/Memory usage (requires metrics-server)."""
    return _kubectl(cluster_name, ["top", "nodes"], timeout=20)


# ═══════════════ API & Resources ═══════════════

@tool
def get_api_resources(cluster_name: str) -> str:
    """List all API resources on the cluster (incl. CRDs)."""
    return _kubectl(cluster_name, ["api-resources", "--sort-by=name"], timeout=15)


@tool
def get_api_versions(cluster_name: str) -> str:
    """List all API group versions supported by the cluster."""
    return _kubectl(cluster_name, ["api-versions"], timeout=15)


@tool
def get_resource_yaml(cluster_name: str, resource_type: str,
                      resource_name: str, namespace: str = "") -> str:
    """Get full YAML of any resource (e.g. get_resource_yaml('prod','deploy','vpc-cni','kube-system'))."""
    args = ["get", resource_type, resource_name, "-o", "yaml"]
    if namespace:
        args.extend(["-n", namespace])
    return _kubectl(cluster_name, args, timeout=15)


@tool
def explain_resource(cluster_name: str, resource_path: str,
                     recursive: bool = False) -> str:
    """Get field documentation via kubectl explain."""
    args = ["explain", resource_path]
    if recursive:
        args.append("--recursive")
    return _kubectl(cluster_name, args, timeout=15)


# ═══════════════ System Pods ═══════════════

@tool
def get_system_pods(cluster_name: str) -> str:
    """Get all system pods in kube-system namespace."""
    return _kubectl(cluster_name, ["get", "pods", "-n", "kube-system", "-o", "wide"], timeout=15)


# ═══════════════ Controllers ═══════════════

@tool
def describe_controller(cluster_name: str, resource_type: str,
                        resource_name: str, namespace: str = "") -> str:
    """Describe a controller (deployment, statefulset, daemonset, replicaset).
    Use full resource_type names: deployment not deploy, daemonset not ds.
    """
    args = ["describe", resource_type, resource_name]
    if namespace:
        args.extend(["-n", namespace])
    return _kubectl(cluster_name, args, timeout=15)


# ═══════════════ Services ═══════════════

@tool
def check_service_endpoints(cluster_name: str, service_name: str,
                            namespace: str = "default") -> str:
    """Check whether a Service has healthy endpoints."""
    svc = _kubectl(cluster_name, ["get", "svc", service_name, "-n", namespace, "-o", "wide"], timeout=10)
    ep = _kubectl(cluster_name, ["get", "endpointslices", "-n", namespace,
                   "-l", f"kubernetes.io/service-name={service_name}"], timeout=10)
    return f"## Service\n{svc}\n\n## EndpointSlices\n{ep}"


# ═══════════════ Config & Resources ═══════════════

@tool
def get_configmap(cluster_name: str, configmap_name: str,
                  namespace: str = "default") -> str:
    """Get ConfigMap content."""
    return _kubectl(cluster_name, ["get", "configmap", configmap_name, "-n", namespace, "-o", "yaml"], timeout=10)


@tool
def list_namespace_resources(cluster_name: str, namespace: str = "default") -> str:
    """List all resources in a namespace."""
    return _kubectl(cluster_name, ["get", "all", "-n", namespace, "-o", "wide"], timeout=15)


@tool
def get_pv_pvc_status(cluster_name: str, namespace: str = "") -> str:
    """Get PV/PVC status."""
    pv = _kubectl(cluster_name, ["get", "pv", "-o", "wide"], timeout=10)
    pvc_args = ["get", "pvc"] + (["-n", namespace] if namespace else ["--all-namespaces"])
    pvc = _kubectl(cluster_name, pvc_args, timeout=10)
    return f"## PersistentVolumes\n{pv}\n\n## PersistentVolumeClaims\n{pvc}"


@tool
def get_ingress_status(cluster_name: str, namespace: str = "") -> str:
    """Get Ingress resources and backend status."""
    args = ["get", "ingress", "-o", "wide"]
    if namespace:
        args.extend(["-n", namespace])
    else:
        args.append("--all-namespaces")
    return _kubectl(cluster_name, args, timeout=10)


# ═══════════════ CoreDNS ═══════════════

@tool
def get_coredns_logs(cluster_name: str, tail_lines: int = 200,
                     since_minutes: int = 0) -> str:
    """Get logs from all CoreDNS pods in kube-system.

    CoreDNS runs as a deployment in kube-system with label k8s-app=kube-dns.
    Use this when DNS resolution failures are suspected.
    """
    pods_raw = _kubectl(cluster_name, [
        "get", "pods", "-n", "kube-system",
        "-l", "k8s-app=kube-dns", "-o", "name",
    ], timeout=10)
    if not pods_raw or "[kubectl error" in pods_raw:
        pods_raw = _kubectl(cluster_name, [
            "get", "pods", "-n", "kube-system",
            "-l", "app=coredns", "-o", "name",
        ], timeout=10)
    if not pods_raw or "[kubectl error" in pods_raw:
        return "[CoreDNS pods not found in kube-system]"

    pod_names = [p.strip().replace("pod/", "") for p in pods_raw.splitlines() if p.strip()]
    parts = []
    for pn in pod_names:
        if since_minutes > 0:
            log = _kubectl(cluster_name, [
                "logs", pn, "-n", "kube-system",
                f"--since={since_minutes}m", "--timestamps",
            ], timeout=20)
        else:
            log = _kubectl(cluster_name, [
                "logs", pn, "-n", "kube-system",
                f"--tail={tail_lines}", "--timestamps",
            ], timeout=20)
        parts.append(f"## {pn}\n{log}")

    deploy = _kubectl(cluster_name, [
        "get", "deployment", "coredns", "-n", "kube-system", "-o", "wide",
    ], timeout=10)
    parts.insert(0, f"## CoreDNS Deployment\n{deploy}")
    return "\n\n".join(parts)


@tool
def describe_coredns(cluster_name: str) -> str:
    """Describe CoreDNS deployment and show Corefile ConfigMap in kube-system."""
    deploy = _kubectl(cluster_name, [
        "describe", "deployment", "coredns", "-n", "kube-system",
    ], timeout=15)
    cm = _kubectl(cluster_name, [
        "get", "configmap", "coredns", "-n", "kube-system", "-o", "yaml",
    ], timeout=10)
    svc = _kubectl(cluster_name, [
        "get", "svc", "kube-dns", "-n", "kube-system", "-o", "wide",
    ], timeout=10)
    return (
        f"## CoreDNS Deployment\n{deploy}\n\n"
        f"## kube-dns Service\n{svc}\n\n"
        f"## Corefile ConfigMap\n{cm}"
    )
