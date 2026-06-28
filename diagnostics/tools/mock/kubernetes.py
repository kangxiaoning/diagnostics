"""Mock Kubernetes diagnostic tools — scenario-driven equivalents of production kubectl tools.

These tools follow the same signatures as those in live/kubernetes.py
but return pre-crafted scenario-based responses instead of calling kubectl.

One-to-one correspondence: live/kubernetes.py

Scenario data is keyed by the global `_active_scenario` from `diagnostics.tools.mock.scenarios`.
"""

from __future__ import annotations

from langchain_core.tools import tool

from diagnostics.tools.mock.data import (
    kubernetes_control_plane_data,
    kubernetes_nodes_data,
    kubernetes_pods_data,
)
from diagnostics.tools.mock.scenarios import get_active_scenario

# ═══════════════════════════════════════════════════════════════════
# Scenario data: Pod Logs
# ═══════════════════════════════════════════════════════════════════

_POD_LOGS: dict[str, str] = {
    "normal": (
        "2026-06-14T08:00:01.123Z INFO  Application started successfully\n"
        "2026-06-14T08:00:02.456Z INFO  Listening on port 8080\n"
        "2026-06-14T08:00:30.789Z INFO  Health check passed\n"
        "2026-06-14T08:01:00.012Z INFO  Processing request GET /api/health (200) 2ms\n"
    ),
    "container_crash": (
        "2026-06-14T07:55:00.123Z INFO  Application started\n"
        "2026-06-14T07:55:05.456Z WARN  Heap usage: 480MiB / 512MiB (93%)\n"
        "2026-06-14T07:56:10.789Z WARN  GC paused 2.3s — near OOM threshold\n"
        "2026-06-14T07:56:30.012Z ERROR java.lang.OutOfMemoryError: Java heap space\n"
        "2026-06-14T07:56:30.345Z WARN  Container exceeded memory limit — OOMKilled\n"
        "2026-06-14T07:57:00.123Z INFO  Application started (restart #12 in last hour)\n"
    ),
    "conntrack_table_full": (
        "2026-06-14T08:00:01.123Z INFO  Gateway started on :8080\n"
        "2026-06-14T08:00:05.456Z INFO  Connecting to redis.default.svc.cluster.local... OK\n"
        "2026-06-14T08:02:30.789Z WARN  DNS resolution timeout for backend.default.svc.cluster.local (5s)\n"
        "2026-06-14T08:02:31.012Z ERROR Connection refused to backend (UDP DNS drop by conntrack)\n"
        "2026-06-14T08:03:00.456Z WARN  Liveness probe timeout — connection refused after conntrack drop\n"
        "2026-06-14T08:03:15.000Z INFO  Container restart triggered by failed probe\n"
    ),
    "cpu_throttle_probe_failure": (
        "2026-06-14T08:00:01.123Z INFO  Application started\n"
        "2026-06-14T08:00:05.456Z INFO  Processing requests normally...\n"
        "2026-06-14T08:01:30.789Z INFO  Heavy computation task started (batch job)\n"
        "2026-06-14T08:02:00.012Z WARN  kubelet PLEG is not healthy — probe delayed 8.5s\n"
        "2026-06-14T08:02:05.345Z INFO  Liveness probe GET /health → timeout 5s (CPU throttled)\n"
        "2026-06-14T08:02:10.678Z ERROR Container killed by kubelet — liveness probe timeout\n"
        "2026-06-14T08:02:30.000Z INFO  Application started (restart #8)\n"
    ),
    "kubelet_disk_io_starvation": (
        "2026-06-14T06:30:00.123Z INFO  Application started 2 days ago\n"
        "2026-06-14T06:30:01.456Z INFO  Processing requests...\n"
        "2026-06-14T07:45:00.789Z WARN  kubelet heartbeat lost — node lease not renewed (disk IO timeout)\n"
        "2026-06-14T07:50:00.012Z INFO  Node marked NotReady — Pods being evicted\n"
        "2026-06-14T07:50:05.000Z ERROR Container terminated — eviction by NodeController\n"
    ),
    "node_oom_kubelet_killed": (
        "2026-06-14T06:00:00.123Z INFO  Application started 2 days ago\n"
        "2026-06-14T06:00:01.456Z INFO  Processing requests normally...\n"
        "2026-06-14T07:30:00.789Z WARN  System memory exhausted — Java RSS=5.8GB on worker-2\n"
        "2026-06-14T07:31:00.012Z WARN  Node connection lost — kubelet killed by OOM Killer (PID 3210)\n"
        "2026-06-14T07:31:05.000Z ERROR Node status Unknown — all Pods unreachable\n"
    ),
    "multi_layer_cascading": (
        "2026-06-14T06:00:00.123Z INFO  api-gateway started\n"
        "2026-06-14T07:00:05.456Z WARN  Heap usage: 480MiB / 512MiB (94%)\n"
        "2026-06-14T07:05:30.789Z ERROR java.lang.OutOfMemoryError: Java heap space\n"
        "2026-06-14T07:05:31.012Z INFO  OOMKilled — cgroup memory limit 512Mi exceeded (RSS=650MiB)\n"
        "2026-06-14T07:05:40.000Z INFO  Container restart #18 in CrashLoopBackOff\n"
        "2026-06-14T07:30:00.456Z WARN  DNS resolution timeout for user-service (CoreDNS 1/3 replicas)\n"
        "2026-06-14T07:30:05.789Z ERROR Intermittent DNS failure — 2 CoreDNS on worker-3 lost due to Node NotReady\n"
    ),
    "conntrack_and_oom": (
        "2026-06-14T14:50:00.123Z INFO  api-gateway started, connected to backend\n"
        "2026-06-14T14:55:00.456Z WARN  Connection timeout to backend-svc (retry 1)\n"
        "2026-06-14T15:00:00.789Z WARN  DNS lookup timeout for backend.default.svc.cluster.local\n"
        "2026-06-14T15:03:00.012Z WARN  Heap usage: 5.8GiB / 6.0GiB — near limit (off-heap leak!)\n"
        "2026-06-14T15:05:00.345Z ERROR java.lang.OutOfMemoryError: Direct buffer memory (RSS=6.4GB)\n"
        "2026-06-14T15:05:00.456Z FATAL OOM Killer invoked — process 8765 (java) killed\n"
        "2026-06-14T15:06:00.123Z INFO  api-gateway restarted (RSS climbing again...)\n"
    ),
    "disk_io_and_dns": (
        "2026-06-14T14:58:00.123Z INFO  backend-svc processing requests (P99=200ms)\n"
        "2026-06-14T15:02:00.456Z WARN  Write I/O timeout (30s) — disk saturated by logrotate\n"
        "2026-06-14T15:03:00.789Z WARN  Response latency spiked to 5.2s (awaiting disk write)\n"
        "2026-06-14T15:04:00.012Z ERROR java.net.UnknownHostException: upstream-db.external.com\n"
        "2026-06-14T15:04:05.345Z ERROR DNS query to 10.0.0.53:53 timed out (upstream DNS offline)\n"
        "2026-06-14T15:04:10.678Z WARN  Falling back to stale DNS cache entry\n"
    ),
    "etcd_quota_near_full": (
        "2026-06-14T18:00:01.123Z INFO  api-gateway v1.5.2 started on :8080 (last deployed 180d ago)\n"
        "2026-06-14T18:01:00.456Z INFO  Watcher connected to kube-apiserver OK\n"
        "2026-06-14T18:02:30.789Z ERROR failed to fetch service backend endpoints: etcdserver: request timed out\n"
        "2026-06-14T18:02:31.012Z WARN  Readiness check failed: unable to resolve backend.default.svc (DNS timeout)\n"
        "2026-06-14T18:02:35.456Z ERROR Liveness probe failed: etcd request timeout while listing Service endpoints\n"
        "2026-06-14T18:02:40.000Z INFO  Container restart triggered by failed probe (exit:1)\n"
        "2026-06-14T18:03:00.123Z INFO  api-gateway restarted (restart #8 in 2d)\n"
    ),
    "oom_score_misconfig": (
        "2026-06-14T14:30:00.123Z INFO  java-app started successfully on worker-2\n"
        "2026-06-14T14:30:05.456Z INFO  Processing requests normally...\n"
        "2026-06-14T15:00:00.789Z WARN  System memory pressure increasing (RSS growing)\n"
        "2026-06-14T15:05:00.012Z WARN  kubelet connection lost — node worker-2 unreachable\n"
        "2026-06-14T15:05:05.345Z INFO  Application still running — OOM Killer killed kubelet (PID 3210) instead of java app!\n"
        "2026-06-14T15:05:10.678Z WARN  oom_score_adj=-500 for kubelet should have protected it — misconfig confirmed\n"
    ),
    "memory_leak_and_disk_full": (
        "2026-06-14T14:50:00.123Z INFO  java-backend started, heap=2Gi, off-heap stable\n"
        "2026-06-14T14:55:00.456Z WARN  Heap usage: 85% — RSS climbing 5.8GB (off-heap leak suspected)\n"
        "2026-06-14T15:00:00.789Z ERROR Failed to write to /var/log/app.log: No space left on device\n"
        "2026-06-14T15:02:00.012Z WARN  Disk full + RSS 6.8GB — approaching double failure\n"
        "2026-06-14T15:03:00.345Z FATAL OOM Killer invoked — process 8765 (java) killed (RSS=6.8GB)\n"
        "2026-06-14T15:03:15.678Z ERROR Container OOMKilled — cannot write crash log (disk full!)\n"
    ),
    "coredns_cache_poison": (
        "2026-06-14T10:00:00.123Z INFO  backend-svc started, connecting to upstream\n"
        "2026-06-14T10:01:00.456Z INFO  Resolved backend-svc.default.svc → 10.0.1.55 OK\n"
        "2026-06-14T10:30:00.789Z WARN  DNS resolved backend-svc.default.svc → 10.0.1.88 (wrong IP! stale cache entry)\n"
        "2026-06-14T10:31:00.012Z ERROR Connection refused to 10.0.1.88 — Pod already terminated\n"
        "2026-06-14T10:32:00.345Z INFO  DNS re-resolved backend-svc → 10.0.1.55 (corrected after cache expiry)\n"
        "2026-06-14T10:35:00.678Z WARN  DNS again returned stale IP 10.0.1.88 — CoreDNS cache pollution persists\n"
    ),
    "dns_and_etcd": (
        "2026-06-14T09:00:00.123Z INFO  user-svc started, connected to internal services\n"
        "2026-06-14T09:05:00.456Z WARN  DNS resolution timeout for internal-api.default.svc (5s)\n"
        "2026-06-14T09:06:00.789Z ERROR Failed to resolve user-service.default.svc: CoreDNS CrashLoopBackOff\n"
        "2026-06-14T09:07:00.012Z WARN  Reconnected after DNS retry — intermittent failures continue\n"
        "2026-06-14T09:10:00.345Z ERROR etcd leader changed mid-request — API Server slow\n"
        "2026-06-14T09:12:00.678Z FATAL liveness probe failed: DNS lookup timeout → container restart #5\n"
    ),
    "image_pull_backoff": (
        "2026-06-14T08:00:00.123Z INFO  new-deploy Pod scheduled on worker-2\n"
        "2026-06-14T08:00:05.456Z ERROR Failed to pull image \"node:18-alpine\": received unexpected HTTP status: 429 Too Many Requests\n"
        "2026-06-14T08:01:00.789Z WARN  Image pull rate limit exceeded — Docker Hub free tier limit (100 pulls/6h)\n"
        "2026-06-14T08:02:00.012Z ERROR ImagePullBackOff — waiting for rate limit reset\n"
        "2026-06-14T08:05:00.345Z INFO  Retrying image pull... 429 again — rate limit not yet reset\n"
    ),
}

_POD_PREVIOUS_LOGS: dict[str, str] = {
    "container_crash": (
        "2026-06-14T07:55:00.123Z INFO  Application started\n"
        "2026-06-14T07:55:10.456Z WARN  Heap usage rapidly climbing: 320/512MiB\n"
        "2026-06-14T07:55:30.789Z WARN  Full GC triggered — failed to reclaim (RSS 500MiB)\n"
        "2026-06-14T07:55:50.012Z ERROR java.lang.OutOfMemoryError: Java heap space\n"
        "Previous exit code: 137 (SIGKILL — OOMKilled by cgroup)\n"
    ),
    "conntrack_table_full": (
        "2026-06-14T07:55:00.123Z INFO  Previous instance started\n"
        "2026-06-14T07:56:00.456Z ERROR Connection refused to upstream (conntrack drop)\n"
        "2026-06-14T07:57:00.789Z ERROR Name resolution failed for backend service\n"
        "Previous exit code: 143 (SIGTERM — probe failed due to conntrack table full)\n"
    ),
    "cpu_throttle_probe_failure": (
        "2026-06-14T07:55:00.123Z INFO  Previous instance — processing valid requests\n"
        "2026-06-14T07:56:30.456Z WARN  Liveness probe failed — app was healthy but kubelet probe timed out\n"
        "Previous exit code: 137 (SIGKILL — liveness probe timeout due to CPU throttling)\n"
    ),
}

# ═══════════════════════════════════════════════════════════════════
# Scenario data: Pod Describe
# ═══════════════════════════════════════════════════════════════════

