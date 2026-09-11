"""Raw-evidence ledger layer (design document §6.6, v3.37.0).

WHY this module exists
----------------------
Diagnosis evidence reached the report as LLM retold summaries
(``evidence.summary`` <=3000 chars written by the Coordinator's
``record_finding`` argument, or expert prose).  The raw tool outputs DO
exist (the ledger middleware offloads every diagnostic result to
``/tool_results/r{n}/...`` and experts may read them back), but nothing
in the report-time context pointed at them, so the report's root-cause
claims were only as good as the retelling — no addressable evidence, no
provenance.

Industry practice (verified 2026-09-11):
- Google SRE postmortem example: the body carries conclusion-level
  facts plus verifiable quantitative anchors; raw material lives in
  "Supporting information" as external references (dashboard link with
  an explicit time window), not pasted into the narrative.
- AWS CloudWatch incident reports: AI-derived *facts* (atomic,
  time-shifted relations) form the foundation of the report, with the
  investigation telemetry retained behind them.
- Evidence-grounded generation research (DeepFaith, arXiv:2607.24348;
  evidence-tracing survey, arXiv:2606.04990): the evidence layer must be
  separated from the generation layer, and the serialisation step must be
  DETERMINISTIC — the ablation that removed deterministic serialisation
  lost the most faithfulness (0.92 -> 0.82); claim-level attribution is
  what makes a report auditable.

Design consequences implemented here (all deterministic, no LLM):
1. Every offloaded diagnostic result gets an addressable evidence entry
   (``EV-r{round}-{n}``) with actor, target args, size, failure/empty
   classification, hypothesis attribution (parsed from the delegation
   instruction) and a bounded excerpt.
2. The excerpt is selected by code (timestamps / signal words /
   head-tail context), never paraphrased — verbatim lines only.
3. At REPORT time the ledger context gains an evidence index (paths to
   re-read) and a per-root-cause evidence pack (verbatim excerpts), so
   the report can be written from primary evidence instead of memory.

Metric layer exclusion (user decision, 2026-09-11): Argus results are
metric time series, and raw series are NOT report-grade material —
"当前基于时序总结的格式满足要求".  Metric entries are therefore recorded
(traceability) but excluded from the report-time index and pack, with the
excluded count disclosed so silence is never read as "no data".

Non-interference (verified 2026-09-11): this layer only WRITES
``ledger["tool_results"]`` and reads it back at report time.  It does not
touch the round counter (``_model_call_count`` is never mutated — the
evidence round is a local value), the dedup cache/counters, or the
EIG inputs consumed by ``delegation_value_for`` (``rounds[].delegated_for``
/ ``delegated_experts`` and ``evidence[].source``); see
``test_evidence_ledger.py`` I 组 for the locked invariants.

The LLM's role is deliberately reduced to SELECTION AND ATTRIBUTION
(which evidence supports which hypothesis) — the transport of evidence
text stays in code, because LLM-mediated evidence transport is the
failure mode this project already hit twice (derived appendix truncation
echoing internal system text; ``key_findings`` 200-char truncation making
the conflict detector structurally blind, v3.33.0).
"""

from __future__ import annotations

import re
from typing import Any

# ── Budgets (deterministic caps, report-time rendering only) ───────────────

DIGEST_MAX_CHARS = 4000
"""Max chars of one verbatim excerpt (code-selected lines)."""

DIGEST_MIN_CONTENT = 400
"""Results shorter than this get no excerpt — ``preview`` already covers them."""

INDEX_LIMIT = 20
"""Max rows in the report-time evidence index."""

PACK_PER_HYPOTHESIS = 3
"""Max excerpts per hypothesis in the report-time evidence pack."""

PACK_MAX_CHARS = 16000
"""Total budget of the report-time evidence pack."""

EVIDENCE_INDEX_TITLE = "原始证据索引（系统整理，可用 read_file 回读全文）"
EVIDENCE_PACK_TITLE = "根因支持证据（原始片段，系统确定性摘录）"

# ── Deterministic selection patterns ──────────────────────────────────────

_TS_RE = re.compile(r"\b\d{1,2}:\d{2}(?::\d{2})?\b")
"""Clock timestamps — the timeline anchor of any diagnosis line."""

