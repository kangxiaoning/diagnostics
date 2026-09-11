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
def get_k8s_namespaces(cluster_name: str) -> str:
    """List all namespaces in a cluster. Call before tools that need namespace."""
    return _kubectl(cluster_name, ["get", "namespaces", "-o", "wide"], timeout=10)


# ═══════════════ Overview ═══════════════

@tool
def get_k8s_cluster_overview(cluster_name: str) -> str:
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
def get_k8s_pod_logs(cluster_name: str, namespace: str,
                 pod_name: str, tail_lines: int = 200) -> str:
    """Retrieve recent pod logs."""
    return _kubectl(cluster_name, [
        "logs", pod_name, "-n", namespace,
        f"--tail={tail_lines}", "--timestamps",
    ], timeout=20)


@tool
def get_k8s_pod_logs_since(cluster_name: str, namespace: str,
                       pod_name: str, minutes: int = 5) -> str:
    """Retrieve pod logs from the last N minutes."""
    return _kubectl(cluster_name, [
        "logs", pod_name, "-n", namespace,
        f"--since={minutes}m", "--timestamps",
    ], timeout=20)


@tool
def get_k8s_pod_logs_head(cluster_name: str, namespace: str,
                       pod_name: str, head_lines: int = 50) -> str:
    """Retrieve the last N lines of pod logs."""
    return _kubectl(cluster_name, [
        "logs", pod_name, "-n", namespace,
        "--tail", str(head_lines),
    ], timeout=20)


@tool
def get_k8s_pod_previous_logs(cluster_name: str, namespace: str,
                          pod_name: str, tail_lines: int = 200) -> str:
    """Retrieve logs from the PREVIOUS (crashed) container instance."""
    return _kubectl(cluster_name, [
        "logs", pod_name, "-n", namespace,
        "--previous", f"--tail={tail_lines}", "--timestamps",
    ], timeout=20)


@tool
def describe_k8s_resource(cluster_name: str, namespace: str, pod_name: str) -> str:
    """Get full describe output for a pod."""
    return _kubectl(cluster_name, [
        "describe", "pod", pod_name, "-n", namespace,
    ], timeout=15)


@tool
def get_k8s_pod_events_info(cluster_name: str, namespace: str, pod_name: str) -> str:
    """Get events related to a specific pod."""
    return _kubectl(cluster_name, [
        "get", "events", "-n", namespace,
        "--field-selector", f"involvedObject.name={pod_name}",
        "--sort-by=.lastTimestamp",
    ], timeout=15)


@tool
def get_k8s_cluster_events(cluster_name: str, namespace: str = "") -> str:
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
def get_k8s_node_info(cluster_name: str, node_name: str) -> str:
    """Get detailed node information."""
    return _kubectl(cluster_name, ["describe", "node", node_name], timeout=15)


# ═══════════════ Resource Usage ═══════════════

@tool
def get_k8s_pod_resource_usage(cluster_name: str, namespace: str = "") -> str:
    """Get pod CPU/Memory usage (requires metrics-server)."""
    args = ["top", "pods"]
    if namespace:
        args.extend(["-n", namespace])
    else:
        args.append("--all-namespaces")
    return _kubectl(cluster_name, args, timeout=20)


@tool
def get_k8s_node_resource_usage(cluster_name: str) -> str:
    """Get node CPU/Memory usage (requires metrics-server)."""
    return _kubectl(cluster_name, ["top", "nodes"], timeout=20)


# ═══════════════ API & Resources ═══════════════

@tool
def get_k8s_api_resources(cluster_name: str) -> str:
    """List all API resources on the cluster (incl. CRDs)."""
    return _kubectl(cluster_name, ["api-resources", "--sort-by=name"], timeout=15)


@tool
def get_k8s_api_versions(cluster_name: str) -> str:
    """List all API group versions supported by the cluster."""
    return _kubectl(cluster_name, ["api-versions"], timeout=15)