_POD_DESCRIBE: dict[str, str] = {
    "normal": (
        "Name:             nginx-7d8f-abc12\n"
        "Namespace:        default\n"
        "Status:           Running\n"
        "Containers:\n"
        "  nginx:\n"
        "    Image: nginx:1.25\n"
        "    State: Running (Started: 3 days ago)\n"
        "    Ready: True\n"
        "    Restart Count: 0\n"
        "    Limits: cpu=500m, memory=256Mi\n"
        "    Requests: cpu=250m, memory=128Mi\n"
        "Conditions: Initialized=True, Ready=True, ContainersReady=True\n"
    ),
    "container_crash": (
        "Name:             java-backend-abc12\n"
        "Namespace:        default\n"
        "Status:           CrashLoopBackOff\n"
        "Containers:\n"
        "  java-backend:\n"
        "    Image: openjdk:17-slim\n"
        "    State: Waiting (CrashLoopBackOff)\n"
        "    Last State: Terminated (OOMKilled, ExitCode: 137)\n"
        "    Restart Count: 12\n"
        "    Limits: memory=512Mi\n"
        "    Requests: memory=256Mi\n"
        "    Last Termination Reason: OOMKilled\n"
        "    Last Termination Message: Memory cgroup out of memory: Killed process 12345 (java)\n"
        "Conditions: Initialized=True, Ready=False, ContainersReady=False\n"
        "Events: BackOff 12x — restarting failed container (OOMKilled)\n"
    ),
    "conntrack_table_full": (
        "Name:             api-gateway-abc12\n"
        "Namespace:        default\n"
        "Status:           Running\n"
        "Containers:\n"
        "  api-gateway:\n"
        "    Image: envoyproxy/envoy:1.28\n"
        "    State: Running (Started: 2 days ago)\n"
        "    Restart Count: 5\n"
        "    Last State: Terminated (ExitCode: 143)\n"
        "    Limits: memory=256Mi\n"
        "    Liveness: http-get :8080/health delay=30s period=10s timeout=5s\n"
        "    Readiness: http-get :8080/ready period=5s timeout=3s\n"
        "Conditions: Initialized=True, Ready=True, ContainersReady=True\n"
        "Events: Warning — Readiness probe failed: DNS lookup timeout\n"
        "        Warning — Liveness probe failed: connection refused (conntrack drop)\n"
    ),
    "cpu_throttle_probe_failure": (
        "Name:             java-backend-abc12\n"
        "Namespace:        default\n"
        "Status:           Running\n"
        "Containers:\n"
        "  java-backend:\n"
        "    Image: openjdk:17-slim\n"
        "    State: Running (Started: 8 minutes ago)\n"
        "    Restart Count: 8 (in 2 days)\n"
        "    Last State: Terminated (ExitCode: 137 — SIGKILL)\n"
        "    Limits: cpu=500m, memory=512Mi\n"
        "    Liveness: http-get :8080/health period=10s timeout=5s failureThreshold=3\n"
        "Conditions: Initialized=True, Ready=True, ContainersReady=True\n"
        "Events: Warning — Liveness probe failed: Get \"http://10.0.1.5:8080/health\": context deadline exceeded\n"
        "        Warning — BackOff 8x in 2 days (CPU throttle causing probe timeout, not app crash)\n"
    ),
    "kubelet_disk_io_starvation": (
        "Name:             java-backend-def34\n"
        "Namespace:        default\n"
        "Status:           Unknown\n"
        "Containers:\n"
        "  java-backend:\n"
        "    Image: openjdk:17-slim\n"
        "    State: Running (Last seen 4 minutes ago)\n"
        "    Restart Count: 0\n"
        "Conditions: Ready=Unknown (NodeStatusUnknown)\n"
        "Events: Warning — Node worker-2 status is Unknown(NotReady)\n"
        "        Warning — Pod evicted: Node lost heartbeat (kubelet lease timeout)\n"
    ),
    "node_oom_kubelet_killed": (
        "Name:             java-backend-abc12\n"
        "Namespace:        default\n"
        "Status:           Unknown\n"
        "Containers:\n"
        "  app:\n"
        "    State: Unknown — node communication lost\n"
        "Conditions: Ready=Unknown\n"
        "Events: Warning — Node worker-2 has been unresponsive for 6m (StatusUnknown)\n"
        "        Warning — All pods on worker-2 unreachable (kubelet killed by OOM Killer PID 3210)\n"
    ),
    "multi_layer_cascading": (
        "Name:             api-gateway-abc12\n"
        "Namespace:        default\n"
        "Status:           CrashLoopBackOff\n"
        "Containers:\n"
        "  api-gateway:\n"
        "    Image: api-gateway:v2.1\n"
        "    State: Waiting (CrashLoopBackOff)\n"
        "    Last State: Terminated (OOMKilled, ExitCode: 137)\n"
        "    Restart Count: 18\n"
        "    Limits: memory=512Mi\n"
        "    Actual RSS at crash: ~650MiB\n"
        "Events: Warning — BackOff 18x (cgroup OOM: RSS 650MiB > limit 512MiB)\n"
        "        Warning — DNS resolution timeout for user-service (intermittent)\n"
    ),
    "conntrack_and_oom": (
        "Name:             api-gateway-abc12\n"
        "Namespace:        default\n"
        "Status:           OOMKilled\n"
        "Containers:\n"
        "  api-gateway:\n"
        "    Image: api-gateway:v3.2\n"
        "    State: Terminated (OOMKilled, ExitCode: 137)\n"
        "    Restart Count: 8\n"
        "    Limits: memory=6Gi (host Java RSS=6.4GB due to off-heap leak)\n"
        "    Last Termination Reason: java invoked oom-killer: total-vm:12849152kB anon-rss:6754304kB\n"
        "Events: Warning — OOMKilled 8x in 2d (heap=2Gi stable, off-heap climbing 0.2Gi/day)\n"
        "        Warning — Readiness probe failed: DNS lookup timeout (conntrack UDP drop)\n"
    ),
    "disk_io_and_dns": (
        "Name:             backend-svc-abc12\n"
        "Namespace:        default\n"
        "Status:           Running\n"
        "Containers:\n"
        "  backend-svc:\n"
        "    Image: backend-svc:v1.8\n"
        "    State: Running\n"
        "    Restart Count: 0\n"
        "    Limits: memory=1Gi\n"
        "Events: Warning — Write I/O timeout 30s (disk saturated by logrotate)\n"
        "        Warning — java.net.UnknownHostException: upstream-db.external.com\n"
    ),
    "etcd_quota_near_full": (
        "Name:             api-gateway-abc12\n"
        "Namespace:        default\n"
        "Status:           CrashLoopBackOff\n"
        "Containers:\n"
        "  api-gateway:\n"
        "    Image: registry.example.com/api-gateway:v1.5.2\n"
        "    State: Waiting (CrashLoopBackOff)\n"
        "    Last State: Terminated (ExitCode: 1 — Error)\n"
        "    Restart Count: 8\n"
        "    Limits: cpu=500m, memory=512Mi\n"
        "    Requests: cpu=250m, memory=256Mi\n"
        "    Liveness: http-get :8080/health period=10s timeout=5s failureThreshold=3\n"
        "    Readiness: http-get :8080/ready period=5s timeout=3s\n"
        "Conditions: Initialized=True, Ready=False, ContainersReady=False\n"
        "Events: Warning — Readiness probe failed: context deadline exceeded (etcd timeout)\n"
        "        Warning — Liveness probe failed: etcd request timeout while listing Service\n"
        "        Normal  — Killing container (failed probe, exit:1)\n"
        "说明: api-gateway 因 etcd 写入延迟导致探针超时，不是OOM或CPU问题\n"
        "      对比multi_layer场景: 此处exit:1(探针失败) vs exit:137(OOMKilled)\n"
    ),
    "oom_score_misconfig": (
        "Name:             java-app-abc12\n"
        "Namespace:        default\n"
        "Status:           Running\n"
        "Containers:\n"
        "  java-app:\n"
        "    Image: openjdk:17-slim\n"
        "    State: Running (Started: 2 days ago)\n"
        "    Restart Count: 0\n"
        "    Limits: memory=512Mi\n"
        "    oom_score_adj: 750 (— 应用反而存活!)\n"
        "Conditions: Initialized=True, Ready=True, ContainersReady=True (正常!)\n"
        "Events: Warning — Node worker-2 status Unknown — kubelet killed by OOM Killer\n"
        "        Warning — kubelet oom_score_adj=-500 but still killed (OOM Killer bug/misconfig)\n"
        "说明: 应用Pod oom_score_adj=750反而存活，kubelet oom_score_adj=-500却被杀\n"
        "      OOM Killer 选择逻辑异常—可能的kernel bug或cgroup版本不匹配\n"
    ),
    "memory_leak_and_disk_full": (
        "Name:             java-backend-abc12\n"
        "Namespace:        default\n"
        "Status:           OOMKilled\n"
        "Containers:\n"
        "  java-backend:\n"
        "    Image: openjdk:17-slim\n"
        "    State: Terminated (OOMKilled, ExitCode: 137)\n"
        "    Last State: Terminated (OOMKilled)\n"
        "    Restart Count: 8\n"
        "    Limits: memory=1Gi\n"
        "    Last Termination Reason: OOMKilled (RSS=6.8GB, heap=2Gi stable, off-heap leak!)\n"
        "Conditions: Ready=False, ContainersReady=False\n"
        "Events: Warning — OOMKilled 8x in 2d (off-heap memory leak + disk full!)\n"
        "        Warning — Failed to write logs: ext4 journal abort (disk full)\n"
        "说明: 双根因—Java堆外泄漏OOMKilled + 磁盘空间耗尽无法写日志\n"
    ),
    "coredns_cache_poison": (
        "Name:             backend-svc-def34\n"
        "Namespace:        default\n"
        "Status:           Running\n"
        "Containers:\n"
        "  backend-svc:\n"
        "    Image: backend-service:v2.0\n"
        "    State: Running (Started: 2 days ago)\n"
        "    Restart Count: 0\n"
        "    Limits: memory=512Mi\n"
        "Conditions: Initialized=True, Ready=True, ContainersReady=True\n"
        "Events: Warning — Intermittent connection failure to 10.0.1.88 (stale DNS cache!)\n"
        "        Warning — DNS resolved to wrong IP — CoreDNS cache returned terminated Pod endpoint\n"
        "说明: Pod本身健康，但DNS间歇解析到已终止的旧Pod IP→请求路由到错误端点\n"
    ),
    "dns_and_etcd": (
        "Name:             user-svc-abc12\n"
        "Namespace:        default\n"
        "Status:           CrashLoopBackOff\n"
        "Containers:\n"
        "  user-svc:\n"
        "    Image: user-service:v1.3\n"
        "    State: Waiting (CrashLoopBackOff)\n"
        "    Last State: Terminated (ExitCode: 1 — Error)\n"
        "    Restart Count: 5\n"
        "    Limits: memory=256Mi\n"
        "Conditions: Initialized=True, Ready=False, ContainersReady=False\n"
        "Events: Warning — liveness probe failed: DNS lookup timeout (CoreDNS CrashLoopBackOff)\n"
        "        Warning — CrashLoopBackOff #5 in 2d — DNS failure preventing startup\n"
        "        Warning — etcd leader changed during API Server request\n"
        "说明: 双根因—CoreDNS CrashLoopBackOff导致DNS解析超时+etcd leader频繁切换\n"
    ),
    "image_pull_backoff": (
        "Name:             new-deploy-abc12\n"
        "Namespace:        default\n"
        "Status:           ImagePullBackOff\n"
        "Containers:\n"
        "  node-app:\n"
        "    Image: node:18-alpine\n"
        "    State: Waiting (ImagePullBackOff)\n"
        "    Restart Count: 0\n"
        "Conditions: Initialized=False, Ready=False, ContainersReady=False\n"
        "Events: Warning — Failed to pull image \"node:18-alpine\": 429 Too Many Requests\n"
        "        Warning — Image pull rate limit exceeded (Docker Hub free tier)\n"
        "        Normal  — Pulling image \"node:18-alpine\" (retry #5)\n"
        "说明: Docker Hub速率限制导致ImagePullBackOff—已有Pod不受影响，仅新Pod受阻\n"
    ),
}

# ═══════════════════════════════════════════════════════════════════
# Scenario data: Pod Events
# ═══════════════════════════════════════════════════════════════════