_SIGNAL_RE = re.compile(
    r"(error|fail|panic|timeout|refus|denied|killed|oom|notready|crashloop"
    r"|backoff|unhealthy|unavailable|异常|失败|超时|拒绝|不可用|中断|突增"
    r"|陡增|抖动|丢包|耗尽|打满|重启)",
    re.IGNORECASE,
)
"""Signal vocabulary; deliberately broad — over-selecting costs budget,
under-selecting costs evidence (full text stays one read_file away)."""

_NOISE_RE = re.compile(r"^[\s\-=_*#|+`~.]+$")
"""Separator / table-rule / empty lines carry no evidence."""

_NUM_RE = re.compile(r"\d")

# Target-arg whitelist: the parameters that identify WHAT was observed.
# Ordered by identification value (entity first, time window last).
_TARGET_ARG_KEYS: tuple[str, ...] = (
    "pod_name", "hostname", "node_name", "cluster_name", "namespace",
    "workload_name", "deployment_name", "service_name", "container_name",
    "device", "mountpoint", "monitor_name", "start_time", "end_time",
    "minutes", "query", "pattern",
)
_TARGET_MAX_KEYS = 4
_TARGET_VALUE_CHARS = 120


def is_dedup_receipt(content: str) -> bool:
    """True for the system's dedup receipt (not fresh evidence)."""
    return content.lstrip().startswith("[系统去重]")


def is_metric_tool(tool_name: str) -> bool:
    """Metric time-series tool classification (deterministic by name).

    Argus tools return metric samples (open-falcon style endpoint/metric/
    tags time series).  User decision (2026-09-11): metric time series must
    NOT be quoted raw in a failure report — the TIME-SERIES SUMMARY written
    by the Argus experts is the report-grade form ("当前基于时序总结的格式满足
    要求").  Metric results therefore stay OUT of the report-time evidence
    index and pack (they remain in ``tool_results`` for traceability and
    re-readability, and the exclusion is disclosed with a count).

    Classification signal: every Argus tool carries "argus" in its name
    (16 tools, verified 2026-09-11: ``get_argus_os_*_metrics``,
    ``get_argus_k8s_*_metrics``, ``get_argus_shared_etcd_metrics``); a
    ``*_metrics`` suffix is kept as a second, family-agnostic signal.
    """
    name = (tool_name or "").lower()
    return "argus" in name or name.endswith("_metrics")


def is_empty_result(content: str) -> bool:
    """Empty / no-data result (same predicate family as dedup caching)."""
    stripped = content.strip()
    if not stripped:
        return True
    return len(stripped) < 50 and bool(
        re.search(
            r"(no\s+data|not\s+found|no\s+results?|empty|无数据|未找到|无匹配"
            r"|参数缺失|需要参数)",
            stripped,
            re.IGNORECASE,
        )
    )


def extract_target(args: dict) -> dict:
    """Deterministic target extraction from tool args (whitelist only)."""
    out: dict[str, str] = {}
    if not isinstance(args, dict):
        return out
    for key in _TARGET_ARG_KEYS:
        if len(out) >= _TARGET_MAX_KEYS:
            break
        raw = args.get(key)
        if raw is None or isinstance(raw, (dict, list)):
            continue
        value = str(raw).strip()
        if not value:
            continue
        out[key] = value[:_TARGET_VALUE_CHARS]
    return out


def fmt_target(target: dict) -> str:
    """Render a target dict as a compact ``key=value`` list."""
    if not target:
        return "(未标注目标)"
    return " · ".join(f"{k}={v}" for k, v in target.items())