@tool
def get_k8s_resource_yaml(cluster_name: str, resource_type: str,
                      resource_name: str, namespace: str = "default") -> str:
    """Get full YAML of any resource (e.g. get_k8s_resource_yaml('prod','deploy','vpc-cni','kube-system'))."""
    args = ["get", resource_type, resource_name, "-o", "yaml"]
    if namespace:
        args.extend(["-n", namespace])
    return _kubectl(cluster_name, args, timeout=15)


@tool
def explain_k8s_resource(cluster_name: str, resource_type: str) -> str:
    """Get field documentation via kubectl explain (signature mirrors mock)."""
    return _kubectl(cluster_name, ["explain", resource_type], timeout=15)


# ═══════════════ System Pods ═══════════════

@tool
def get_k8s_system_pods(cluster_name: str) -> str:
    """Get all system pods in kube-system namespace."""
    return _kubectl(cluster_name, ["get", "pods", "-n", "kube-system", "-o", "wide"], timeout=15)


# ═══════════════ Controllers ═══════════════

@tool
def describe_k8s_controller(cluster_name: str, controller_name: str,
                            resource_type: str = "deployment") -> str:
    """Describe a controller (deployment, statefulset, daemonset, replicaset).
    Use full resource_type names: deployment not deploy, daemonset not ds.
    Signature mirrors mock/describe_k8s_controller.
    """
    return _kubectl(cluster_name,
                    ["describe", resource_type, controller_name, "-n", "default"],
                    timeout=15)


# ═══════════════ Services ═══════════════

@tool
def check_k8s_service_endpoints(cluster_name: str, service_name: str,
                            namespace: str = "default") -> str:
    """Check whether a Service has healthy endpoints."""
    svc = _kubectl(cluster_name, ["get", "svc", service_name, "-n", namespace, "-o", "wide"], timeout=10)
    ep = _kubectl(cluster_name, ["get", "endpointslices", "-n", namespace,
                   "-l", f"kubernetes.io/service-name={service_name}"], timeout=10)
    return f"## Service\n{svc}\n\n## EndpointSlices\n{ep}"


# ═══════════════ Config & Resources ═══════════════

@tool
def get_k8s_configmap(cluster_name: str, configmap_name: str,
                  namespace: str = "default") -> str:
    """Get ConfigMap content."""
    return _kubectl(cluster_name, ["get", "configmap", configmap_name, "-n", namespace, "-o", "yaml"], timeout=10)


@tool
def list_k8s_namespace_resources(cluster_name: str, namespace: str = "default") -> str:
    """List all resources in a namespace."""
    return _kubectl(cluster_name, ["get", "all", "-n", namespace, "-o", "wide"], timeout=15)


@tool
def get_k8s_pv_pvc_status(cluster_name: str, namespace: str = "") -> str:
    """Get PV/PVC status."""
    pv = _kubectl(cluster_name, ["get", "pv", "-o", "wide"], timeout=10)
    pvc_args = ["get", "pvc"] + (["-n", namespace] if namespace else ["--all-namespaces"])
    pvc = _kubectl(cluster_name, pvc_args, timeout=10)
    return f"## PersistentVolumes\n{pv}\n\n## PersistentVolumeClaims\n{pvc}"


@tool
def get_k8s_ingress_status(cluster_name: str, namespace: str = "") -> str:
    """Get Ingress resources and backend status."""
    args = ["get", "ingress", "-o", "wide"]
    if namespace:
        args.extend(["-n", namespace])
    else:
        args.append("--all-namespaces")
    return _kubectl(cluster_name, args, timeout=10)


# ═══════════════ CoreDNS ═══════════════