_POD_EVENTS: dict[str, str] = {
    "normal": (
        "LAST SEEN   TYPE     REASON      MESSAGE\n"
        "3d          Normal   Scheduled   Successfully assigned default/nginx-7d8f-abc12 to worker-1\n"
        "3d          Normal   Pulled      Container image \"nginx:1.25\" already present on machine\n"
        "3d          Normal   Created     Created container nginx\n"
        "3d          Normal   Started     Started container nginx\n"
    ),
    "container_crash": (
        "LAST SEEN   TYPE      REASON       MESSAGE\n"
        "2h          Normal    Scheduled    Successfully assigned default/java-backend-abc12 to worker-2\n"
        "2h          Normal    Pulled       Container image already present on machine\n"
        "2h          Normal    Created      Created container java-backend\n"
        "2h          Normal    Started      Started container java-backend\n"
        "30m         Warning   OOMKilled    Container java-backend exceeded memory limit 512Mi\n"
        "30m         Warning   BackOff      Back-off restarting failed container (OOMKilled)\n"
        "25m         Warning   OOMKilled    (repeat 4 times in last 30min)\n"
        "5m          Warning   OOMKilled    Latest OOM — RSS=620MiB, limit=512MiB\n"
    ),
    "conntrack_table_full": (
        "LAST SEEN   TYPE      REASON               MESSAGE\n"
        "2d          Normal    Scheduled            Assigned to worker-2\n"
        "2d          Normal    Started              Container started\n"
        "4h          Warning   ReadinessProbeFailed Readiness probe failed: DNS lookup timeout\n"
        "3h          Warning   LivenessProbeFailed  Liveness probe failed: connection refused\n"
        "3h          Normal    Killing              Killing container — failed liveness probe\n"
        "1h          Warning   ReadinessProbeFailed (repeated due to conntrack table full)\n"
    ),
    "cpu_throttle_probe_failure": (
        "LAST SEEN   TYPE      REASON               MESSAGE\n"
        "2d          Normal    Scheduled            Assigned to worker-1\n"
        "2d          Normal    Started              Container started\n"
        "8h          Warning   LivenessProbeFailed  Liveness probe timeout (http-get 5s)\n"
        "8h          Normal    Killing              Killing container (failed liveness)\n"
        "4h          Warning   LivenessProbeFailed  (repeat — CPU throttle, not app error)\n"
        "2h          Warning   PLEGNotHealthy       kubelet PLEG is not healthy (CPU bound)\n"
        "1h          Normal    Killing              Repeat kill #8 in 2 days\n"
    ),
    "kubelet_disk_io_starvation": (
        "LAST SEEN   TYPE      REASON               MESSAGE\n"
        "2d          Normal    Scheduled            Assigned to worker-2\n"
        "2d          Normal    Started              Container started\n"
        "40m         Warning   NodeNotReady         Node worker-2 status is NotReady (NodeStatusUnknown)\n"
        "35m         Warning   Evicted              Pod evicted — kubelet heartbeat lost (disk IO timeout)\n"
        "35m         Normal    Killing              Stopping container (eviction)\n"
    ),
    "node_oom_kubelet_killed": (
        "LAST SEEN   TYPE      REASON               MESSAGE\n"
        "2d          Normal    Scheduled            Assigned to worker-2\n"
        "2d          Normal    Started              Container started\n"
        "35m         Warning   NodeStatusUnknown    Node worker-2 stopped posting node status\n"
        "30m         Warning   Unreachable          Pod unreachable — kubelet process killed by OOM Killer\n"
    ),
    "multi_layer_cascading": (
        "LAST SEEN   TYPE      REASON               MESSAGE\n"
        "2h          Normal    Scheduled            Assigned to worker-1\n"
        "2h          Normal    Started              Container started\n"
        "1h          Warning   OOMKilled            Container exceeded memory limit 512Mi (RSS=650MiB)\n"
        "30m         Warning   BackOff              CrashLoopBackOff #18 — OOMKilled recurrence\n"
        "25m         Warning   DNSLookupTimeout     Intermittent DNS failure — CoreDNS only 1/3 replicas\n"
    ),
    "conntrack_and_oom": (
        "LAST SEEN   TYPE      REASON               MESSAGE\n"
        "2d          Normal    Scheduled            Assigned to worker-3\n"
        "2d          Normal    Started              Container started\n"
        "1h          Warning   OOMKilled            Process java(8765) RSS=6.4GB — OOM Killer invoked\n"
        "45m         Warning   DNSLookupTimeout     Readiness probe: DNS timeout (conntrack UDP drop)\n"
        "30m         Warning   BackOff              CrashLoopBackOff #5\n"
        "15m         Warning   OOMKilled            Repeat kill #8 in 2 days\n"
    ),
    "disk_io_and_dns": (
        "LAST SEEN   TYPE      REASON               MESSAGE\n"
        "2d          Normal    Scheduled            Assigned to worker-2\n"
        "2d          Normal    Started              Container started\n"
        "40m         Warning   IOTimeout            Write I/O timeout 30s (disk saturated)\n"
        "35m         Warning   DNSResolutionFailed  UnknownHostException: upstream-db.external.com\n"
    ),
    "etcd_quota_near_full": (
        "LAST SEEN   TYPE      REASON               MESSAGE\n"
        "2d          Normal    Scheduled            Assigned to worker-1\n"
        "2d          Normal    Started              Container started\n"
        "3h          Warning   ReadinessProbeFailed Readiness probe HTTP 8080/ready timeout 5s (etcd slow)\n"
        "2h          Warning   LivenessProbeFailed  Liveness probe HTTP 8080/health timeout 5s (etcd slow)\n"
        "2h          Normal    Killing              Killing container (failed liveness probe, exit:1)\n"
        "1h          Warning   ReadinessProbeFailed (repeat — etcd NOSPACE causing API Server write latency)\n"
        "30m         Warning   BackOff              CrashLoopBackOff #8 — restart triggered by etcd timeout\n"
        "10m         Normal    Started              Container restarted (last restart #8)\n"
    ),
    "oom_score_misconfig": (
        "LAST SEEN   TYPE      REASON               MESSAGE\n"
        "2d          Normal    Scheduled            Assigned to worker-2\n"
        "2d          Normal    Started              Container started (java-app)\n"
        "35m         Warning   NodeStatusUnknown    Node worker-2 stopped posting status (kubelet killed!)\n"
        "35m         Warning   OOMKillMisconfig     OOM Killer killed kubelet (oom_score_adj=-500) instead of java-app (oom_score_adj=750)\n"
        "30m         Warning   NodeUnreachable      Pods on worker-2 unreachable — kubelet process killed by OOM\n"
    ),
    "memory_leak_and_disk_full": (
        "LAST SEEN   TYPE      REASON               MESSAGE\n"
        "2d          Normal    Scheduled            Assigned to worker-1\n"
        "2d          Normal    Started              Container started\n"
        "1h          Warning   MemoryPressure       RSS climbing 5.8→6.8GB (off-heap leak!)\n"
        "30m         Warning   DiskPressure         /dev/sda1 disk usage 100% — log write failed\n"
        "15m         Warning   OOMKilled            Process java(8765) RSS=6.8GB — OOM Killer invoked\n"
        "10m         Warning   BackOff              CrashLoopBackOff #8 (disk full prevents log writes)\n"
    ),
    "coredns_cache_poison": (
        "LAST SEEN   TYPE      REASON               MESSAGE\n"
        "2d          Normal    Scheduled            Assigned to worker-2\n"
        "2d          Normal    Started              Container started\n"
        "1h          Warning   DNSResolutionError   Service resolved to stale IP 10.0.1.88 (Pod already terminated)\n"
        "45m         Warning   ConnectionRefused    Connection to 10.0.1.88:8080 refused (stale endpoint)\n"
        "30m         Warning   DNSResolutionError   DNS again returned stale IP — CoreDNS cache pollution persists\n"
    ),
    "dns_and_etcd": (
        "LAST SEEN   TYPE      REASON               MESSAGE\n"
        "2d          Normal    Scheduled            Assigned to worker-2\n"
        "2d          Normal    Started              Container started\n"
        "3h          Warning   DNSLookupTimeout     DNS lookup timeout for internal-api.default.svc\n"
        "2h          Warning   CrashLoopBackOff     CoreDNS pod CrashLoopBackOff — DNS unavailable\n"
        "1h          Warning   EtcdLeaderChanged    etcd leader changed during API Server request\n"
        "30m         Warning   BackOff              CrashLoopBackOff #5 — liveness probe DNS timeout\n"
    ),
    "image_pull_backoff": (
        "LAST SEEN   TYPE      REASON               MESSAGE\n"
        "30m         Normal    Scheduled            Assigned to worker-2\n"
        "30m         Warning   Failed               Failed to pull image \"node:18-alpine\": 429 Too Many Requests\n"
        "25m         Warning   Failed               Image pull rate limit exceeded (Docker Hub free tier: 100 pulls/6h)\n"
        "20m         Warning   BackOff              Back-off pulling image — waiting for rate limit reset\n"
        "15m         Warning   Failed               (repeat — rate limit not yet reset)\n"
    ),
}

# ═══════════════════════════════════════════════════════════════════
# Scenario data: etcd
# ═══════════════════════════════════════════════════════════════════

_ETCD_STATUS: dict[str, str] = {
    "normal": (
        "## etcd Pods\n"
        "NAME     READY   STATUS    RESTARTS   AGE\n"
        "etcd-master-1   1/1     Running   0          30d\n"
        "etcd-master-2   1/1     Running   0          30d\n"
        "etcd-master-3   1/1     Running   0          30d\n"
        "\n## etcd Members\n"
        "8e9e05c52164694d, started, master-1, https://172.16.0.1:2380, https://172.16.0.1:2379, false\n"
        "91cc3fe84ba1c2f0, started, master-2, https://172.16.0.2:2380, https://172.16.0.2:2379, false\n"
        "a6f4b2d18c0e9a35, started, master-3, https://172.16.0.3:2380, https://172.16.0.3:2379, true\n"
        "\n## Endpoint Status\n"
        "ENDPOINT           ID                       VERSION  DB SIZE  IS LEADER  RAFT TERM  RAFT INDEX\n"
        "https://172.16.0.1:2379  8e9e05c52164694d  3.5.12   45 MB    false      285        12548790\n"
        "https://172.16.0.2:2379  91cc3fe84ba1c2f0  3.5.12   45 MB    false      285        12548790\n"
        "https://172.16.0.3:2379  a6f4b2d18c0e9a35  3.5.12   45 MB    true       285        12548790\n"
    ),
    "kubelet_disk_io_starvation": (
        "## etcd Pods\n"
        "NAME     READY   STATUS    RESTARTS   AGE\n"
        "etcd-master-1   1/1     Running   2          30d\n"
        "etcd-master-2   1/1     Running   0          30d\n"
        "etcd-master-3   1/1     Running   0          30d\n"
        "\n## etcd Members\n"
        "8e9e05c52164694d, started, master-1, https://172.16.0.1:2380, https://172.16.0.1:2379, false\n"
        "91cc3fe84ba1c2f0, started, master-2, https://172.16.0.2:2380, https://172.16.0.2:2379, false\n"
        "a6f4b2d18c0e9a35, started, master-3, https://172.16.0.3:2380, https://172.16.0.3:2379, true\n"
        "\n## Endpoint Status\n"
        "master-1: fsync p99=250ms (WARNING: >100ms threshold!)\n"
    ),
    "multi_layer_cascading": (
        "## etcd Pods\n"
        "NAME     READY   STATUS    RESTARTS   AGE\n"
        "etcd-master-1   1/1     Running   5          30d (frequent restarts!)\n"
        "etcd-master-2   1/1     Running   0          30d\n"
        "etcd-master-3   1/1     Running   0          30d\n"
        "\n## etcd Members\n"
        "8e9e05c52164694d, started, master-1, https://172.16.0.1:2380, https://172.16.0.1:2379, true\n"
        "91cc3fe84ba1c2f0, started, master-2, https://172.16.0.2:2380, https://172.16.0.2:2379, false\n"
        "a6f4b2d18c0e9a35, started, master-3, https://172.16.0.3:2380, https://172.16.0.3:2379, false\n"
        "\n## Endpoint Status\n"
        "ENDPOINT           DB SIZE  RAFT TERM  RAFT INDEX\n"
        "172.16.0.1:2379   48 MB    298        12556700 (leader, fsync p99=185ms CRITICAL!)\n"
        "172.16.0.2:2379   48 MB    298        12556700 (follower)\n"
        "172.16.0.3:2379   48 MB    298        12556700 (follower)\n"
        "WARNING: leader master-1 disk IO saturated — etcd process in D state!\n"
    ),
    "etcd_quota_near_full": (
        "## etcd Pods\n"
        "NAME     READY   STATUS    RESTARTS   AGE\n"
        "etcd-master-1   1/1     Running   0          30d\n"
        "etcd-master-2   1/1     Running   0          30d\n"
        "etcd-master-3   1/1     Running   0          30d\n"
        "\n## etcd Members\n"
        "8e9e05c52164694d, started, master-1, https://172.16.0.1:2380, https://172.16.0.1:2379, true\n"
        "91cc3fe84ba1c2f0, started, master-2, https://172.16.0.2:2380, https://172.16.0.2:2379, false\n"
        "a6f4b2d18c0e9a35, started, master-3, https://172.16.0.3:2380, https://172.16.0.3:2379, false\n"
        "\n## Endpoint Status\n"
        "ENDPOINT           DB SIZE   RAFT TERM  RAFT INDEX\n"
        "172.16.0.1:2379   7.15 GB   320        13985500 (leader, backend_commit p99=185ms WARNING!)\n"
        "172.16.0.2:2379   7.15 GB   320        13985500 (follower)\n"
        "172.16.0.3:2379   7.15 GB   320        13985500 (follower)\n"
        "WARNING: DB size 7.15GB approaching 8GB quota — NOSPACE alarm on all members!\n"
        "说明: 3 memebr均健康无重启，但DB大小已到89%配额，compaction无法释放足够空间\n"
    ),
    "etcd_quorum_loss": (
        "## etcd Pods\n"
        "NAME     READY   STATUS    RESTARTS   AGE\n"
        "etcd-master-1   0/1     CrashLoopBackOff 12    30d (leader lost!)\n"
        "etcd-master-2   0/1     CrashLoopBackOff 8     30d (follower down!)\n"
        "etcd-master-3   1/1     Running   0          30d (single survivor!)\n"
        "\n## etcd Members\n"
        "8e9e05c52164694d, unreachable, master-1, https://172.16.0.1:2380, https://172.16.0.1:2379, lost\n"
        "91cc3fe84ba1c2f0, unreachable, master-2, https://172.16.0.2:2380, https://172.16.0.2:2379, lost\n"
        "a6f4b2d18c0e9a35, started, master-3, https://172.16.0.3:2380, https://172.16.0.3:2379, false\n"
        "\n## Endpoint Status\n"
        "https://172.16.0.1:2379: unhealthy — connection refused (etcd pod CrashLoopBackOff)\n"
        "https://172.16.0.2:2379: unhealthy — connection refused (etcd pod CrashLoopBackOff)\n"
        "https://172.16.0.3:2379: 仅1/3存活 — 无法形成法定人数! quorum lost!\n"
        "说明: 2/3 etcd members down → quorum lost → API Server 降级只读模式\n"
        "      根因: 某批io hang导致crash，磁盘io问题为主要原因\n"
    ),
    "dns_and_etcd": (
        "## etcd Pods\n"
        "NAME     READY   STATUS    RESTARTS   AGE\n"
        "etcd-master-1   1/1     Running   8          30d (leader elections频繁!)\n"
        "etcd-master-2   1/1     Running   3          30d\n"
        "etcd-master-3   1/1     Running   0          30d\n"
        "\n## etcd Members\n"
        "8e9e05c52164694d, started, master-1, https://172.16.0.1:2380, https://172.16.0.1:2379, false (频繁leader切换!)\n"
        "91cc3fe84ba1c2f0, started, master-2, https://172.16.0.2:2380, https://172.16.0.2:2379, true\n"
        "a6f4b2d18c0e9a35, started, master-3, https://172.16.0.3:2380, https://172.16.0.3:2379, false\n"
        "\n## Endpoint Status\n"
        "ENDPOINT           RAFT TERM  RAFT INDEX\n"
        "172.16.0.1:2379   328        16552300 (频繁leader election! 8次restart)\n"
        "172.16.0.2:2379   328        16552300\n"
        "172.16.0.3:2379   328        16552300\n"
        "WARNING: etcd leader election频繁 — master-1磁盘不稳定导致leader切换\n"
        "说明: etcd leader频繁切换+CoreDNS CrashLoopBackOff — 双重控制面故障\n"
    ),
}

