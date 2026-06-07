---
name: mtu-misconfig-diagnosis
description: Diagnoses MTU mismatch between CNI overlay and host network interface — when large packets are silently dropped due to MTU fragmentation failures while small packets work fine. Use when TCP connections hang intermittently, file transfers fail, or MySQL replication breaks with truncated data.
---

# MTU Misconfiguration Diagnosis

## Trigger
- SSH and small HTTP responses work, but file transfers or large API payloads hang
- Pod-to-pod TCP retransmit rate abnormally high but interface has zero drops
- `ping -M do -s 1472` works, `ping -M do -s 1473` fails (ICMP frag-needed blocked)
- MySQL replication breaks on binlog events > MTU
- CNI is overlay-based (VXLAN, IPIP, Geneve)

## Workflow

1. **check_network()** — look for high TCP retransmit rate with interface drops=0
2. **check_processes()** — identify applications sending large payloads
3. **Verify MTU chain**:
   - Host eth0: `ip link show eth0 | grep mtu` → should be 1500
   - Pod eth0: `ip link show eth0 | grep mtu` (inside pod) → should match CNI
   - CNI config: Calico 1440, Flannel VXLAN 1450, Cilium 1450
   - Path MTU discovery: `ping -M do -s <size> <target>` to find breaking point

## MTU Math

```
Host eth0 MTU:        1500
  - VXLAN overhead:     50 bytes (outer IP + UDP + VXLAN header)
  - Calico IPIP:        20 bytes (outer IP header)
  - Geneve:             54 bytes

Correct CNI MTU = Host MTU - overlay overhead
For VXLAN:  1500 - 50 = 1450
For IPIP:   1500 - 20 = 1480
```

## Root Causes
- `FELIX_MTUIFACEPATTERN` (Calico) or `--mtu` (Flannel) not explicitly set → auto-detect picks wrong value
- ICMP "fragmentation needed" messages blocked by iptables at host or cloud firewall level
- Jumbo frames (MTU 9000) on host not reflected in CNI config
- Mixed MTU across nodes (different NIC models or cloud instance types)

## Fix Options
- Set CNI MTU explicitly: `calicoctl patch felixconfiguration default --patch '{"spec":{"mtuIfacePattern":"eth0","ipipMTU":"1440"}}'`
- Allow ICMP type 3 code 4 (fragmentation-needed) through host iptables
- Test with `ping -M do -s 1448 <target-ip>` to verify fix
- For cloud environments, use cloud provider's documented CNI MTU settings