def artifact_digest(content: str, max_chars: int = DIGEST_MAX_CHARS) -> str:
    """Deterministic verbatim excerpt of a raw tool result.

    Selection (no LLM, stable for identical input):
    - head 3 non-noise lines (context: headers give units/entity),
    - up to 6 highest-signal lines (timestamp / signal vocabulary /
      numeric), kept in original order,
    - tail 2 non-noise lines (end state).
    Lines are deduplicated, truncated to 200 chars each, and clipped to
    ``max_chars``.  Returns "" when nothing is selectable.
    """
    lines = [ln.rstrip() for ln in content.splitlines()]
    keep_idx = [
        i for i, ln in enumerate(lines)
        if ln.strip() and not _NOISE_RE.match(ln.strip())
    ]
    if not keep_idx:
        return ""

    head = keep_idx[:3]
    tail = keep_idx[-2:]

    scored: list[tuple[int, int]] = []
    for i in keep_idx:
        ln = lines[i]
        score = 0
        if _TS_RE.search(ln):
            score += 2
        if _SIGNAL_RE.search(ln):
            score += 3
        if _NUM_RE.search(ln):
            score += 1
        if score >= 2:
            scored.append((i, score))
    top = [i for i, _ in sorted(scored, key=lambda x: (-x[1], x[0]))[:6]]

    chosen: list[int] = []
    for i in sorted({*head, *top, *tail}):
        if i not in chosen:
            chosen.append(i)

    picked: list[str] = []
    seen: set[str] = set()
    total = 0
    for i in chosen:
        ln = lines[i][:800]
        if ln in seen:
            continue
        if total + len(ln) + 1 > max_chars:
            break
        seen.add(ln)
        picked.append(ln)
        total += len(ln) + 1
    return "\n".join(picked)


def _next_ev_id(ledger: dict, round_no: int) -> str:
    """Next free evidence ID within a round (``EV-r{round}-{n}``)."""
    prefix = f"EV-r{round_no}-"
    highest = 0
    for info in (ledger.get("tool_results") or {}).values():
        ev_id = str(info.get("ev_id") or "")
        if ev_id.startswith(prefix):
            tail = ev_id[len(prefix):]
            if tail.isdigit():
                highest = max(highest, int(tail))
    return f"{prefix}{highest + 1}"


def build_artifact_entry(
    ledger: dict,
    *,
    tool: str,
    args: dict,
    content: str,
    source_path: str,
    round_no: int,
    actor: str,
    hypothesis_id: str = "",
    delegation: str = "",
    is_failure: bool = False,
) -> dict:
    """Build one evidence entry (index only; excerpt bounded).

    ``actor`` is ``"coordinator"`` or ``"expert"``; ``hypothesis_id`` is
    parsed from the delegation instruction when available — that is the
    deterministic evidence→hypothesis attribution the report-time pack
    groups by.
    """
    lines = content.splitlines()
    dedup_hit = is_dedup_receipt(content)
    entry: dict[str, Any] = {
        "path": source_path,
        "round": round_no,
        "preview": content[:800].replace("\n", " "),
        "lines": len(lines),
        "chars": len(content),
        "tool": tool,
        "actor": actor,
        "is_metric": is_metric_tool(tool),
        "hypothesis_id": hypothesis_id or "",
        "delegation": delegation or "",
        "target": extract_target(args),
        "is_failure": bool(is_failure),
        "is_empty": is_empty_result(content),
        "dedup_hit": dedup_hit,
        "ev_id": _next_ev_id(ledger, round_no),
    }
    if not dedup_hit and len(content) >= DIGEST_MIN_CONTENT:
        entry["digest"] = artifact_digest(content)
    return entry


def record_artifact(ledger: dict, cache_key: str, entry: dict) -> dict:
    """Store/merge an evidence entry into ``ledger["tool_results"]``.

    Merge semantics (deterministic provenance):
    - dedup receipt → keep the original data fields, count the hit;
    - same evidence re-collected → refresh data, keep first ``ev_id``
      / first round / first actor (that is when it was gathered).
    """
    bucket = ledger.setdefault("tool_results", {})
    prev = bucket.get(cache_key)
    if not isinstance(prev, dict):
        bucket[cache_key] = entry
        return entry

    merged = dict(prev)
    if entry.get("dedup_hit"):
        merged["dedup_hits"] = int(prev.get("dedup_hits", 0)) + 1
        merged["last_seen_round"] = entry.get("round", prev.get("round", 0))
        bucket[cache_key] = merged
        return merged

    merged.update(entry)
    merged["ev_id"] = prev.get("ev_id") or entry.get("ev_id") or ""
    merged["round"] = prev.get("round", entry.get("round", 0))
    merged["actor"] = prev.get("actor") or entry.get("actor") or ""
    merged["dedup_hits"] = int(prev.get("dedup_hits", 0))
    bucket[cache_key] = merged
    return merged