_ETCD_HEALTH: dict[str, str] = {
    "normal": (
        "## Endpoint Health\n"
        "https://172.16.0.1:2379 is healthy: successfully committed proposal: took 2.5ms\n"
        "https://172.16.0.2:2379 is healthy: successfully committed proposal: took 3.1ms\n"
        "https://172.16.0.3:2379 is healthy: successfully committed proposal: took 2.8ms\n"
        "\n## Alarms\n"
        "（无告警）\n"
        "\n## Leader IDs\n"
        "\"leader\": 13740458283750686435 (master-3)\n"
    ),
    "multi_layer_cascading": (
        "## Endpoint Health\n"
        "https://172.16.0.1:2379 is healthy: successfully committed proposal: took 1850.0ms (CRITICAL! >1s)\n"
        "https://172.16.0.2:2379 is healthy: successfully committed proposal: took 3.2ms\n"
        "https://172.16.0.3:2379 is healthy: successfully committed proposal: took 2.9ms\n"
        "\n## Alarms\n"
        "memberID:8e9e05c52164694d alarm:NOSPACE\n"
        "\n## Leader IDs\n"
        "\"leader\": 13740458283750686435 (master-1 — leader with severe disk IO latency!)\n"
    ),
    "etcd_quota_near_full": (
        "## Endpoint Health\n"
        "https://172.16.0.1:2379 is healthy: successfully committed proposal: took 85.2ms (WARNING >50ms)\n"
        "https://172.16.0.2:2379 is healthy: successfully committed proposal: took 78.5ms (WARNING >50ms)\n"
        "https://172.16.0.3:2379 is healthy: successfully committed proposal: took 92.1ms (WARNING >50ms)\n"
        "\n## Alarms\n"
        "memberID:8e9e05c52164694d alarm:NOSPACE\n"
        "memberID:91cc3fe84ba1c2f0 alarm:NOSPACE\n"
        "memberID:a6f4b2d18c0e9a35 alarm:NOSPACE\n"
        "\n## DB Size\n"
        "8e9e05c52164694d: 7.15 GB / 8.0 GB (89.4% — 即将触发只读模式!)\n"
        "91cc3fe84ba1c2f0: 7.15 GB / 8.0 GB (89.4%)\n"
        "a6f4b2d18c0e9a35: 7.15 GB / 8.0 GB (89.4%)\n"
        "quota-backend-bytes=8GB, 任一成员达到 100% 时 etcd 将进入只读模式\n"
        "\n## Leader IDs\n"
        "\"leader\": 13740458283750686435 (master-1)\n"
    ),
    "etcd_quorum_loss": (
        "## Endpoint Health\n"
        "https://172.16.0.1:2379 is unhealthy: failed to connect (CrashLoopBackOff, fsync latency 250ms p99!)\n"
        "https://172.16.0.2:2379 is unhealthy: failed to connect (CrashLoopBackOff, disk IO hang!)\n"
        "https://172.16.0.3:2379 is unhealthy: cluster lacks quorum (1/3 members alive)\n"
        "\n## Alarms\n"
        "MEMBER ERROR: cluster has no leader — quorum lost (2/3 members down)\n"
        "MEMBER ERROR: raft proposal cannot be committed (majority unreachable)\n"
        "\n## Leader IDs\n"
        "\"leader\": 0 (NO LEADER — quorum lost!)\n"
        "说明: 2/3 etcd节点因磁盘延迟CrashLoopBackOff → 法定人数丢失 → API Server只读模式\n"
    ),
    "dns_and_etcd": (
        "## Endpoint Health\n"
        "https://172.16.0.1:2379 is healthy: successfully committed proposal: took 350.0ms (WARNING! >100ms)\n"
        "https://172.16.0.2:2379 is healthy: successfully committed proposal: took 3.5ms\n"
        "https://172.16.0.3:2379 is healthy: successfully committed proposal: took 2.8ms\n"
        "\n## Alarms\n"
        "(无 NOSPACE/空间告警)\n"
        "\n## Leader IDs\n"
        "\"leader\": 25404582283750686435 (master-2 — 频繁切换: 8次在最近1h!)\n"
        "说明: master-1 磁盘不稳定 → leader 频繁 election → API Server 间歇变慢\n"
    ),
}

_ETCD_METRICS: dict[str, str] = {
    "normal": (
        "etcd_disk_backend_commit_duration_seconds_bucket{le=\"0.01\"} 45000\n"
        "etcd_disk_backend_commit_duration_seconds_bucket{le=\"0.1\"} 98000\n"
        "etcd_disk_backend_commit_duration_seconds_bucket{le=\"0.5\"} 99500\n"
        "etcd_disk_wal_fsync_duration_seconds_bucket{le=\"0.005\"} 48500\n"
        "etcd_server_leader_changes_seen_total 3\n"
        "etcd_server_has_leader 1\n"
        "etcd_server_proposals_failed_total 0\n"
        "etcd_server_heartbeat_send_failures_total 12\n"
    ),
    "kubelet_disk_io_starvation": (
        "etcd_disk_backend_commit_duration_seconds_bucket{le=\"0.1\"} 5200\n"
        "etcd_disk_backend_commit_duration_seconds_bucket{le=\"0.5\"} 18500\n"
        "WARNING: p99 backend_commit = 480ms (10x threshold!)\n"
        "etcd_disk_wal_fsync_duration_seconds_bucket{le=\"0.01\"} 1200\n"
        "WARNING: p99 wal_fsync = 350ms (severe disk latency!)\n"
        "etcd_server_leader_changes_seen_total 12 (frequent leader elections!)\n"
        "etcd_server_has_leader 1\n"
        "etcd_server_proposals_failed_total 85 (raft proposals failing!)\n"
    ),
    "multi_layer_cascading": (
        "etcd_disk_backend_commit_duration_seconds_bucket{le=\"0.1\"} 3200\n"
        "etcd_disk_backend_commit_duration_seconds_bucket{le=\"0.5\"} 12500\n"
        "CRITICAL: p99 backend_commit = 1850ms (etcd disk IO saturated!)\n"
        "etcd_disk_wal_fsync_duration_seconds_bucket{le=\"0.01\"} 800\n"
        "CRITICAL: p99 wal_fsync = 1250ms (WAL fsync blocked by disk IO)\n"
        "etcd_server_leader_changes_seen_total 28 (excessive leader elections!)\n"
        "etcd_server_has_leader 1\n"
        "etcd_server_proposals_failed_total 342 (raft proposals failing due to leader IO hang!)\n"
    ),
    "etcd_quota_near_full": (
        "etcd_disk_backend_commit_duration_seconds_bucket{le=\"0.01\"} 12000\n"
        "etcd_disk_backend_commit_duration_seconds_bucket{le=\"0.1\"} 45000\n"
        "etcd_disk_backend_commit_duration_seconds_bucket{le=\"0.5\"} 68000\n"
        "WARNING: p99 backend_commit = 185ms (3x threshold! DB compaction slow)\n"
        "etcd_disk_wal_fsync_duration_seconds_bucket{le=\"0.005\"} 38000\n"
        "WARNING: p99 wal_fsync = 42ms (WAL write slowed by DB size)\n"
        "etcd_server_leader_changes_seen_total 5\n"
        "etcd_server_has_leader 1\n"
        "etcd_server_proposals_failed_total 18 (raft proposals mildly failing)\n"
        "etcd_mvcc_db_total_size_in_bytes 7680000000 (7.15 GB / 8 GB quota!)\n"
        "etcd_server_health_failures 0 (no health failure yet, but NOSPACE alarm active)\n"
        "说明: DB接近8GB配额，compaction/min-free调整已触发，写入延迟上升但不至于超时\n"
    ),
    "etcd_quorum_loss": (
        "etcd_disk_backend_commit_duration_seconds_bucket{le=\"0.1\"} 800\n"
        "CRITICAL: p99 backend_commit = 1250ms (2 members unreachable!)\n"
        "etcd_disk_wal_fsync_duration_seconds_bucket{le=\"0.01\"} 200\n"
        "CRITICAL: p99 wal_fsync = 480ms (disk IO saturated — causing etcd crash!)\n"
        "etcd_server_leader_changes_seen_total 52 (extreme — leader lost repeatedly!)\n"
        "etcd_server_has_leader 0 (NO LEADER — quorum lost!)\n"
        "etcd_server_proposals_failed_total 1240 (all writes failing — quorum lost!)\n"
        "etcd_server_health_failures 2 (master-1 and master-2 unhealthy!)\n"
        "说明: 2/3节点CrashLoop→quorum丢失→所有写操作失败→API Server只读模式\n"
    ),
    "dns_and_etcd": (
        "etcd_disk_backend_commit_duration_seconds_bucket{le=\"0.1\"} 28000\n"
        "WARNING: p99 backend_commit = 380ms (disk unstable on master-1)\n"
        "etcd_disk_wal_fsync_duration_seconds_bucket{le=\"0.01\"} 22000\n"
        "WARNING: p99 wal_fsync = 95ms (moderate disk IO)\n"
        "etcd_server_leader_changes_seen_total 18 (frequent! 8x in last hour)\n"
        "etcd_server_has_leader 1\n"
        "etcd_server_proposals_failed_total 55 (raft proposals occasionally failing)\n"
        "etcd_server_health_failures 0\n"
        "说明: leader election频繁因master-1磁盘不稳定→API Server间歇变慢+CoreDNS CrashLoop\n"
    ),
}

_ETCD_LOGS: dict[str, str] = {
    "normal": (
        "2026-06-14T08:00:00.123Z INFO  saved snapshot at index 12548790\n"
        "2026-06-14T08:00:05.456Z INFO  wrote a new member to the cluster\n"
        "2026-06-14T08:00:30.789Z INFO  serving client requests normally\n"
    ),
    "multi_layer_cascading": (
        "2026-06-14T07:30:00.123Z WARN  apply entries took too long [1.85s for 128 entries]\n"
        "2026-06-14T07:30:05.456Z WARN  avoid-duration 1.85s exceeds election timeout 1s (WARNING!)\n"
        "2026-06-14T07:30:10.789Z WARN  failed to send out heartbeat on time (exceeded the 100ms timeout)\n"
        "2026-06-14T07:30:15.012Z WARN  leader failed to send out heartbeat — fsync took 1.2s\n"
        "2026-06-14T07:30:30.345Z ERROR raft proposal failed: request timed out\n"
        "2026-06-14T07:31:00.678Z WARN  disk backend commit blocked for 2.5s — check IO subsystem\n"
    ),
    "etcd_quota_near_full": (
        "2026-06-14T18:00:00.123Z WARN  database space quota exceeded ([8e9e05c52164694d, 91cc3fe84ba1c2f0, a6f4b2d18c0e9a35]) NOSPACE alarm raised\n"
        "2026-06-14T18:00:05.456Z INFO  saved snapshot at index 13985500 (compaction triggered but unable to free enough space)\n"
        "2026-06-14T18:00:10.789Z WARN  apply entries took too long [0.58s for 128 entries] (compaction running concurrently)\n"
        "2026-06-14T18:00:15.012Z WARN  mvcc: database space exceeded alarm — running compaction and defragmentation\n"
        "2026-06-14T18:00:30.345Z WARN  backend commit latency p99=185ms (compaction I/O contention)\n"
        "2026-06-14T18:01:00.678Z INFO  compaction took 45.2s — freed 120MB only (insufficient to clear NOSPACE)\n"
        "2026-06-14T18:02:00.123Z WARN  database size: 7.15GB / 8GB (89.4%) — compaction/min-free=10% insufficient\n"
    ),
    "etcd_quorum_loss": (
        "2026-06-14T07:50:00.123Z ERROR etcd-master-1: failed to send out heartbeat on time (exceeded the 100ms timeout)\n"
        "2026-06-14T07:50:05.456Z ERROR etcd-master-1: wal: sync duration 250ms, expected less than 100ms\n"
        "2026-06-14T07:50:10.789Z FATAL etcd-master-1: disk IO hang — process restarting (CrashLoopBackOff #12)\n"
        "2026-06-14T07:51:00.012Z ERROR etcd-master-2: disk IO hang — fsync p99=480ms (CRITICAL!)\n"
        "2026-06-14T07:51:05.345Z FATAL etcd-master-2: process crashed — disk IO timeout (CrashLoopBackOff #8)\n"
        "2026-06-14T07:52:00.678Z WARN  etcd-master-3: lost quorum — only 1/3 members alive\n"
        "说明: 2/3 etcd节点因磁盘IO hang崩溃→quorum lost→API Server只读模式\n"
    ),
}

# ═══════════════════════════════════════════════════════════════════
# Scenario data: CoreDNS
# ═══════════════════════════════════════════════════════════════════

