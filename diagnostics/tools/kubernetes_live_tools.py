"""Live Kubernetes diagnostic tools — execute kubectl commands against real clusters."""

from __future__ import annotations

import json
import logging
import os
import subprocess

from langchain_core.tools import tool

logger = logging.getLogger(__name__)

# ── Cluster server mapping ──────────────────────────────────────────
# Format: DIAGNOSTICS_K8S_SERVERS="prod-cluster=https://1.2.3.4:6443,staging=https://5.6.7.8:6443"
_cluster_servers: dict[str, str] = {}


def _load_cluster_servers() -> dict[str, str]:
    """Parse DIAGNOSTICS_K8S_SERVERS env var into {cluster_name: apiserver_url} dict."""
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
    """Get the kube-apiserver URL for a given cluster."""
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
    """Run a kubectl command against the specified cluster's API server.

    All commands use --server for direct API server access (no kubeconfig required).
    """
    server_url = _get_apiserver_url(cluster_name)
    cmd = [
        "kubectl",
        "--server", server_url,
        "--insecure-skip-tls-verify",
        *args,
    ]
    logger.info("[cluster=%s] kubectl %s", cluster_name, " ".join(args))
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            input=input_text,
            env={**os.environ, "KUBECONFIG": "/dev/null"},  # force --server usage
        )
        if result.returncode != 0:
            return f"[kubectl error (exit {result.returncode})]\n{result.stderr.strip()}"
        return result.stdout.strip()
    except subprocess.TimeoutExpired:
        return f"[kubectl timed out after {timeout}s]"
    except FileNotFoundError:
        return "[kubectl not found on PATH — install kubectl to enable live cluster diagnostics]"


# ═══════════════ Public tools ═══════════════


@tool
def get_cluster_overview(cluster_name: str) -> str:
    """Get high-level overview of a Kubernetes cluster: nodes, pods, namespaces.

    Args:
        cluster_name: cluster name (e.g., "prod-cluster", "staging").
                      Must be configured in DIAGNOSTICS_K8S_SERVERS env var.
    """
    nodes = _kubectl(cluster_name, ["get", "nodes", "-o", "wide"])
    pods = _kubectl(cluster_name, ["get", "pods", "--all-namespaces",
                                     "--field-selector=status.phase!=Running,status.phase!=Succeeded",
                                     "-o", "wide"])
    ns_count = _kubectl(cluster_name, ["get", "namespaces", "--no-headers"])
    parts = [
        f"## Cluster: {cluster_name}",
        f"### Nodes\n{nodes}",
        f"### Non-Running Pods (all namespaces)\n{pods or '(all pods Running or Succeeded)'}",
        f"### Namespace count\n{len(ns_count.splitlines()) if ns_count else 0}",
    ]
    return "\n\n".join(parts)


@tool
def get_pod_logs(cluster_name: str, namespace: str,
                 pod_name: str, tail_lines: int = 200) -> str:
    """Retrieve recent logs from a specific pod.

    Args:
        cluster_name: cluster name configured in DIAGNOSTICS_K8S_SERVERS.
        namespace: Kubernetes namespace of the pod.
        pod_name: pod name.
        tail_lines: number of recent log lines to fetch (default 200).
    """
    return _kubectl(cluster_name, [
        "logs", pod_name, "-n", namespace,
        f"--tail={tail_lines}", "--timestamps",
    ], timeout=20)


@tool
def describe_pod(cluster_name: str, namespace: str, pod_name: str) -> str:
    """Get full describe output for a pod — events, conditions, container statuses, volumes.

    Args:
        cluster_name: cluster name configured in DIAGNOSTICS_K8S_SERVERS.
        namespace: Kubernetes namespace of the pod.
        pod_name: pod name.
    """
    return _kubectl(cluster_name, [
        "describe", "pod", pod_name, "-n", namespace,
    ], timeout=15)


@tool
def get_pod_events(cluster_name: str, namespace: str, pod_name: str) -> str:
    """Get Kubernetes events related to a specific pod.

    Args:
        cluster_name: cluster name configured in DIAGNOSTICS_K8S_SERVERS.
        namespace: Kubernetes namespace of the pod.
        pod_name: pod name.
    """
    return _kubectl(cluster_name, [
        "get", "events", "-n", namespace,
        "--field-selector", f"involvedObject.name={pod_name}",
        "--sort-by=.lastTimestamp",
    ], timeout=15)


@tool
def get_node_info(cluster_name: str, node_name: str) -> str:
    """Get detailed information about a Kubernetes node — conditions, capacity, allocatable, system info.

    Args:
        cluster_name: cluster name configured in DIAGNOSTICS_K8S_SERVERS.
        node_name: node name.
    """
    return _kubectl(cluster_name, [
        "describe", "node", node_name,
    ], timeout=15)