def drop_excerpts(ledger: dict) -> dict:
    """Ledger copy with evidence excerpts removed (frontend payload size).

    Excerpts serve the report-time LLM context and the persisted ledger
    file; streaming snapshots for the UI do not need them, and one
    <=600-char excerpt per gathered result adds up across a session.
    """
    tool_results = ledger.get("tool_results")
    if not isinstance(tool_results, dict):
        return ledger
    out = dict(ledger)
    out["tool_results"] = {
        key: (
            {ik: iv for ik, iv in info.items() if ik != "digest"}
            if isinstance(info, dict) and info.get("digest") else info
        )
        for key, info in tool_results.items()
    }
    return out


def iter_artifacts(ledger: dict) -> list[tuple[str, dict]]:
    """Evidence entries in acquisition order (round, insertion)."""
    items = list((ledger.get("tool_results") or {}).items())
    return sorted(
        items,
        key=lambda kv: (kv[1].get("round", 0), kv[1].get("ev_id", "")),
    )


def evidence_stats(ledger: dict) -> dict:
    """Counts for observability (logging/tests)."""
    arts = [info for _k, info in iter_artifacts(ledger)]
    return {
        "total": len(arts),
        "with_hypothesis": sum(1 for a in arts if a.get("hypothesis_id")),
        "expert": sum(1 for a in arts if a.get("actor") == "expert"),
        "coordinator": sum(1 for a in arts if a.get("actor") == "coordinator"),
        "failed": sum(1 for a in arts if a.get("is_failure")),
        "empty": sum(1 for a in arts if a.get("is_empty")),
        "metric": sum(1 for a in arts if a.get("is_metric")),
        "with_digest": sum(1 for a in arts if a.get("digest")),
    }


def _index_line(info: dict) -> str:
    """One evidence-index row: ID | actor/H | tool | target | size | path."""
    ev_id = info.get("ev_id") or "?"
    actor = info.get("actor") or "?"
    hid = info.get("hypothesis_id") or ""
    tool = info.get("tool") or "?"
    flags = ""
    if info.get("is_failure"):
        flags += " ⚠失败"
    elif info.get("is_empty"):
        flags += " ⚠空结果"
    if info.get("dedup_hits"):
        flags += f"（重复调用 {info['dedup_hits']} 次）"
    path = info.get("path") or ""
    ref = f"read_file(path=\"{path}\")" if path else "(未落盘)"
    return (
        f"- {ev_id} | {actor}{('/' + hid) if hid else ''} | {tool} | "
        f"{fmt_target(info.get('target') or {})} | "
        f"{info.get('lines', '?')}行{flags} → `{ref}`"
    )


def render_evidence_index(ledger: dict, limit: int = INDEX_LIMIT) -> str:
    """Report-time index of gathered raw evidence (bounded, newest kept).

    Metric time-series entries are excluded by design (the Argus experts'
    time-series summaries are the report-grade form); their count is
    disclosed so "no raw rows" is never read as "no data gathered".
    """
    arts = iter_artifacts(ledger)
    if not arts:
        return ""
    metric = [info for _k, info in arts if info.get("is_metric")]
    raw = [(k, info) for k, info in arts if not info.get("is_metric")]
    lines = [f"## {EVIDENCE_INDEX_TITLE}"]
    if metric:
        lines.append(
            f"- （Argus 指标时序数据 {len(metric)} 条按设计不注入"
            "——报告按时序总结引用其结论）"
        )
    if raw:
        shown = raw[-limit:] if len(raw) > limit else raw
        if len(raw) > len(shown):
            lines.append(f"- （共 {len(raw)} 条，此处列最近 {len(shown)} 条）")
        lines.extend(_index_line(info) for _k, info in shown)
    return "\n".join(lines)


def _hypothesis_groups(ledger: dict) -> list[tuple[str, dict]]:
    """Hypotheses to render evidence for: root causes first, then refuted."""
    hypotheses = ledger.get("hypotheses", {}) or {}
    ordered: list[tuple[str, dict]] = []
    seen: set[str] = set()
    for hid in ledger.get("root_cause_hypothesis_ids", []) or []:
        node = hypotheses.get(str(hid))
        if node is not None and str(hid) not in seen:
            ordered.append((str(hid), node))
            seen.add(str(hid))
    for hid, node in hypotheses.items():
        if str(hid) in seen:
            continue
        if node.get("status") == "confirmed":
            ordered.append((str(hid), node))
            seen.add(str(hid))
    for hid, node in hypotheses.items():
        if str(hid) in seen:
            continue
        if node.get("status") == "refuted":
            ordered.append((str(hid), node))
            seen.add(str(hid))
    return ordered