_COREDNS_LOGS: dict[str, str] = {
    "normal": (
        "## CoreDNS Deployment\n"
        "NAME      READY   UP-TO-DATE   AVAILABLE   AGE\n"
        "coredns   2/2     2            2           30d\n"
        "\n## coredns-6d4b-xyz11\n"
        "2026-06-14T08:00:00.123Z [INFO] plugin/reload: Running configuration SHA512 = abc123\n"
        "2026-06-14T08:00:10.456Z [INFO] 127.0.0.1:12345 - \"A IN backend.default.svc.cluster.local. udp 52 false 512\" NOERROR qr,aa,rd 112 0.0001s\n"
    ),
    "conntrack_table_full": (
        "## CoreDNS Deployment\n"
        "NAME      READY   UP-TO-DATE   AVAILABLE   AGE\n"
        "coredns   2/2     2            2           30d\n"
        "\n## coredns-6d4b-mnp90\n"
        "2026-06-14T08:00:00.123Z [INFO] CoreDNS-1.11.1\n"
        "2026-06-14T08:02:30.456Z [ERROR] plugin/errors: 2 backend.default.svc.cluster.local. A: read udp 10.0.1.5:43211->10.0.0.10:53: i/o timeout\n"
        "2026-06-14T08:02:31.789Z [ERROR] plugin/errors: 2 order-service.default.svc.cluster.local. A: read udp ... i/o timeout\n"
        "2026-06-14T08:03:00.012Z [WARN] plugin/forward: max retries exceeded for upstream 10.0.0.10:53\n"
    ),
    "multi_layer_cascading": (
        "## CoreDNS Deployment\n"
        "NAME      READY   UP-TO-DATE   AVAILABLE   AGE\n"
        "coredns   1/3     3            1           30d (only 1 available!)\n"
        "\n## coredns-6d4b-xyz11 (worker-1 — only healthy replica)\n"
        "2026-06-14T07:30:00.123Z [INFO] CoreDNS-1.11.1\n"
        "2026-06-14T07:30:05.456Z [WARN] plugin/forward: read udp ... i/o timeout (overloaded!)\n"
        "2026-06-14T07:30:10.789Z [ERROR] plugin/errors: user-service.default A: read udp ... i/o timeout\n"
        "2026-06-14T07:30:15.012Z [WARN] Replicas reduced to 1/3 — 2 CoreDNS on NotReady worker-3 lost!\n"
        "\n## coredns-6d4b-abc99 (worker-3 — UNREACHABLE)\n"
        "[Pod unreachable — worker-3 NotReady, kubelet killed by OOM Killer]\n"
    ),
    "etcd_quota_near_full": (
        "## CoreDNS Deployment\n"
        "NAME      READY   UP-TO-DATE   AVAILABLE   AGE\n"
        "coredns   2/2     2            2           30d (正常!)\n"
        "\n## coredns-6d4b-xyz11\n"
        "2026-06-14T18:00:00.123Z [INFO] CoreDNS-1.11.1\n"
        "2026-06-14T18:02:30.456Z [WARN] plugin/kubernetes: failed to list *v1.Endpoints for backend: context deadline exceeded\n"
        "2026-06-14T18:02:31.789Z [WARN] plugin/kubernetes: failed to list *v1.Service for api-gateway: etcd request timeout\n"
        "2026-06-14T18:03:00.012Z [ERROR] plugin/errors: 2 backend.default.svc.cluster.local. A: read udp ... i/o timeout\n"
        "2026-06-14T18:05:00.345Z [WARN] plugin/kubernetes: List Endpoints latency p99=3.2s (normal<0.1s! etcd slow)\n"
        "\n## coredns-6d4b-abc99\n"
        "2026-06-14T18:00:00.123Z [INFO] CoreDNS-1.11.1\n"
        "2026-06-14T18:02:35.456Z [WARN] plugin/kubernetes: failed to list *v1.Endpoints: context deadline exceeded\n"
        "2026-06-14T18:03:15.789Z [ERROR] plugin/errors: api-gateway.default A: read udp ... i/o timeout\n"
        "说明: CoreDNS Pod全部健康(2/2)，但查询etcd时频繁超时 → etcd NOSPACE导致读延迟\n"
        "      并非CoreDNS自身问题—症状表现为DNS超时但根因在etcd层\n"
    ),
    "coredns_cache_poison": (
        "## CoreDNS Deployment\n"
        "NAME      READY   UP-TO-DATE   AVAILABLE   AGE\n"
        "coredns   2/2     2            2           30d (正常!)\n"
        "\n## coredns-6d4b-xyz11\n"
        "2026-06-14T10:00:00.123Z [INFO] CoreDNS-1.11.1\n"
        "2026-06-14T10:30:00.456Z [WARN] plugin/cache: serving stale record backend-svc.default → 10.0.1.88 (TTL not expired!)\n"
        "2026-06-14T10:30:05.789Z [INFO] plugin/kubernetes: resolved backend-svc.default → 10.0.1.88 (stale endpoint!)\n"
        "2026-06-14T10:31:00.012Z [WARN] plugin/kubernetes: Pod 10.0.1.88 already terminated — cache entry should have been invalidated\n"
        "2026-06-14T10:35:00.345Z [WARN] plugin/cache: returning cached entry for backend-svc (age=300s, TTL still valid)\n"
        "说明: CoreDNS缓存未正确失效已终止Pod的Endpoint→间歇返回错误IP\n"
        "      根因可能: kubelet未及时更新EndpointSlice→CoreDNS使用过时缓存\n"
    ),
    "dns_and_etcd": (
        "## CoreDNS Deployment\n"
        "NAME      READY   UP-TO-DATE   AVAILABLE   AGE\n"
        "coredns   1/2     2            1           30d (仅1副本可用!)\n"
        "\n## coredns-6d4b-xyz11 (worker-1 — only healthy replica)\n"
        "2026-06-14T09:00:00.123Z [INFO] CoreDNS-1.11.1\n"
        "2026-06-14T09:05:00.456Z [ERROR] plugin/kubernetes: failed to list *v1.Service: etcdserver: request timed out\n"
        "2026-06-14T09:05:05.789Z [WARN] plugin/forward: max retries exceeded for upstream\n"
        "\n## coredns-6d4b-def88 (worker-2 — CrashLoopBackOff)\n"
        "2026-06-14T08:50:00.123Z [FATAL] OOMKilled — memory limit 64Mi exceeded (need 128Mi!)\n"
        "2026-06-14T08:55:00.456Z [INFO] Starting CoreDNS-1.11.1 (restart #8)\n"
        "2026-06-14T08:58:00.789Z [FATAL] OOMKilled again — CrashLoopBackOff\n"
        "说明: CoreDNS 副本1/2 → 1个CrashLoopBackOff(资源不足OOMKilled) + etcd leader频繁切换\n"
        "      双根因叠加 → 剩余副本过载 + 查询etcd超时 → DNS间歇失败\n"
    ),
    "disk_io_and_dns": (
        "## CoreDNS Deployment\n"
        "NAME      READY   UP-TO-DATE   AVAILABLE   AGE\n"
        "coredns   2/2     2            2           30d\n"
        "\n## coredns-6d4b-xyz11\n"
        "2026-06-14T15:02:00.123Z [INFO] plugin/reload: Running configuration SHA512 = def456\n"
        "2026-06-14T15:03:00.456Z [ERROR] plugin/forward: read udp 10.0.0.53:53: i/o timeout\n"
        "2026-06-14T15:03:05.789Z [ERROR] plugin/forward: read udp 10.0.0.54:53: i/o timeout\n"
        "2026-06-14T15:03:10.012Z [ERROR] plugin/errors: 2 upstream-db.external.com. A: read udp 10.0.0.53:53: i/o timeout\n"
        "2026-06-14T15:03:15.345Z [WARN] plugin/forward: max retries exceeded for upstream 10.0.0.53:53\n"
        "2026-06-14T15:04:00.678Z [INFO] 127.0.0.1:12345 - \"A IN backend.default.svc.cluster.local. udp 52 false 512\" NOERROR qr,aa,rd 112 0.0001s\n"
        "2026-06-14T15:05:00.901Z [ERROR] plugin/forward: dial tcp 10.0.0.53:53: i/o timeout\n"
        "2026-06-14T15:06:00.234Z [ERROR] plugin/errors: 2 api.external-service.com. A: read udp 10.0.0.54:53: i/o timeout\n"
        "说明: CoreDNS Pod全部健康(2/2 Ready)，内部域名解析正常(cluster.local)\n"
        "      但forward插件指向已下线DNS(10.0.0.53/54不可达)→外部域名解析全部超时\n"
        "      这是独立根因，与磁盘IO饱和无关\n"
    ),
}

_COREDNS_DESCRIBE: dict[str, str] = {
    "normal": (
        "## CoreDNS Deployment\n"
        "Name:                   coredns\n"
        "Namespace:              kube-system\n"
        "Replicas:               2 desired | 2 available\n"
        "Strategy:               RollingUpdate (max unavailable 1)\n"
        "Conditions:             Available=True, Progressing=False\n"
        "\n## kube-dns Service\n"
        "NAME       TYPE        CLUSTER-IP   EXTERNAL-IP   PORT(S)\n"
        "kube-dns   ClusterIP   10.0.0.10    <none>        53/UDP,53/TCP\n"
        "\n## Corefile ConfigMap\n"
        ".:53 {\n    errors\n    health\n    kubernetes cluster.local in-addr.arpa ip6.arpa\n    prometheus :9153\n    forward . /etc/resolv.conf\n    cache 30\n    loop\n    reload\n}\n"
    ),
    "multi_layer_cascading": (
        "## CoreDNS Deployment\n"
        "Name:                   coredns\n"
        "Namespace:              kube-system\n"
        "Replicas:               3 desired | 1 available (2 lost on worker-3!)\n"
        "Strategy:               RollingUpdate (max unavailable 1)\n"
        "Conditions:             Available=False (MinimumReplicasUnavailable)\n"
        "Events: Warning — Deployment has minimum availability (1/3 replicas available)\n"
        "\n## kube-dns Service\n"
        "kube-dns   ClusterIP   10.0.0.10    <none>        53/UDP,53/TCP\n"
        "\n## Corefile ConfigMap (same as normal)\n"
    ),
    "etcd_quota_near_full": (
        "## CoreDNS Deployment\n"
        "Name:                   coredns\n"
        "Namespace:              kube-system\n"
        "Replicas:               2 desired | 2 available (正常!)\n"
        "Strategy:               RollingUpdate (max unavailable 1)\n"
        "Conditions:             Available=True, Progressing=False\n"
        "Events: (无异常事件)\n"
        "\n## kube-dns Service\n"
        "NAME       TYPE        CLUSTER-IP   EXTERNAL-IP   PORT(S)\n"
        "kube-dns   ClusterIP   10.0.0.10    <none>        53/UDP,53/TCP\n"
        "\n## Corefile ConfigMap\n"
        ".:53 {\n    errors\n    health\n    kubernetes cluster.local in-addr.arpa ip6.arpa\n    prometheus :9153\n    forward . /etc/resolv.conf\n    cache 30\n    loop\n    reload\n}\n"
        "说明: CoreDNS Deployment完全正常(2/2 Ready, Corefile标准, kube-dns Service存在)\n"
        "      不像 conntrack/multi_layer 场景，此场景 CoreDNS 本身无故障\n"
    ),
    "coredns_cache_poison": (
        "## CoreDNS Deployment\n"
        "Name:                   coredns\n"
        "Namespace:              kube-system\n"
        "Replicas:               2 desired | 2 available (正常!)\n"
        "Strategy:               RollingUpdate (max unavailable 1)\n"
        "Conditions:             Available=True, Progressing=False\n"
        "Events: (无异常事件 — CoreDNS Deployment本身健康)\n"
        "\n## kube-dns Service\n"
        "kube-dns   ClusterIP   10.0.0.10    <none>        53/UDP,53/TCP\n"
        "\n## Corefile ConfigMap\n"
        ".:53 {\n    errors\n    health\n    kubernetes cluster.local in-addr.arpa ip6.arpa\n    prometheus :9153\n    forward . /etc/resolv.conf\n    cache 30\n    loop\n    reload\n}\n"
        "说明: CoreDNS Deployment健康但cache未失效→返回stale endpoint IP→间歇路由错误\n"
        "      注意: CoreDNS本身无Crash/Restart—问题在缓存数据一致性\n"
    ),
    "dns_and_etcd": (
        "## CoreDNS Deployment\n"
        "Name:                   coredns\n"
        "Namespace:              kube-system\n"
        "Replicas:               2 desired | 1 available (1 CrashLoopBackOff!)\n"
        "Strategy:               RollingUpdate (max unavailable 1)\n"
        "Conditions:             Available=False (MinimumReplicasUnavailable)\n"
        "Events: Warning — CrashLoopBackOff on coredns-6d4b-def88 (OOMKilled: 64Mi limit too small!)\n"
        "\n## kube-dns Service\n"
        "kube-dns   ClusterIP   10.0.0.10    <none>        53/UDP,53/TCP\n"
        "\n## Corefile ConfigMap (same as normal)\n"
        "说明: CoreDNS 1/2 Running — 1个副本因内存不足OOMKilled CrashLoopBackOff\n"
        "      剩余副本处理全部DNS查询→过载超时。\n"
    ),
    "disk_io_and_dns": (
        "## CoreDNS Deployment\n"
        "Name:                   coredns\n"
        "Namespace:              kube-system\n"
        "Replicas:               2 desired | 2 available\n"
        "Strategy:               RollingUpdate (max unavailable 1)\n"
        "Conditions:             Available=True, Progressing=False\n"
        "Events: (无异常事件 — CoreDNS Pod全部健康)\n"
        "\n## kube-dns Service\n"
        "NAME       TYPE        CLUSTER-IP   EXTERNAL-IP   PORT(S)\n"
        "kube-dns   ClusterIP   10.0.0.10    <none>        53/UDP,53/TCP\n"
        "\n## Corefile ConfigMap\n"
        ".:53 {\n    errors\n    health\n    kubernetes cluster.local in-addr.arpa ip6.arpa\n    prometheus :9153\n    forward . 10.0.0.53 10.0.0.54\n    cache 30\n    loop\n    reload\n}\n"
        "说明: CoreDNS Deployment健康(2/2 Ready)，内部DNS解析正常(cluster.local)\n"
        "      但forward插件指向已下线DNS服务器(10.0.0.53/54不可达!)→外部域名解析全部超时\n"
        "      注意: 这是独立根因，与磁盘IO饱和无关—需分别修复\n"
    ),
}

# ═══════════════════════════════════════════════════════════════════
# Scenario data: Helm
# ═══════════════════════════════════════════════════════════════════

_HELM_RELEASES: dict[str, str] = {
    "normal": (
        "RELEASE         REVISION  STATUS      CHART                   NAMESPACE   AGE\n"
        "api-gateway     12        deployed    api-gateway-1.5.2       default     2026-05-10T08:00:00Z\n"
        "redis-cluster   5         deployed    redis-19.1.5            default     2026-03-15T10:00:00Z\n"
        "ingress-nginx   3         deployed    ingress-nginx-4.10.0    ingress     2026-04-01T06:00:00Z\n"
        "cert-manager    2         deployed    cert-manager-v1.14.0    cert-manager 2026-02-20T12:00:00Z\n"
        "monitoring      8         deployed    kube-prometheus-8.2.0   monitoring  2026-01-10T08:00:00Z\n"
    ),
    "container_crash": (
        "RELEASE         REVISION  STATUS      CHART                   NAMESPACE   AGE\n"
        "api-gateway     15        failed      api-gateway-1.5.3       default     2026-06-14T07:00:00Z (UPGRADE FAILED!)\n"
        "redis-cluster   5         deployed    redis-19.1.5            default     2026-03-15T10:00:00Z\n"
        "ingress-nginx   3         deployed    ingress-nginx-4.10.0    ingress     2026-04-01T06:00:00Z\n"
    ),
}

_HELM_HISTORY: dict[str, str] = {
    "normal": (
        "REVISION  STATUS    CHART               APP_VERSION  CREATED\n"
        "10        deployed  api-gateway-1.5.0    v2.1.0       2026-05-01T08:00:00Z\n"
        "11        deployed  api-gateway-1.5.1    v2.1.1       2026-05-05T10:00:00Z\n"
        "12        deployed  api-gateway-1.5.2    v2.1.2       2026-05-10T08:00:00Z\n"
    ),
    "container_crash": (
        "REVISION  STATUS    CHART               APP_VERSION  CREATED\n"
        "13        deployed  api-gateway-1.5.2    v2.1.2       2026-05-10T08:00:00Z\n"
        "14        deployed  api-gateway-1.5.3    v2.2.0       2026-06-14T06:00:00Z\n"
        "15        failed    api-gateway-1.5.3    v2.2.0       2026-06-14T07:00:00Z (upgrade failed!)\n"
    ),
}