@tool
def get_cluster_events(cluster_name: str, namespace: str = "") -> str:
    """Get recent cluster-wide or namespace-scoped warning events.

    Args:
        cluster_name: cluster name configured in DIAGNOSTICS_K8S_SERVERS.
        namespace: optional namespace filter. If empty, returns events across all namespaces.
    """
    args = ["get", "events", "--sort-by=.lastTimestamp"]
    if namespace:
        args.extend(["-n", namespace])
    else:
        args.append("--all-namespaces")
    raw = _kubectl(cluster_name, args, timeout=15)
    # Return only the last 30 lines (most recent events)
    lines = raw.splitlines()
    if len(lines) > 31:  # header + 30 events
        header = lines[0]
        recent = lines[-30:]
        return header + "\n" + "\n".join(recent) + f"\n... (showing last 30 of {len(lines)-1} events)"
    return raw


@tool
def get_pod_resource_usage(cluster_name: str, namespace: str = "") -> str:
    """Get resource usage (CPU/Memory) of pods via kubectl top (requires metrics-server).

    Args:
        cluster_name: cluster name configured in DIAGNOSTICS_K8S_SERVERS.
        namespace: optional namespace filter. If empty, returns across all namespaces.
    """
    args = ["top", "pods"]
    if namespace:
        args.extend(["-n", namespace])
    else:
        args.append("--all-namespaces")
    return _kubectl(cluster_name, args, timeout=20)


@tool
def get_node_resource_usage(cluster_name: str) -> str:
    """Get resource usage (CPU/Memory) of all nodes via kubectl top (requires metrics-server).

    Args:
        cluster_name: cluster name configured in DIAGNOSTICS_K8S_SERVERS.
    """
    return _kubectl(cluster_name, ["top", "nodes"], timeout=20)


@tool
def get_api_resources(cluster_name: str) -> str:
    """List all API resources available on the cluster (CRDs, native resources).

    Args:
        cluster_name: cluster name configured in DIAGNOSTICS_K8S_SERVERS.
    """
    return _kubectl(cluster_name, ["api-resources", "--sort-by=name"], timeout=15)


@tool
def get_api_versions(cluster_name: str) -> str:
    """List all API group versions supported by the cluster.

    Args:
        cluster_name: cluster name configured in DIAGNOSTICS_K8S_SERVERS.
    """
    return _kubectl(cluster_name, ["api-versions"], timeout=15)


@tool
def get_resource_yaml(cluster_name: str, resource_type: str,
                      resource_name: str, namespace: str = "") -> str:
    """Get the full YAML manifest of any Kubernetes resource.

    Args:
        cluster_name: cluster name configured in DIAGNOSTICS_K8S_SERVERS.
        resource_type: resource type (e.g., deploy, svc, elbsvc, daemonset, configmap).
        resource_name: resource name (e.g., vpc-cni, my-service).
        namespace: Kubernetes namespace. Leave empty for cluster-scoped resources.
    """
    args = ["get", resource_type, resource_name, "-o", "yaml"]
    if namespace:
        args.extend(["-n", namespace])
    return _kubectl(cluster_name, args, timeout=15)


@tool
def get_pod_logs_since(cluster_name: str, namespace: str,
                       pod_name: str, minutes: int = 5) -> str:
    """Retrieve pod logs from the last N minutes.

    Args:
        cluster_name: cluster name configured in DIAGNOSTICS_K8S_SERVERS.
        namespace: Kubernetes namespace of the pod.
        pod_name: pod name.
        minutes: fetch logs from the last N minutes (default 5).
    """
    return _kubectl(cluster_name, [
        "logs", pod_name, "-n", namespace,
        f"--since={minutes}m", "--timestamps",
    ], timeout=20)


@tool
def get_pod_logs_lines(cluster_name: str, namespace: str,
                       pod_name: str, head_lines: int = 50) -> str:
    """Retrieve the first N lines of pod logs.

    Args:
        cluster_name: cluster name configured in DIAGNOSTICS_K8S_SERVERS.
        namespace: Kubernetes namespace of the pod.
        pod_name: pod name.
        head_lines: number of lines to fetch from the beginning of the log (default 50).
    """
    return _kubectl(cluster_name, [
        "logs", pod_name, "-n", namespace,
        "--tail", str(head_lines),
    ], timeout=20)


@tool
def explain_resource(cluster_name: str, resource_path: str,
                     recursive: bool = False) -> str:
    """Get documentation and field descriptions for a Kubernetes resource via kubectl explain.

    Args:
        cluster_name: cluster name configured in DIAGNOSTICS_K8S_SERVERS.
        resource_path: resource path (e.g., pods.spec.containers, deployment.spec.template).
        recursive: if True, show full field tree recursively.
    """
    args = ["explain", resource_path]
    if recursive:
        args.append("--recursive")
    return _kubectl(cluster_name, args, timeout=15)