def _pack_block(hid: str, node: dict, arts: list[tuple[str, dict]],
                metric_count: int = 0) -> list[str]:
    """Evidence block for one hypothesis (verbatim excerpts)."""
    from diagnostics.agent.ledger import fmt_hid  # local: avoid import cycle

    status = node.get("status", "")
    mark = "根因" if status == "confirmed" else "已排除"
    lines = [
        f"### {fmt_hid(hid)} {node.get('statement', '')}"
        f"（{mark}，置信度 {node.get('probability')}%）"
    ]
    if not arts:
        if metric_count:
            lines.append(
                f"- （该假设的原始数据为 Argus 指标时序 {metric_count} 条"
                "——报告按时序总结引用其结论，不注入原始序列）"
            )
        else:
            lines.append(
                "- （该系统整理层未关联到原始证据条目——本假设的判定依据见台账"
                " evidence；若需原始数据请按上方索引回读）"
            )
        return lines
    for _k, info in arts:
        head = (
            f"- **{info.get('ev_id') or '?'}**（{info.get('actor') or '?'}"
            f" · {info.get('tool') or '?'}"
            f" · {fmt_target(info.get('target') or {})}"
            f" · {info.get('round', '?')}轮"
            f" · {info.get('lines', '?')}行"
        )
        if info.get("is_failure"):
            head += " · ⚠工具失败"
        elif info.get("is_empty"):
            head += " · ⚠空结果"
        path = info.get("path") or ""
        head += f" · 全文 `{path}`）" if path else "）"
        lines.append(head)
        digest = str(info.get("digest") or "")
        if digest:
            for dline in digest.splitlines():
                lines.append(f"  - `{dline.strip()[:800]}`")
        else:
            lines.append("  - （结果过短，无摘录——见路径或 preview）")
    return lines


def render_evidence_pack(
    ledger: dict,
    *,
    per_hypothesis: int = PACK_PER_HYPOTHESIS,
    max_chars: int = PACK_MAX_CHARS,
) -> str:
    """Report-time per-hypothesis evidence pack (verbatim, bounded).

    Attribution is deterministic: expert evidence entries carry the
    hypothesis ID parsed from the delegation instruction (v3.37.0).
    Coordinator-level evidence cannot be attributed by code and shows up
    in the index instead — reported as such, never guessed.
    """
    groups = _hypothesis_groups(ledger)
    if not groups:
        return ""
    by_hid: dict[str, list[tuple[str, dict]]] = {}
    metric_by_hid: dict[str, int] = {}
    arts_any = False
    for cache_key, info in iter_artifacts(ledger):
        arts_any = True
        hid = str(info.get("hypothesis_id") or "")
        if not hid:
            continue
        if info.get("is_metric"):
            # Metric time series never enter the pack (time-series summaries
            # are the report-grade form); only the count is disclosed.
            metric_by_hid[hid] = metric_by_hid.get(hid, 0) + 1
            continue
        by_hid.setdefault(hid, []).append((cache_key, info))
    if not arts_any:
        # No raw evidence gathered at all → nothing to disclose (the report
        # context stays as before; silence is honest here).
        return ""

    lines = [
        f"## {EVIDENCE_PACK_TITLE}",
        "> 以下片段由系统从工具原始结果中确定性摘录（未改写）；正文引用证据时"
        "请给出编号（如 EV-r6-2）或工具名 + 实体 + 时间窗，完整原文见各自路径。",
    ]
    budget = max_chars
    truncated = False
    for hid, node in groups:
        arts = by_hid.get(hid, [])[:per_hypothesis]
        block = _pack_block(hid, node, arts, metric_by_hid.get(hid, 0))
        block_len = sum(len(x) + 1 for x in block)
        if block_len > budget:
            truncated = True
            break
        lines.extend(block)
        budget -= block_len
    if truncated:
        lines.append("> （证据包达到长度上限，其余条目见上方索引）")
    return "\n".join(lines)