_HELM_VALUES: dict[str, str] = {
    "normal": (
        "## api-gateway (revision 12)\n"
        "Chart: api-gateway-1.5.2\n"
        "Status: deployed\n"
        "Deployed: 2026-05-10T08:00:00Z\n\n"
        "### Values\n"
        "```yaml\n"
        "replicaCount: 3\n"
        "image:\n"
        "  repository: registry.example.com/api-gateway\n"
        "  tag: v2.1.2\n"
        "resources:\n"
        "  limits:\n"
        "    memory: \"512Mi\"\n"
        "    cpu: \"500m\"\n"
        "  requests:\n"
        "    memory: \"256Mi\"\n"
        "    cpu: \"250m\"\n"
        "```"
    ),
    "container_crash": (
        "## api-gateway (revision 15)\n"
        "Chart: api-gateway-1.5.3\n"
        "Status: failed\n"
        "Deployed: 2026-06-14T07:00:00Z\n\n"
        "### Values\n"
        "```yaml\n"
        "replicaCount: 3\n"
        "image:\n"
        "  repository: registry.example.com/api-gateway\n"
        "  tag: v2.2.0 (INCREASED memory footprint!)\n"
        "resources:\n"
        "  limits:\n"
        "    memory: \"512Mi\" (unchanged — v2.2.0 needs 650MiB!)\n"
        "    cpu: \"500m\"\n"
        "  requests:\n"
        "    memory: \"256Mi\"\n"
        "    cpu: \"250m\"\n"
        "```"
    ),
}

# ═══════════════════════════════════════════════════════════════════
# Scenario data: Service Endpoints
# ═══════════════════════════════════════════════════════════════════

_SERVICE_ENDPOINTS: dict[str, str] = {
    "normal": (
        "## Service\n"
        "NAME            TYPE        CLUSTER-IP    PORT(S)    AGE\n"
        "api-gateway     ClusterIP   10.0.100.1    8080/TCP   30d\n"
        "\n## EndpointSlices\n"
        "NAME                  ADDRESSTYPE  PORTS   ENDPOINTS                    AGE\n"
        "api-gateway-abc12     IPv4         8080    10.0.1.5, 10.0.2.8, 10.0.3.12   30d\n"
    ),
    "kubelet_disk_io_starvation": (
        "## Service\n"
        "NAME            TYPE        CLUSTER-IP    PORT(S)    AGE\n"
        "java-backend    ClusterIP   10.0.100.5    8080/TCP   30d\n"
        "\n## EndpointSlices\n"
        "NAME                  ADDRESSTYPE  PORTS   ENDPOINTS           AGE\n"
        "java-backend-abc12    IPv4         8080    10.0.2.8 (1/3!)    30d\n"
        "WARNING: 2 of 3 endpoints lost — worker-2 NotReady, Pods evicted!\n"
    ),
}

# ═══════════════════════════════════════════════════════════════════
# Scenario data: RBAC & Network Policy
# ═══════════════════════════════════════════════════════════════════

_RBAC: dict[str, str] = {
    "normal": (
        "## ClusterRoleBindings\n"
        "NAME                                    ROLE                                    AGE\n"
        "cluster-admin                           ClusterRole/cluster-admin              30d\n"
        "system:kube-scheduler                   ClusterRole/system:kube-scheduler      30d\n"
        "api-gateway-rolebinding                 ClusterRole/api-gateway-reader         30d\n"
    ),
}

_NETWORK_POLICIES: dict[str, str] = {
    "normal": (
        "NAMESPACE   NAME                   POD-SELECTOR     AGE\n"
        "default     deny-all-by-default    <none>           30d\n"
        "default     allow-api-gateway      app=api-gateway  30d\n"
    ),
}

# ═══════════════════════════════════════════════════════════════════
# Discovery tools
# ═══════════════════════════════════════════════════════════════════

@tool
def get_namespaces(cluster_name: str) -> str:
    """List all namespaces in a cluster. Call before tools that need namespace."""
    _ = cluster_name
    return (
        "NAME              STATUS   AGE\n"
        "default           Active   30d\n"
        "kube-system       Active   30d\n"
        "kube-public       Active   30d\n"
        "kube-node-lease   Active   30d\n"
        "ingress           Active   30d\n"
        "cert-manager      Active   30d\n"
        "monitoring        Active   30d\n"
        "argocd            Active   30d\n"
    )


# ═══════════════════════════════════════════════════════════════════
# Overview
# ═══════════════════════════════════════════════════════════════════

@tool
def get_cluster_overview(cluster_name: str) -> str:
    """Get high-level cluster overview: nodes, non-running pods, namespace count."""
    from diagnostics.tools.mock.data import kubernetes_nodes_data, kubernetes_pods_data

    scenario = get_active_scenario()
    nodes = kubernetes_nodes_data(scenario)
    pods = kubernetes_pods_data(scenario)
    return (
        f"## Cluster: {cluster_name}\n\n"
        f"### Nodes\n{nodes}\n\n"
        f"### Pods (including non-Running)\n{pods}\n\n"
        f"### Namespace count\n8"
    )


# ═══════════════════════════════════════════════════════════════════
# Pod Tools
# ═══════════════════════════════════════════════════════════════════

@tool
def get_pod_logs(cluster_name: str, namespace: str,
                 pod_name: str, tail_lines: int = 200) -> str:
    """Retrieve recent pod logs."""
    _ = cluster_name, namespace, pod_name, tail_lines
    return _POD_LOGS.get(get_active_scenario(), _POD_LOGS["normal"])


@tool
def get_pod_logs_since(cluster_name: str, namespace: str,
                       pod_name: str, minutes: int = 5) -> str:
    """Retrieve pod logs from the last N minutes."""
    _ = cluster_name, namespace, pod_name, minutes
    logs = _POD_LOGS.get(get_active_scenario(), _POD_LOGS["normal"])
    # Return last few lines as "recent"
    lines = logs.strip().split("\n")
    recent = lines[-min(4, len(lines)):]
    return f"(Last {minutes} minutes)\n" + "\n".join(recent)


@tool
def get_pod_logs_lines(cluster_name: str, namespace: str,
                       pod_name: str, head_lines: int = 50) -> str:
    """Retrieve the last N lines of pod logs."""
    _ = cluster_name, namespace, pod_name, head_lines
    logs = _POD_LOGS.get(get_active_scenario(), _POD_LOGS["normal"])
    lines = logs.strip().split("\n")
    return "\n".join(lines[-min(head_lines, len(lines)):])


@tool
def get_pod_previous_logs(cluster_name: str, namespace: str,
                          pod_name: str, tail_lines: int = 200) -> str:
    """Retrieve logs from the PREVIOUS (crashed) container instance."""
    _ = cluster_name, namespace, pod_name, tail_lines
    logs = _POD_PREVIOUS_LOGS.get(get_active_scenario())
    if logs:
        return f"## Previous Container Logs\n{logs}"
    return "[No previous container — pod has not restarted or previous logs unavailable]"


@tool
def describe_pod(cluster_name: str, namespace: str, pod_name: str) -> str:
    """Get full describe output for a pod."""
    _ = cluster_name, namespace, pod_name
    return _POD_DESCRIBE.get(get_active_scenario(), _POD_DESCRIBE["normal"])


@tool
def get_pod_events(cluster_name: str, namespace: str, pod_name: str) -> str:
    """Get events related to a specific pod."""
    _ = cluster_name, namespace, pod_name
    return _POD_EVENTS.get(get_active_scenario(), _POD_EVENTS["normal"])


# ═══════════════════════════════════════════════════════════════════
# Cluster Events
# ═══════════════════════════════════════════════════════════════════

@tool
def get_cluster_events(cluster_name: str, namespace: str = "") -> str:
    """Get recent cluster-wide or namespace-scoped events (last 30)."""
    _ = cluster_name, namespace
    scenario = get_active_scenario()
    events_map: dict[str, str] = {
        "normal": (
            "NAMESPACE   LAST SEEN   TYPE     REASON               MESSAGE\n"
            "default     1d          Normal   ScalingReplicaSet    Scaled up replica set nginx to 3\n"
            "default     3d          Normal   Scheduled            Successfully assigned pod to node\n"
        ),
        "container_crash": (
            "NAMESPACE   LAST SEEN   TYPE      REASON               MESSAGE\n"
            "default     2m          Warning   OOMKilled            Container java-backend exceeded memory limit 512Mi\n"
            "default     2m          Warning   BackOff              Back-off restarting failed container\n"
            "default     5m          Warning   OOMKilled            (repeat 12x in last hour)\n"
            "default     10m         Normal    Scheduled            Successfully assigned default/java-backend-abc12\n"
        ),
        "conntrack_table_full": (
            "NAMESPACE   LAST SEEN   TYPE      REASON                    MESSAGE\n"
            "default     1m          Warning   ReadinessProbeFailed      Readiness probe failed: DNS lookup timeout\n"
            "default     1m          Warning   LivenessProbeFailed       Liveness probe failed: connection refused\n"
            "default     3m          Warning   ReadinessProbeFailed      (repeat — conntrack table full dropping UDP DNS)\n"
        ),
        "kubelet_disk_io_starvation": (
            "NAMESPACE   LAST SEEN   TYPE      REASON               MESSAGE\n"
            "default     35m         Warning   NodeNotReady         Node worker-2 status is Unknown(NotReady)\n"
            "default     35m         Warning   Evicted              Pod java-backend-def34 evicted — node lost heartbeat\n"
            "default     40m         Warning   NodeHasDiskPressure  Node worker-2 has DiskPressure\n"
        ),
        "node_oom_kubelet_killed": (
            "NAMESPACE   LAST SEEN   TYPE      REASON               MESSAGE\n"
            "default     30m         Warning   NodeStatusUnknown    Node worker-2 stopped posting node status\n"
            "default     30m         Warning   Unreachable          Pods on worker-2 unreachable — kubelet killed by OOM\n"
            "default     35m         Warning   MemoryPressure       Node worker-2 has MemoryPressure\n"
        ),
        "cpu_throttle_probe_failure": (
            "NAMESPACE   LAST SEEN   TYPE      REASON               MESSAGE\n"
            "default     1m          Warning   LivenessProbeFailed  Liveness probe timeout (http-get 5s)\n"
            "default     2m          Warning   PLEGNotHealthy       kubelet PLEG is not healthy (CPU bound)\n"
            "default     5m          Warning   Killing              Killing container (failed probe #8 in 2d)\n"
        ),
        "multi_layer_cascading": (
            "NAMESPACE     LAST SEEN   TYPE      REASON               MESSAGE\n"
            "default       1m          Warning   OOMKilled            api-gateway OOMKilled (RSS 650MiB > limit 512MiB)\n"
            "default       1m          Warning   BackOff              CrashLoopBackOff #18\n"
            "default       5m          Warning   NodeStatusUnknown    Node worker-3 status Unknown\n"
            "default       5m          Warning   DNSResolutionFailed  Intermittent DNS — CoreDNS only 1/3 replicas\n"
            "kube-system   5m          Warning   MinimumReplicasUnavailable  CoreDNS deployment 1/3 available\n"
        ),
        "etcd_quota_near_full": (
            "NAMESPACE     LAST SEEN   TYPE      REASON               MESSAGE\n"
            "kube-system   2m          Warning   NOSPACE              etcd cluster has limited space: 7.15GB/8GB (89%)\n"
            "default       30s         Warning   ReadinessProbeFailed api-gateway readiness probe HTTP 8080/ready timeout\n"
            "default       30s         Warning   BackOff              CrashLoopBackOff — restart triggered by etcd timeout\n"
            "default       2m          Warning   LivenessProbeFailed  api-gateway liveness probe failed (etcd wait)\n"
            "kube-system   3m          Warning   NOSPACE              etcd database size approaching quota (all 3 members)\n"
        ),
        "oom_score_misconfig": (
            "NAMESPACE   LAST SEEN   TYPE      REASON               MESSAGE\n"
            "default     35m         Warning   NodeStatusUnknown    Node worker-2 stopped posting status (kubelet killed)\n"
            "default     35m         Warning   OOMKillMisconfig     OOM Killer killed kubelet (oom_score_adj=-500) instead of java-app (750)\n"
            "default     30m         Warning   Unreachable          Pods on worker-2 unreachable\n"
        ),
        "memory_leak_and_disk_full": (
            "NAMESPACE   LAST SEEN   TYPE      REASON               MESSAGE\n"
            "default     15m         Warning   OOMKilled            java-backend OOMKilled (RSS=6.8GB — off-heap leak)\n"
            "default     15m         Warning   DiskPressure         /dev/sda1 100% full — ext4 journal abort\n"
            "default     10m         Warning   BackOff              CrashLoopBackOff #8 (disk full prevents log writes)\n"
        ),
        "coredns_cache_poison": (
            "NAMESPACE   LAST SEEN   TYPE      REASON               MESSAGE\n"
            "default     30m         Warning   DNSResolutionError   Service resolved to stale IP 10.0.1.88 (terminated Pod)\n"
            "default     30m         Warning   ConnectionRefused    Connection to 10.0.1.88 refused — stale endpoint\n"
        ),
        "etcd_quorum_loss": (
            "NAMESPACE     LAST SEEN   TYPE      REASON               MESSAGE\n"
            "kube-system   5m          Warning   CrashLoopBackOff     etcd-master-1 CrashLoopBackOff (fsync latency 250ms!)\n"
            "kube-system   5m          Warning   CrashLoopBackOff     etcd-master-2 CrashLoopBackOff (disk IO hang!)\n"
            "kube-system   5m          Warning   QuorumLost           etcd cluster lost quorum (1/3 members alive)\n"
            "default       3m          Warning   FailedCreate         API Server read-only — cannot create/update resources\n"
        ),
        "disk_io_and_dns": (
            "NAMESPACE     LAST SEEN   TYPE      REASON               MESSAGE\n"
            "default       40m         Warning   IOTimeout            Write I/O timeout 30s (disk saturated by logrotate)\n"
            "default       35m         Warning   DNSResolutionFailed  UnknownHostException: upstream-db.external.com\n"
            "kube-system   30m         Warning   DNSForwardTimeout    CoreDNS forward timeout: upstream 10.0.0.53:53 unreachable\n"
            "kube-system   25m         Warning   DNSForwardTimeout    CoreDNS forward timeout: upstream 10.0.0.54:53 unreachable\n"
        ),
        "dns_and_etcd": (
            "NAMESPACE     LAST SEEN   TYPE      REASON               MESSAGE\n"
            "default       1m          Warning   DNSLookupTimeout     user-svc DNS lookup timeout (CoreDNS CrashLoopBackOff)\n"
            "kube-system   2m          Warning   CrashLoopBackOff     CoreDNS pod coredns-6d4b-def88 OOMKilled (memory 64Mi limit)\n"
            "kube-system   3m          Warning   EtcdLeaderChanged    etcd leader changed (master-1 unstable — 8x in 1h)\n"
        ),
        "image_pull_backoff": (
            "NAMESPACE   LAST SEEN   TYPE      REASON               MESSAGE\n"
            "default     30m         Warning   Failed               Failed to pull image \"node:18-alpine\": 429 Too Many Requests\n"
            "default     25m         Warning   BackOff              ImagePullBackOff — Docker Hub rate limit (100 pulls/6h)\n"
        ),
        "conntrack_and_oom": (
            "NAMESPACE   LAST SEEN   TYPE      REASON               MESSAGE\n"
            "default     15m         Warning   OOMKilled            api-gateway OOMKilled (java RSS=6.4GB, off-heap leak!)\n"
            "default     10m         Warning   DNSLookupTimeout     Readiness probe: DNS timeout (conntrack UDP drop)\n"
            "default     5m          Warning   BackOff              CrashLoopBackOff #8\n"
            "default     2m          Warning   OOMKilled            Repeat kill — java process memory climbing\n"
        ),
    }
    return events_map.get(scenario, events_map["normal"])


