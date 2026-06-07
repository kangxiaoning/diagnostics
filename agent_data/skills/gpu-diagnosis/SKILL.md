---
name: gpu-diagnosis
description: >
  Diagnose GPU issues: CUDA OOM, thermal/power throttling, utilization gaps,
  ECC errors, PCIe bottlenecks. Use when GPU training/inference workloads
  fail or underperform.
---

1. `check_gpu_health()` — temperature, power, throttling, ECC, PCIe
   - `temp > 85°C` → thermal throttling, check cooling
   - `throttle reason != None` → identify type (thermal/power/other)
   - `ECC uncorrectable > 0` → hardware fault, replace GPU
   - `PCIe Gen1 x1` (should be Gen3/4 x16) → PCIe degradation
2. `check_gpu_memory()` — VRAM usage, per-process, OOM indicators
   - `VRAM > 95%` → risk of CUDA OOM
   - `orphaned allocations` → crashed process didn't release memory
3. `check_gpu_utilization()` — compute vs memory bandwidth
   - `GPU util < 30%` but training → data pipeline bottleneck (CPU too slow)
   - `memory BW near max` + low compute util → memory-bound workload
4. Classify:
   - CUDA OOM → reduce batch_size or fix memory leak in training loop
   - thermal throttle → improve cooling / reduce power limit
   - low utilization → profile data loading pipeline
   - ECC errors → hardware replacement