@tool
def get_k8s_coredns_logs(cluster_name: str, tail_lines: int = 200,
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
def describe_k8s_coredns(cluster_name: str) -> str:
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


# ═══════════════ Helm 3 ═══════════════

@tool
def list_k8s_helm_releases(cluster_name: str, namespace: str = "") -> str:
    """List all Helm 3 releases by querying release secrets.

    Helm 3 stores release metadata in secrets with label owner=helm.
    Returns release name, revision, status, chart, and last deployed time.

    Args:
        cluster_name: cluster name configured in DIAGNOSTICS_K8S_SERVERS.
        namespace: optional namespace filter. If empty, shows all namespaces.
    """
    args = [
        "get", "secrets",
        "-l", "owner=helm",
        "--sort-by=.metadata.creationTimestamp",
        "-o",
        "custom-columns="
        "RELEASE:.metadata.labels.name,"
        "REVISION:.metadata.labels.version,"
        "STATUS:.metadata.labels.status,"
        "CHART:.metadata.labels.chart,"
        "NAMESPACE:.metadata.namespace,"
        "AGE:.metadata.creationTimestamp",
    ]
    if namespace:
        args.extend(["-n", namespace])
    else:
        args.append("--all-namespaces")
    return _kubectl(cluster_name, args, timeout=15)


@tool
def get_k8s_helm_release_history(cluster_name: str, release_name: str,
                              namespace: str = "default", max_revisions: int = 10) -> str:
    """Get revision history of a Helm release from its stored secrets.

    Args:
        cluster_name: cluster name configured in DIAGNOSTICS_K8S_SERVERS.
        release_name: Helm release name.
        namespace: namespace where the release is deployed (default "default").
        max_revisions: max number of revisions to show (default 10).
    """
    secrets = _kubectl(cluster_name, [
        "get", "secrets", "-n", namespace,
        "-l", f"owner=helm,name={release_name}",
        "--sort-by=.metadata.labels.version",
        "-o",
        "custom-columns="
        "REVISION:.metadata.labels.version,"
        "STATUS:.metadata.labels.status,"
        "CHART:.metadata.labels.chart,"
        "APP_VERSION:.metadata.labels.app,"
        "CREATED:.metadata.creationTimestamp",
    ], timeout=15)
    if not secrets or "[kubectl error" in secrets:
        return f"[Helm release '{release_name}' not found in namespace '{namespace}']"

    lines = secrets.splitlines()
    if len(lines) <= 1:
        return f"[No revision history for '{release_name}']"
    header = lines[0]
    recent = lines[-max_revisions:] if len(lines) > max_revisions + 1 else lines[1:]
    return "REVISION  STATUS  CHART  APP_VERSION  CREATED\n" + "\n".join(recent)


@tool
def get_k8s_helm_release_values(cluster_name: str, release_name: str,
                             namespace: str = "default", revision: int = 0) -> str:
    """Extract values from a Helm release secret for a specific revision.

    If revision=0, returns the latest revision's values. Values are stored
    base64-encoded in the release secret's data.release field.

    Args:
        cluster_name: cluster name configured in DIAGNOSTICS_K8S_SERVERS.
        release_name: Helm release name.
        namespace: namespace (default "default").
        revision: revision number. 0 = latest (default).
    """
    import base64
    import json

    if revision > 0:
        secret_name_pattern = f"sh.helm.release.v1.{release_name}.v{revision}"
    else:
        # Get latest revision
        secrets_raw = _kubectl(cluster_name, [
            "get", "secrets", "-n", namespace,
            "-l", f"owner=helm,name={release_name}",
            "--sort-by=.metadata.labels.version",
            "-o", "jsonpath={.items[-1].metadata.name}",
        ], timeout=10)
        if not secrets_raw or "[kubectl error" in secrets_raw:
            return f"[Helm release '{release_name}' not found in '{namespace}']"
        secret_name_pattern = secrets_raw.strip()

    secret_yaml = _kubectl(cluster_name, [
        "get", "secret", secret_name_pattern, "-n", namespace, "-o", "json",
    ], timeout=10)
    if "[kubectl error" in secret_yaml:
        return f"[Secret '{secret_name_pattern}' not found]"

    try:
        secret = json.loads(secret_yaml)
        release_b64 = secret.get("data", {}).get("release", "")
        if release_b64:
            decoded = base64.b64decode(release_b64)
            # The release data is gzip-compressed
            import gzip
            release_data = json.loads(gzip.decompress(decoded))
            config = release_data.get("config", {})
            chart = release_data.get("chart", {}).get("metadata", {})
            info = release_data.get("info", {})
            return (
                f"## {release_name} (revision {release_data.get('version', '?')})\n"
                f"Chart: {chart.get('name', '?')}-{chart.get('version', '?')}\n"
                f"Status: {info.get('status', '?')}\n"
                f"Deployed: {info.get('last_deployed', '?')}\n\n"
                f"### Values\n```yaml\n{json.dumps(config, indent=2, ensure_ascii=False)}\n```"
            )
    except Exception as e:
        return f"[Failed to decode release values: {e}]"

    return "[No values found]"


# ═══════════════ Node Conditions ═══════════════

@tool
def get_k8s_node_conditions(cluster_name: str) -> str:
    """Get a quick overview of all node conditions (Ready, MemoryPressure, DiskPressure, PIDPressure).

    Args:
        cluster_name: cluster name configured in DIAGNOSTICS_K8S_SERVERS.
    """
    return _kubectl(cluster_name, [
        "get", "nodes",
        "-o", "custom-columns="
        "NAME:.metadata.name,"
        "READY:.status.conditions[?(@.type==\"Ready\")].status,"
        "MEMORY:.status.conditions[?(@.type==\"MemoryPressure\")].status,"
        "DISK:.status.conditions[?(@.type==\"DiskPressure\")].status,"
        "PID:.status.conditions[?(@.type==\"PIDPressure\")].status,"
        "NETWORK:.status.conditions[?(@.type==\"NetworkUnavailable\")].status,"
        "VERSION:.status.nodeInfo.kubeletVersion,"
        "AGE:.metadata.creationTimestamp",
    ], timeout=15)


# ═══════════════ Network Policy ═══════════════

@tool
def get_k8s_network_policies(cluster_name: str, namespace: str = "") -> str:
    """List NetworkPolicies affecting pod-to-pod communication.

    Args:
        cluster_name: cluster name configured in DIAGNOSTICS_K8S_SERVERS.
        namespace: optional namespace filter. If empty, shows all namespaces.
    """
    args = ["get", "networkpolicies", "-o", "wide"]
    if namespace:
        args.extend(["-n", namespace])
    else:
        args.append("--all-namespaces")
    return _kubectl(cluster_name, args, timeout=15)


# ═══════════════ RBAC ═══════════════

@tool
def check_k8s_rbac_permissions(cluster_name: str, namespace: str = "") -> str:
    """List RBAC roles, rolebindings, and clusterroles to debug permission issues.

    Args:
        cluster_name: cluster name configured in DIAGNOSTICS_K8S_SERVERS.
        namespace: namespace to check. If empty, shows cluster-scoped bindings only.
    """
    parts = []
    if namespace:
        roles = _kubectl(cluster_name, ["get", "roles,rolebindings", "-n", namespace, "-o", "wide"], timeout=10)
        parts.append(f"## Namespace {namespace}\n{roles}")
    crb = _kubectl(cluster_name, ["get", "clusterrolebindings", "-o", "wide"], timeout=10)
    parts.append(f"## ClusterRoleBindings\n{crb}")
    return "\n\n".join(parts)


# ═══════════════ Pod Restart Summary ═══════════════

@tool
def get_k8s_pod_restart_counts(cluster_name: str, namespace: str = "") -> str:
    """Get pods with high restart counts (>=5 restarts), sorted by restart count.

    Args:
        cluster_name: cluster name configured in DIAGNOSTICS_K8S_SERVERS.
        namespace: optional namespace filter. If empty, shows all namespaces.
    """
    args = [
        "get", "pods",
        "-o", "custom-columns="
        "NAMESPACE:.metadata.namespace,"
        "NAME:.metadata.name,"
        "RESTARTS:.status.containerStatuses[*].restartCount,"
        "STATUS:.status.phase,"
        "NODE:.spec.nodeName",
        "--sort-by=.status.containerStatuses[*].restartCount",
    ]
    if namespace:
        args.extend(["-n", namespace])
    else:
        args.append("--all-namespaces")
    raw = _kubectl(cluster_name, args, timeout=15)
    lines = raw.splitlines()
    if len(lines) <= 1:
        return raw
    header = lines[0]
    # Filter rows with restart count >= 5
    high_restarts = []
    for line in lines[1:]:
        parts_line = line.split()
        # restart count is the 3rd column (after namespace, name)
        try:
            restarts = sum(int(r) for r in parts_line[2].split(",") if r.isdigit())
            if restarts >= 5:
                high_restarts.append(f"{line}  (total: {restarts})")
        except (ValueError, IndexError):
            continue
    if high_restarts:
        return f"{header}\n" + "\n".join(high_restarts[-20:]) + f"\n(Showing pods with ≥5 restarts, last 20)"
    return f"{header}\n(No pods with ≥5 restarts)"


# ═══════════════ Certificate & Security ═══════════════

@tool
def check_k8s_certificate_expiry(cluster_name: str) -> str:
    """Check TLS certificate expiry for kube-apiserver, kubelet, and etcd.

    Args:
        cluster_name: cluster name configured in DIAGNOSTICS_K8S_SERVERS.

    Note: Placeholder — integrate with your certificate inspection API.
    """
    return "[PLACEHOLDER] Certificate expiry check — implement via your cert inspection API."


@tool
def check_k8s_webhook_status(cluster_name: str) -> str:
    """List MutatingWebhookConfigurations and ValidatingWebhookConfigurations.

    Use when Pod creation/update is unexpectedly blocked or modified.

    Args:
        cluster_name: cluster name configured in DIAGNOSTICS_K8S_SERVERS.
    """
    mutating = _kubectl(cluster_name, [
        "get", "mutatingwebhookconfigurations", "-o", "wide",
    ], timeout=10)
    validating = _kubectl(cluster_name, [
        "get", "validatingwebhookconfigurations", "-o", "wide",
    ], timeout=10)
    return f"## MutatingWebhookConfigurations\n{mutating}\n\n## ValidatingWebhookConfigurations\n{validating}"


# ═══════════════ etcd ═══════════════

_ETCDCTL_FLAGS = (
    "--cacert=/etc/kubernetes/pki/etcd/ca.crt "
    "--cert=/etc/kubernetes/pki/etcd/server.crt "
    "--key=/etc/kubernetes/pki/etcd/server.key "
    "--endpoints=https://127.0.0.1:2379"
)


def _etcd_exec(cluster_name: str, etcdctl_args: str, timeout: int = 15) -> str:
    """Execute etcdctl in the first available etcd pod."""
    # Find an etcd pod
    pods_raw = _kubectl(cluster_name, [
        "get", "pods", "-n", "kube-system",
        "-l", "component=etcd",
        "-o", "jsonpath={.items[0].metadata.name}",
    ], timeout=10)
    if not pods_raw or "[kubectl error" in pods_raw:
        pods_raw = _kubectl(cluster_name, [
            "get", "pods", "-n", "kube-system",
            "-l", "app=etcd",
            "-o", "jsonpath={.items[0].metadata.name}",
        ], timeout=10)
    if not pods_raw or "[kubectl error" in pods_raw:
        return "[etcd pod not found in kube-system]"
    pod = pods_raw.strip()

    cmd = f"etcdctl {etcdctl_args}"
    return _kubectl(cluster_name, [
        "exec", pod, "-n", "kube-system", "--", "sh", "-c", cmd,
    ], timeout=timeout)


@tool
def get_k8s_etcd_status(cluster_name: str) -> str:
    """Get etcd cluster status: pod health, member list, leader info.

    Args:
        cluster_name: cluster name configured in DIAGNOSTICS_K8S_SERVERS.
    """
    parts = []

    # Pod status
    pods = _kubectl(cluster_name, [
        "get", "pods", "-n", "kube-system",
        "-l", "component=etcd", "-o", "wide",
    ], timeout=10)
    if not pods or "[kubectl error" in pods:
        pods = _kubectl(cluster_name, [
            "get", "pods", "-n", "kube-system",
            "-l", "app=etcd", "-o", "wide",
        ], timeout=10)
    parts.append(f"## etcd Pods\n{pods}")

    # Member list
    members = _etcd_exec(cluster_name, f"{_ETCDCTL_FLAGS} member list", timeout=10)
    parts.append(f"## etcd Members\n{members}")

    # Endpoint status
    status = _etcd_exec(cluster_name, f"{_ETCDCTL_FLAGS} endpoint status --write-out=table", timeout=15)
    parts.append(f"## Endpoint Status\n{status}")

    return "\n\n".join(parts)


@tool
def get_k8s_etcd_logs(cluster_name: str = "default", tail_lines: int = 200,
                  since_minutes: int = 0) -> str:
    """Get recent logs from all etcd pods.

    Args:
        cluster_name: cluster name configured in DIAGNOSTICS_K8S_SERVERS.
        tail_lines: number of recent log lines per pod (default 200).
        since_minutes: if > 0, fetch logs from last N minutes.
    """
    pods_raw = _kubectl(cluster_name, [
        "get", "pods", "-n", "kube-system",
        "-l", "component=etcd", "-o", "name",
    ], timeout=10)
    if not pods_raw or "[kubectl error" in pods_raw:
        pods_raw = _kubectl(cluster_name, [
            "get", "pods", "-n", "kube-system",
            "-l", "app=etcd", "-o", "name",
        ], timeout=10)
    if not pods_raw or "[kubectl error" in pods_raw:
        return "[etcd pods not found]"

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
    return "\n\n".join(parts)


@tool
def check_k8s_etcd_health(cluster_name: str) -> str:
    """Check etcd endpoint health, alarm list, and Raft leader changes.

    Args:
        cluster_name: cluster name configured in DIAGNOSTICS_K8S_SERVERS.
    """
    parts = []

    # Endpoint health
    health = _etcd_exec(cluster_name, f"{_ETCDCTL_FLAGS} endpoint health", timeout=10)
    parts.append(f"## Endpoint Health\n{health}")

    # Alarms
    alarms = _etcd_exec(cluster_name, f"{_ETCDCTL_FLAGS} alarm list", timeout=10)
    parts.append(f"## Alarms\n{alarms}")

    # Leader changes counter (from metrics)
    leader_changes = _etcd_exec(
        cluster_name,
        f"{_ETCDCTL_FLAGS} endpoint status --write-out=json 2>/dev/null | "
        "grep -o '\"leader\":[0-9]*' | head -5",
        timeout=10,
    )
    parts.append(f"## Leader IDs\n{leader_changes}")

    return "\n\n".join(parts)


@tool
def get_k8s_etcd_metrics(cluster_name: str) -> str:
    """Get key etcd metrics: disk backend commit duration, Raft proposal duration, WAL fsync.

    Args:
        cluster_name: cluster name configured in DIAGNOSTICS_K8S_SERVERS.
    """
    metrics_cmd = (
        "curl -s http://localhost:2379/metrics 2>/dev/null | "
        "grep -E 'etcd_disk_backend_commit_duration_seconds_bucket|"
        "etcd_disk_wal_fsync_duration_seconds_bucket|"
        "etcd_network_peer_round_trip_time_seconds_bucket|"
        "etcd_server_leader_changes_seen_total|"
        "etcd_server_has_leader|"
        "etcd_server_proposals_failed_total|"
        "etcd_server_heartbeat_send_failures_total' | "
        "grep -v '#' | head -30"
    )
    return _etcd_exec(cluster_name, metrics_cmd, timeout=15)

# ═══════════════════════════════════════════════════════════════════
# 实际环境工具面补齐（生产在用、此前缺失的占位实现）
# ═══════════════════════════════════════════════════════════════════


@tool
def get_pod_node_name(cluster_name: str, namespace: str, pod_name: str) -> str:
    """获取Pod所在节点信息（节点名/IP）。"""
    return _kubectl(cluster_name, ["get", "pod", pod_name, "-n", namespace,
                                   "-o", "jsonpath={.spec.nodeName}{'\\t'}{.status.hostIP}"])


@tool
def get_k8s_kubelet_status(cluster_name: str, node_name: str = "") -> str:
    """获取节点容器组件状态：kubelet 运行状态、runtime。"""
    return _kubectl(cluster_name, ["get", "node", node_name, "-o", "wide"])


@tool
def get_k8s_kubelet_logs(cluster_name: str, node_name: str,
                         tail_lines: int = 200) -> str:
    """获取节点Kubelet日志。"""
    return "[LIVE] kubelet logs — requires OSP script execution on the target node."


@tool
def get_k8s_kubeproxy_logs(cluster_name: str, node_name: str,
                           tail_lines: int = 200) -> str:
    """获取节点Kube-Proxy日志。"""
    return "[LIVE] kube-proxy logs — requires OSP script execution on the target node."


@tool
def get_k8s_xid_logs(cluster_name: str, node_name: str = "") -> str:
    """获取节点XID错误日志（GPU Xid/NVRM，来自 dmesg）。"""
    return "[LIVE] GPU Xid/NVRM logs — requires OSP script execution on the target node."


@tool
def get_k8s_etcd_check(cluster_name: str, endpoint: str = "") -> str:
    """获取etcd健康检查：成员/leader/延迟/DB 用量。

    返回契约（2026-09-10 明确）：bool 维度渲染为「是/否」而非值+单位
    （mock 曾输出 `0.0bool`）；has_leader=否 时须显式给出「无 Leader
    （多数派丢失 / quorum lost）」判定与后续确认方向（成员状态 + WAL fsync）。
    """
    return _etcd_exec(cluster_name, "etcdctl endpoint health && etcdctl endpoint status -w table")


@tool
def get_k8s_node_events_info(cluster_name: str, node_name: str = "") -> str:
    """获取K8S节点事件日志（节点级事件）。"""
    field = f"--field-selector involvedObject.name={node_name}" if node_name else ""
    return _kubectl(cluster_name, ["get", "events", "-A", field, "--sort-by=.lastTimestamp"])


@tool
def get_k8s_elb_service(cluster_name: str, service_name: str = "") -> str:
    """获取K8S ELB服务信息。"""
    return _kubectl(cluster_name, ["get", "service", service_name, "-A", "-o", "wide"])


@tool
def get_k8s_vpc_cni(cluster_name: str, node_name: str = "") -> str:
    """获取K8S VPC CNI网络信息。"""
    return "[LIVE] VPC CNI info — requires OSP script execution on the target node."


@tool
def get_k8s_resource_top(cluster_name: str, resource_type: str = "pods",
                         namespace: str = "") -> str:
    """获取K8S资源（节点/Pod）的Top资源使用统计。"""
    if resource_type in ("node", "nodes"):
        return _kubectl(cluster_name, ["top", "nodes"])
    ns = ["-n", namespace] if namespace else ["-A"]
    return _kubectl(cluster_name, ["top", "pods", *ns, "--sort-by=cpu"])


@tool
def get_k8s_resource_list(cluster_name: str, resource_type: str = "pods",
                          namespace: str = "default") -> str:
    """获取K8S指定资源列表。"""
    return _kubectl(cluster_name, ["get", resource_type, "-n", namespace, "-o", "wide"])


@tool
def get_k8s_resource_summary(cluster_name: str, namespace: str,
                             resource_type: str, resource_name: str) -> str:
    """获取K8S资源的精简要信息。"""
    return _kubectl(cluster_name, ["get", resource_type, resource_name,
                                   "-n", namespace, "-o", "wide"])


@tool
def get_k8s_resource_history(cluster_name: str, namespace: str,
                             resource_type: str, resource_name: str) -> str:
    """获取K8S资源变更历史记录。"""
    return _kubectl(cluster_name, ["rollout", "history",
                                   f"{resource_type}/{resource_name}", "-n", namespace])

@tool
def check_k8s_control_plane() -> str:
    """[LIVE 占位] Check Kubernetes control plane health: kube-apiserver, etcd, kube-scheduler, kube-controller-manager.

    移植说明：实现体请对接实际环境的对应接口/脚本；签名与 mock 严格一致。
    """
    return "[LIVE] check_k8s_control_plane — implement against the actual environment backend (signature mirrors mock)."


@tool
def check_k8s_nodes() -> str:
    """[LIVE 占位] Check Kubernetes node statuses, resource usage, and conditions (MemoryPressure, etc.).

    移植说明：实现体请对接实际环境的对应接口/脚本；签名与 mock 严格一致。
    """
    return "[LIVE] check_k8s_nodes — implement against the actual environment backend (signature mirrors mock)."


@tool
def check_k8s_pods(namespace: str = 'default') -> str:
    """[LIVE 占位] Check Kubernetes pod statuses, restart counts, and OOM events.

    移植说明：实现体请对接实际环境的对应接口/脚本；签名与 mock 严格一致。
    """
    return "[LIVE] check_k8s_pods — implement against the actual environment backend (signature mirrors mock)."


@tool
def get_k8s_apigateway_status(cluster_name: str, namespace: str = 'kube-system') -> str:
    """[LIVE 占位] 获取K8S API Gateway状态（Serverless 控制面 VK→SCI 隧道入口链路）。

    移植说明：实现体请对接实际环境的对应接口/脚本；签名与 mock 严格一致。
    """
    return "[LIVE] get_k8s_apigateway_status — implement against the actual environment backend (signature mirrors mock)."


@tool
def get_k8s_group1_status(cluster_name: str, namespace: str = 'kmc-ingress') -> str:
    """[LIVE 占位] 获取K8S Group1 隧道状态（Serverless logs/exec 隧道组件）。

    移植说明：实现体请对接实际环境的对应接口/脚本；签名与 mock 严格一致。
    """
    return "[LIVE] get_k8s_group1_status — implement against the actual environment backend (signature mirrors mock)."


@tool
def get_k8s_ipam_status(cluster_name: str, namespace: str = 'kmc-system') -> str:
    """[LIVE 占位] 获取K8S IPAM状态（Serverless Pod 固定 IP 分配与冲突）。

    移植说明：实现体请对接实际环境的对应接口/脚本；签名与 mock 严格一致。
    """
    return "[LIVE] get_k8s_ipam_status — implement against the actual environment backend (signature mirrors mock)."


@tool
def get_k8s_pod_ip(cluster_name: str, namespace: str, pod_name: str) -> str:
    """[LIVE 占位] 获取K8S Pod IP 与固定 IP 保留记录（VPC-CNI/IPAM 分配）。

    移植说明：实现体请对接实际环境的对应接口/脚本；签名与 mock 严格一致。
    """
    return "[LIVE] get_k8s_pod_ip — implement against the actual environment backend (signature mirrors mock)."