# ═══════════════════════════════════════════════════════════════════
# Node Tools
# ═══════════════════════════════════════════════════════════════════

@tool
def get_node_info(cluster_name: str, node_name: str) -> str:
    """Get detailed node information."""
    _ = cluster_name, node_name
    scenario = get_active_scenario()
    node_info_map: dict[str, str] = {
        "normal": (
            f"Name:               {node_name}\n"
            "Roles:              worker\n"
            "Status:             Ready\n"
            "OS:                 Ubuntu 22.04 LTS\n"
            "Kernel:             5.15.0-105-generic\n"
            "Kubelet Version:    v1.28.5\n"
            "Container Runtime:  containerd://1.7.13\n"
            "Conditions:         Ready=True, MemoryPressure=False, DiskPressure=False, PIDPressure=False\n"
            "Allocated resources: CPU=65%, Memory=60%\n"
        ),
        "kubelet_disk_io_starvation": (
            f"Name:               {node_name}\n"
            "Roles:              worker\n"
            "Status:             NotReady (Reason: NodeStatusUnknown)\n"
            "Conditions:         Ready=Unknown, DiskPressure=Unknown, MemoryPressure=Unknown\n"
            "Kubelet Last Heartbeat: 3m48s ago (timeout!)\n"
            "Kubelet Logs:       W0624... Error updating node lease, connect: disk IO timeout\n"
            "Events:             Node has lost heartbeat — kubelet lease renewal blocked by disk IO\n"
        ),
        "node_oom_kubelet_killed": (
            f"Name:               {node_name}\n"
            "Roles:              worker\n"
            "Status:             NotReady (Reason: NodeStatusUnknown)\n"
            "Conditions:         Ready=Unknown, MemoryPressure=Unknown, DiskPressure=Unknown\n"
            "Kubelet Last Heartbeat: 5m12s ago (extended outage!)\n"
            "Kubelet Status:     Process killed by OOM Killer (PID 3210)\n"
            "dmesg:              oom-killer killed process kubelet (total-vm:4194304kB)\n"
        ),
        "oom_score_misconfig": (
            f"Name:               {node_name}\n"
            "Roles:              worker\n"
            "Status:             NotReady (Reason: NodeStatusUnknown)\n"
            "Conditions:         Ready=Unknown, MemoryPressure=True\n"
            "Kubelet Status:     Process killed by OOM Killer (PID 3210)\n"
            "Kubelet oom_score_adj: -500 (should be protected but WAS killed!)\n"
            "App Pod oom_score_adj: 750 (survived — misconfig confirmed!)\n"
            "dmesg:              oom-killer killed kubelet instead of java-app (oom_score_adj inversion!)\n"
        ),
        "conntrack_and_oom": (
            f"Name:               {node_name}\n"
            "Roles:              worker\n"
            "Status:             Ready (but under severe memory pressure)\n"
            "Conditions:         Ready=True, MemoryPressure=True\n"
            "Kernel:             conntrack table 262144/262144 (FULL!)\n"
            "OOM Events:         8 OOM kills in 2 days (java off-heap leak)\n"
            "dmesg:              conntrack table full, dropping packets + java OOM killed\n"
        ),
        "multi_layer_cascading": (
            f"Name:               {node_name}\n"
            "Roles:              worker\n"
            "Status:             NotReady (worker-3 — OOM Killer killed kubelet)\n"
            "Conditions:         Ready=Unknown, MemoryPressure=True, DiskPressure=False\n"
            "Kubelet Status:     Process killed by OOM Killer\n"
            "Pods on this node:  All Unknown (2 CoreDNS replicas lost!)\n"
        ),
    }
    return node_info_map.get(scenario, node_info_map["normal"])


@tool
def get_node_conditions(cluster_name: str) -> str:
    """Get a quick overview of all node conditions."""
    _ = cluster_name
    from diagnostics.tools.mock.data import kubernetes_nodes_data
    return kubernetes_nodes_data(get_active_scenario())


@tool
def get_node_resource_usage(cluster_name: str) -> str:
    """Get node CPU/Memory usage (requires metrics-server)."""
    _ = cluster_name
    scenario = get_active_scenario()
    usage_map: dict[str, str] = {
        "normal": (
            "NAME         CPU(cores)   CPU%   MEMORY(bytes)   MEMORY%\n"
            "master-1     650m         16%    1890Mi          45%\n"
            "worker-1     1250m        31%    2310Mi          55%\n"
            "worker-2     1480m        37%    2520Mi          60%\n"
        ),
        "kubelet_disk_io_starvation": (
            "NAME         CPU(cores)   CPU%   MEMORY(bytes)   MEMORY%\n"
            "worker-2     N/A         N/A    N/A              N/A     (metrics-server unreachable — node NotReady)\n"
        ),
        "node_oom_kubelet_killed": (
            "NAME         CPU(cores)   CPU%   MEMORY(bytes)   MEMORY%\n"
            "worker-2     N/A         N/A    N/A              N/A     (node unreachable — kubelet killed by OOM)\n"
        ),
        "cpu_throttle_probe_failure": (
            "NAME         CPU(cores)   CPU%   MEMORY(bytes)   MEMORY%\n"
            "worker-1     3950m        98%    2730Mi          65%     (CPU nearly saturated!)\n"
            "worker-2     3750m        93%    2520Mi          60%     (CPU nearly saturated!)\n"
        ),
    }
    return usage_map.get(scenario, usage_map["normal"])


# ═══════════════════════════════════════════════════════════════════
# Pod Resource Usage
# ═══════════════════════════════════════════════════════════════════

@tool
def get_pod_resource_usage(cluster_name: str, namespace: str = "") -> str:
    """Get pod CPU/Memory usage (requires metrics-server)."""
    _ = cluster_name, namespace
    scenario = get_active_scenario()
    usage: dict[str, str] = {
        "normal": (
            "NAMESPACE     NAME                     CPU(cores)   MEMORY(bytes)\n"
            "default       nginx-7d8f-abc12         5m           45Mi\n"
            "default       redis-5c8b-ghi56         15m          120Mi\n"
        ),
        "container_crash": (
            "NAMESPACE     NAME                     CPU(cores)   MEMORY(bytes)\n"
            "default       java-backend-ghi56       45m          480Mi    (93% of 512Mi limit!)\n"
            "default       nginx-7d8b-jkl78         5m           45Mi\n"
        ),
        "conntrack_table_full": (
            "NAMESPACE     NAME                     CPU(cores)   MEMORY(bytes)\n"
            "default       api-gateway-abc12        55m          220Mi    (high connections)\n"
            "default       api-gateway-def34        48m          210Mi\n"
        ),
        "etcd_quota_near_full": (
            "NAMESPACE     NAME                     CPU(cores)   MEMORY(bytes)\n"
            "default       api-gateway-ghi56        25m          420Mi    (82% of 512Mi limit — normal!)\n"
            "default       backend-service-xyz12    15m          350Mi\n"
            "default       nginx-7d8b-jkl78         5m           45Mi\n"
        ),
        "conntrack_and_oom": (
            "NAMESPACE     NAME                     CPU(cores)   MEMORY(bytes)\n"
            "default       api-gateway-abc12        85m          5.9Gi    (98% of 6Gi limit! off-heap leak!)\n"
            "default       api-gateway-def34        72m          5.6Gi    (93% of 6Gi limit — climbing!)\n"
            "default       backend-svc-xyz12        15m          350Mi\n"
        ),
    }
    return usage.get(scenario, usage["normal"])


@tool
def get_pod_restart_counts(cluster_name: str, namespace: str = "") -> str:
    """Get pods with high restart counts (>=5 restarts)."""
    _ = cluster_name, namespace
    scenario = get_active_scenario()
    restart_map: dict[str, str] = {
        "normal": (
            "NAMESPACE  NAME                 RESTARTS  STATUS    NODE\n"
            "(No pods with ≥5 restarts)\n"
        ),
        "container_crash": (
            "NAMESPACE  NAME                 RESTARTS  STATUS             NODE\n"
            "default    java-backend-abc12   12        CrashLoopBackOff   worker-2 (total: 12)\n"
            "default    java-backend-def34   8         OOMKilled          worker-2 (total: 8)\n"
            "(Showing pods with ≥5 restarts, last 20)\n"
        ),
        "conntrack_table_full": (
            "NAMESPACE  NAME                 RESTARTS  STATUS    NODE\n"
            "default    api-gateway-abc12    5         Running   worker-2 (total: 5)\n"
            "default    api-gateway-def34    3         Running   worker-2 (total: 3 — also frequent)\n"
            "(Showing pods with ≥5 restarts, last 20)\n"
        ),
        "cpu_throttle_probe_failure": (
            "NAMESPACE  NAME                 RESTARTS  STATUS    NODE\n"
            "default    java-backend-abc12   8         Running   worker-1 (total: 8 — probe timeout kills)\n"
            "default    java-backend-def34   6         Running   worker-2 (total: 6)\n"
            "(Showing pods with ≥5 restarts, last 20)\n"
        ),
        "multi_layer_cascading": (
            "NAMESPACE  NAME                 RESTARTS  STATUS             NODE\n"
            "default    api-gateway-abc12    18        CrashLoopBackOff   worker-1 (total: 18 — cgroup OOM!)\n"
            "(Showing pods with ≥5 restarts, last 20)\n"
        ),
        "etcd_quota_near_full": (
            "NAMESPACE  NAME                 RESTARTS  STATUS             NODE\n"
            "default    api-gateway-abc12    8         CrashLoopBackOff   worker-1 (total: 8 — etcd timeout kills)\n"
            "default    api-gateway-def34    6         CrashLoopBackOff   worker-2 (total: 6 — etcd timeout kills)\n"
            "(Showing pods with ≥5 restarts, last 20)\n"
        ),
        "oom_score_misconfig": (
            "NAMESPACE  NAME                 RESTARTS  STATUS    NODE\n"
            "(No pods with ≥5 restarts — kubelet was killed, not pods)\n"
        ),
        "memory_leak_and_disk_full": (
            "NAMESPACE  NAME                 RESTARTS  STATUS             NODE\n"
            "default    java-backend-abc12   8         OOMKilled          worker-1 (total: 8 — off-heap leak + disk full)\n"
            "(Showing pods with ≥5 restarts, last 20)\n"
        ),
        "etcd_quorum_loss": (
            "NAMESPACE     NAME                 RESTARTS  STATUS             NODE\n"
            "kube-system   etcd-master-1        12        CrashLoopBackOff   master-1 (total: 12 — disk IO crash!)\n"
            "kube-system   etcd-master-2        8         CrashLoopBackOff   master-2 (total: 8 — disk IO hang!)\n"
            "(Showing pods with ≥5 restarts, last 20)\n"
        ),
        "dns_and_etcd": (
            "NAMESPACE     NAME                 RESTARTS  STATUS             NODE\n"
            "kube-system   coredns-6d4b-def88   8         CrashLoopBackOff   worker-2 (total: 8 — OOMKilled 64Mi!)\n"
            "default       user-svc-abc12       5         CrashLoopBackOff   worker-2 (total: 5)\n"
            "(Showing pods with ≥5 restarts, last 20)\n"
        ),
        "image_pull_backoff": (
            "NAMESPACE  NAME                 RESTARTS  STATUS    NODE\n"
            "(No pods with ≥5 restarts — ImagePullBackOff, no crashes)\n"
        ),
    }
    return restart_map.get(scenario, restart_map["normal"])


# ═══════════════════════════════════════════════════════════════════
# System Pods
# ═══════════════════════════════════════════════════════════════════

@tool
def get_system_pods(cluster_name: str) -> str:
    """Get all system pods in kube-system namespace."""
    _ = cluster_name
    scenario = get_active_scenario()
    system_pods: dict[str, str] = {
        "normal": (
            "NAMESPACE     NAME                              READY   STATUS    RESTARTS   AGE\n"
            "kube-system   coredns-6d4b-xyz11                 1/1     Running   0          30d\n"
            "kube-system   coredns-6d4b-abc99                 1/1     Running   0          30d\n"
            "kube-system   etcd-master-1                      1/1     Running   0          30d\n"
            "kube-system   kube-apiserver-master-1             1/1     Running   0          30d\n"
            "kube-system   kube-controller-manager-master-1    1/1     Running   0          30d\n"
            "kube-system   kube-scheduler-master-1             1/1     Running   0          30d\n"
            "kube-system   kube-proxy-lmn99                    1/1     Running   0          30d\n"
        ),
        "multi_layer_cascading": (
            "NAMESPACE     NAME                              READY   STATUS      RESTARTS   AGE\n"
            "kube-system   coredns-6d4b-xyz11                 1/1     Running     0          30d (worker-1, only healthy!)\n"
            "kube-system   coredns-6d4b-abc99                 0/1     Unknown     0          30d (worker-3, node NotReady!)\n"
            "kube-system   coredns-6d4b-def88                 0/1     Unknown     0          30d (worker-3, node NotReady!)\n"
            "kube-system   etcd-master-1                      1/1     Running     5          30d (frequent restarts — disk IO!)\n"
            "kube-system   kube-apiserver-master-1             1/1     Running     1          30d\n"
            "kube-system   kube-controller-manager-master-1    1/1     Running     1          30d\n"
            "kube-system   kube-scheduler-master-1             1/1     Running     0          30d\n"
        ),
        "etcd_quota_near_full": (
            "NAMESPACE     NAME                              READY   STATUS    RESTARTS   AGE\n"
            "kube-system   coredns-6d4b-xyz11                 1/1     Running   0          30d (正常)\n"
            "kube-system   coredns-6d4b-abc99                 1/1     Running   0          30d (正常)\n"
            "kube-system   etcd-master-1                      1/1     Running   0          30d (NOSPACE alarm!)\n"
            "kube-system   etcd-master-2                      1/1     Running   0          30d (NOSPACE alarm!)\n"
            "kube-system   etcd-master-3                      1/1     Running   0          30d (NOSPACE alarm!)\n"
            "kube-system   kube-apiserver-master-1             1/1     Running   0          30d (写入延迟p99=850ms)\n"
            "kube-system   kube-controller-manager-master-1    1/1     Running   0          30d\n"
            "kube-system   kube-scheduler-master-1             1/1     Running   0          30d\n"
            "kube-system   kube-proxy-lmn99                    1/1     Running   0          30d\n"
        ),
        "etcd_quorum_loss": (
            "NAMESPACE     NAME                              READY   STATUS             RESTARTS   AGE\n"
            "kube-system   etcd-master-1                      0/1     CrashLoopBackOff   12         30d (disk IO hang!)\n"
            "kube-system   etcd-master-2                      0/1     CrashLoopBackOff   8          30d (disk IO hang!)\n"
            "kube-system   etcd-master-3                      1/1     Running            0          30d (only survivor)\n"
            "kube-system   coredns-6d4b-xyz11                 1/1     Running            0          30d\n"
            "kube-system   coredns-6d4b-abc99                 1/1     Running            0          30d\n"
            "kube-system   kube-apiserver-master-1             1/1     Running            0          30d (read-only mode!)\n"
            "kube-system   kube-controller-manager-master-1    1/1     Running            0          30d\n"
            "kube-system   kube-scheduler-master-1             1/1     Running            0          30d\n"
            "kube-system   kube-proxy-lmn99                    1/1     Running            0          30d\n"
        ),
        "dns_and_etcd": (
            "NAMESPACE     NAME                              READY   STATUS             RESTARTS   AGE\n"
            "kube-system   coredns-6d4b-xyz11                 1/1     Running            0          30d (worker-1, healthy)\n"
            "kube-system   coredns-6d4b-def88                 0/1     CrashLoopBackOff   8          30d (OOMKilled 64Mi!)\n"
            "kube-system   etcd-master-1                      1/1     Running            8          30d (leader election频繁!)\n"
            "kube-system   etcd-master-2                      1/1     Running            3          30d\n"
            "kube-system   etcd-master-3                      1/1     Running            0          30d\n"
            "kube-system   kube-apiserver-master-1             1/1     Running            1          30d (etcd慢导致间歇延迟)\n"
            "kube-system   kube-controller-manager-master-1    1/1     Running            0          30d\n"
            "kube-system   kube-scheduler-master-1             1/1     Running            0          30d\n"
            "kube-system   kube-proxy-lmn99                    1/1     Running            0          30d\n"
        ),
        "coredns_cache_poison": (
            "NAMESPACE     NAME                              READY   STATUS    RESTARTS   AGE\n"
            "kube-system   coredns-6d4b-xyz11                 1/1     Running   0          30d (正常 — 缓存问题非Pod故障)\n"
            "kube-system   coredns-6d4b-abc99                 1/1     Running   0          30d (正常)\n"
            "kube-system   etcd-master-1                      1/1     Running   0          30d\n"
            "kube-system   etcd-master-2                      1/1     Running   0          30d\n"
            "kube-system   etcd-master-3                      1/1     Running   0          30d\n"
            "kube-system   kube-apiserver-master-1             1/1     Running   0          30d\n"
            "kube-system   kube-controller-manager-master-1    1/1     Running   0          30d\n"
            "kube-system   kube-scheduler-master-1             1/1     Running   0          30d\n"
            "kube-system   kube-proxy-lmn99                    1/1     Running   0          30d\n"
        ),
        "image_pull_backoff": (
            "NAMESPACE     NAME                              READY   STATUS    RESTARTS   AGE\n"
            "kube-system   coredns-6d4b-xyz11                 1/1     Running   0          30d\n"
            "kube-system   coredns-6d4b-abc99                 1/1     Running   0          30d\n"
            "kube-system   etcd-master-1                      1/1     Running   0          30d\n"
            "kube-system   etcd-master-2                      1/1     Running   0          30d\n"
            "kube-system   etcd-master-3                      1/1     Running   0          30d\n"
            "kube-system   kube-apiserver-master-1             1/1     Running   0          30d\n"
            "kube-system   kube-controller-manager-master-1    1/1     Running   0          30d\n"
            "kube-system   kube-scheduler-master-1             1/1     Running   0          30d\n"
            "kube-system   kube-proxy-lmn99                    1/1     Running   0          30d\n"
            "说明: 所有系统Pod正常 — ImagePullBackOff仅影响用户namespace的新Pod部署\n"
        ),
    }
    return system_pods.get(scenario, system_pods["normal"])


# ═══════════════════════════════════════════════════════════════════
# CoreDNS
# ═══════════════════════════════════════════════════════════════════

@tool
def get_coredns_logs(cluster_name: str, tail_lines: int = 200,
                     since_minutes: int = 0) -> str:
    """Get logs from all CoreDNS pods in kube-system."""
    _ = cluster_name, tail_lines, since_minutes
    return _COREDNS_LOGS.get(get_active_scenario(), _COREDNS_LOGS["normal"])


@tool
def describe_coredns(cluster_name: str) -> str:
    """Describe CoreDNS deployment and show Corefile ConfigMap in kube-system."""
    _ = cluster_name
    return _COREDNS_DESCRIBE.get(get_active_scenario(), _COREDNS_DESCRIBE["normal"])


# ═══════════════════════════════════════════════════════════════════
# Helm 3
# ═══════════════════════════════════════════════════════════════════

@tool
def list_helm_releases(cluster_name: str, namespace: str = "") -> str:
    """List all Helm 3 releases by querying release secrets."""
    _ = cluster_name, namespace
    return _HELM_RELEASES.get(get_active_scenario(), _HELM_RELEASES["normal"])


@tool
def get_helm_release_history(cluster_name: str, release_name: str,
                              namespace: str = "default", max_revisions: int = 10) -> str:
    """Get revision history of a Helm release."""
    _ = cluster_name, release_name, namespace, max_revisions
    return _HELM_HISTORY.get(get_active_scenario(), _HELM_HISTORY["normal"])


@tool
def get_helm_release_values(cluster_name: str, release_name: str,
                             namespace: str = "default", revision: int = 0) -> str:
    """Extract values from a Helm release secret."""
    _ = cluster_name, release_name, namespace, revision
    return _HELM_VALUES.get(get_active_scenario(), _HELM_VALUES["normal"])


# ═══════════════════════════════════════════════════════════════════
# etcd
# ═══════════════════════════════════════════════════════════════════

@tool
def get_etcd_status(cluster_name: str) -> str:
    """Get etcd cluster status: pod health, member list, leader info."""
    _ = cluster_name
    return _ETCD_STATUS.get(get_active_scenario(), _ETCD_STATUS["normal"])


@tool
def get_etcd_logs(cluster_name: str, tail_lines: int = 200,
                  since_minutes: int = 0) -> str:
    """Get recent logs from all etcd pods."""
    _ = cluster_name, tail_lines, since_minutes
    return _ETCD_LOGS.get(get_active_scenario(), _ETCD_LOGS["normal"])


@tool
def check_etcd_health(cluster_name: str) -> str:
    """Check etcd endpoint health, alarm list, and Raft leader changes."""
    _ = cluster_name
    return _ETCD_HEALTH.get(get_active_scenario(), _ETCD_HEALTH["normal"])


@tool
def get_etcd_metrics(cluster_name: str) -> str:
    """Get key etcd metrics: disk backend commit duration, Raft proposals, WAL fsync."""
    _ = cluster_name
    return _ETCD_METRICS.get(get_active_scenario(), _ETCD_METRICS["normal"])


# ═══════════════════════════════════════════════════════════════════
# Services & Configuration
# ═══════════════════════════════════════════════════════════════════

@tool
def check_service_endpoints(cluster_name: str, service_name: str,
                            namespace: str = "default") -> str:
    """Check whether a Service has healthy endpoints."""
    _ = cluster_name, service_name, namespace
    return _SERVICE_ENDPOINTS.get(get_active_scenario(), _SERVICE_ENDPOINTS["normal"])


@tool
def get_configmap(cluster_name: str, configmap_name: str,
                  namespace: str = "default") -> str:
    """Get ConfigMap content."""
    _ = cluster_name, namespace
    configmaps: dict[str, str] = {
        "normal": f"## ConfigMap: {configmap_name}\napiVersion: v1\nkind: ConfigMap\ndata:\n  config.yaml: |\n    server.port: 8080\n    log.level: info",
    }
    return configmaps.get(get_active_scenario(), configmaps["normal"])


@tool
def list_namespace_resources(cluster_name: str, namespace: str = "default") -> str:
    """List all resources in a namespace."""
    _ = cluster_name, namespace
    from diagnostics.tools.mock.data import kubernetes_pods_data
    pods = kubernetes_pods_data(get_active_scenario())
    return (
        "NAME                                    READY   STATUS             RESTARTS   AGE\n"
        + pods
    )


@tool
def get_pv_pvc_status(cluster_name: str, namespace: str = "") -> str:
    """Get PV/PVC status."""
    _ = cluster_name, namespace
    return (
        "## PersistentVolumes\n"
        "NAME        CAPACITY   ACCESS MODES   STATUS   CLAIM                    AGE\n"
        "pv-data-1   100Gi      RWO            Bound    default/data-claim-1     30d\n"
        "pv-data-2   50Gi       RWO            Bound    default/data-claim-2     30d\n"
        "\n## PersistentVolumeClaims\n"
        "NAME            STATUS   VOLUME       CAPACITY   AGE\n"
        "data-claim-1    Bound    pv-data-1    100Gi      30d\n"
        "data-claim-2    Bound    pv-data-2    50Gi       30d\n"
    )


@tool
def get_ingress_status(cluster_name: str, namespace: str = "") -> str:
    """Get Ingress resources and backend status."""
    _ = cluster_name, namespace
    return (
        "NAMESPACE   NAME            CLASS   HOSTS              ADDRESS        AGE\n"
        "default     api-ingress     nginx   api.example.com    10.0.200.100   30d\n"
    )


# ═══════════════════════════════════════════════════════════════════
# Security
# ═══════════════════════════════════════════════════════════════════

@tool
def get_network_policies(cluster_name: str, namespace: str = "") -> str:
    """List NetworkPolicies affecting pod-to-pod communication."""
    _ = cluster_name, namespace
    return _NETWORK_POLICIES.get(get_active_scenario(), _NETWORK_POLICIES["normal"])


@tool
def check_rbac_permissions(cluster_name: str, namespace: str = "") -> str:
    """List RBAC roles, rolebindings, and clusterroles."""
    _ = cluster_name, namespace
    return _RBAC.get(get_active_scenario(), _RBAC["normal"])


@tool
def check_certificate_expiry(cluster_name: str) -> str:
    """Check TLS certificate expiry for kube-apiserver, kubelet, and etcd."""
    _ = cluster_name
    return (
        "CERTIFICATE                    EXPIRY                  REMAINING\n"
        "kube-apiserver.crt             2027-01-15T03:14:07Z    215 days\n"
        "kube-apiserver-kubelet-client  2027-01-15T03:14:07Z    215 days\n"
        "etcd-server.crt                2027-01-15T03:14:07Z    215 days\n"
        "etcd-peer.crt                  2027-01-15T03:14:07Z    215 days\n"
        "All certificates valid — no expiry warnings.\n"
        "[PLACEHOLDER: Full certificate chain validation via cert inspection API]"
    )


@tool
def check_webhook_status(cluster_name: str) -> str:
    """List MutatingWebhookConfigurations and ValidatingWebhookConfigurations."""
    _ = cluster_name
    return (
        "## MutatingWebhookConfigurations\n"
        "NAME                     WEBHOOKS   AGE\n"
        "istio-sidecar-injector   1          30d\n"
        "cert-manager-webhook     1          30d\n"
        "\n## ValidatingWebhookConfigurations\n"
        "NAME                     WEBHOOKS   AGE\n"
        "gatekeeper-validating    3          30d\n"
        "cert-manager-webhook     1          30d\n"
    )


# ═══════════════════════════════════════════════════════════════════
# High-level K8s tools (from original kubernetes_tools.py)
# ═══════════════════════════════════════════════════════════════════

@tool
def check_kubernetes_pods(namespace: str = "default") -> str:
    """Check Kubernetes pod statuses, restart counts, and OOM events.

    Args:
        namespace: Kubernetes namespace (default: "default")
    """
    pods = kubernetes_pods_data(get_active_scenario())
    return f"Namespace: {namespace}\n{pods}"


@tool
def check_kubernetes_nodes() -> str:
    """Check Kubernetes node statuses, resource usage, and conditions (MemoryPressure, etc.)."""
    return kubernetes_nodes_data(get_active_scenario())


@tool
def check_kubernetes_control_plane() -> str:
    """Check Kubernetes control plane health: kube-apiserver, etcd, kube-scheduler, kube-controller-manager."""
    return kubernetes_control_plane_data(get_active_scenario())
