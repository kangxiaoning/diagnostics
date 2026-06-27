"""Diagnosis ledger data structures, rendering, and serialization.

The ledger is a structured diagnosis memory persisted in LangGraph state.
It holds the hypothesis tree, evidence chain, and diagnosis round history.

See: private/HYPOTHESIS_LEDGER_DESIGN.md
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Annotated, Any, Literal, NotRequired

from deepagents.graph import DeepAgentState
from langchain.agents.middleware.types import PrivateStateAttr


# ── Phase & status types ──

DiagnosisPhase = Literal[
    "understand", "hypothesize", "verify", "evaluate", "report",
    "skill_verify", "backtrack",
]

HypothesisStatus = Literal[
    "pending", "verifying", "confirmed",
    "refuted", "deprioritized", "dead_end",
]

Verdict = Literal["confirmed", "refuted", "inconclusive"]


# ── TypedDicts (plain dicts at runtime, typed for clarity) ──

class FaultProfile(dict):
    """从用户描述提取的故障画像。"""


class Evidence(dict):
    """单条证据。"""


class HypothesisNode(dict):
    """假设树节点。"""


class RoundRecord(dict):
    """单个诊断步骤的记录。"""


class DiagnosisLedger(dict):
    """诊断台账 — Agent 的结构化诊断记忆。"""


# ── State schema ──

class DiagnosisLedgerState(DeepAgentState):
    """Extend deepagents state with a private diagnosis ledger field."""

    _diagnosis_ledger: Annotated[
        NotRequired[DiagnosisLedger | None],
        PrivateStateAttr,
    ]


# ── Factory functions ──

def new_ledger() -> DiagnosisLedger:
    """Create an empty diagnosis ledger."""
    return DiagnosisLedger(
        fault_profile=None,
        hypotheses={},
        root_hypothesis_ids=[],
        active_path=[],
        current_phase="understand",
        current_round=0,
        rounds=[],
        root_cause=None,               # backward-compat: first confirmed root cause
        root_cause_hypothesis_id=None,  # backward-compat
        root_causes=[],                # list[str] — all confirmed root causes
        root_cause_hypothesis_ids=[],  # list[str] — matching hypothesis IDs
        report=None,
        exhausted=False,
        skill_mode=False,
        skill_ids=[],
        _inconclusive_streak=0,
        _backtrack_count=0,
    )


def new_fault_profile(
    entities: list[str],
    symptoms: list[str],
    user_raw: str,
    timeline: str = "",
    recent_changes: str = "",
    prior_actions: str = "",
) -> FaultProfile:
    return FaultProfile(
        entities=entities,
        symptoms=symptoms,
        timeline=timeline,
        recent_changes=recent_changes,
        prior_actions=prior_actions,
        user_raw=user_raw,
    )


def new_hypothesis(
    hid: str,
    statement: str,
    probability: int,
    parent_id: str | None,
    depth: int,
    round_num: int,
    rationale: str = "",
) -> HypothesisNode:
    return HypothesisNode(
        id=hid,
        parent_id=parent_id,
        depth=depth,
        statement=statement,
        probability=max(0, min(100, probability)),
        status="pending",
        evidence=[],
        verification_tools=[],
        sub_hypothesis_ids=[],
        selected=False,
        rationale=rationale,
        created_at_round=round_num,
    )


def new_evidence(
    source: str,
    summary: str,
    supports: bool,
    tool_call_id: str | None = None,
) -> Evidence:
    return Evidence(
        source=source,
        summary=summary[:500],
        supports=supports,
        tool_call_id=tool_call_id,
        timestamp=datetime.now(UTC).isoformat(),
    )


# ── ID generation ──

def _next_hypothesis_id(parent_id: str | None, hypotheses: dict[str, HypothesisNode]) -> str:
    """Generate next hypothesis ID like H1, H2, H1.1, H1.2.

    Scans existing IDs to avoid collisions when called multiple times
    for the same parent within a single commit_hypotheses invocation.
    """
    if parent_id is None:
        existing_top = [h for h in hypotheses.values() if h["parent_id"] is None]
        next_num = len(existing_top) + 1
        # Avoid collision: skip numbers already taken
        while f"H{next_num}" in hypotheses:
            next_num += 1
        return f"H{next_num}"
    parent = hypotheses.get(parent_id)
    if parent is None:
        return "H1"
    count = len(parent.get("sub_hypothesis_ids", []))
    candidate = f"{parent_id}.{count + 1}"
    # Avoid collision: increment suffix until unique
    suffix = count + 1
    while candidate in hypotheses:
        suffix += 1
        candidate = f"{parent_id}.{suffix}"
    return candidate


# ── Hypothesis tree operations ──

def add_hypotheses(
    ledger: DiagnosisLedger,
    hypothesis_specs: list[dict],
    parent_id: str | None,
) -> list[HypothesisNode]:
    """Add up to 3 hypotheses under a parent. Returns created nodes."""
    if len(hypothesis_specs) > 3:
        raise ValueError(
            f"最多3个假设，收到 {len(hypothesis_specs)} 个。请只提交可能性最大的3个。"
        )

    created: list[HypothesisNode] = []
    hypotheses = ledger["hypotheses"]
    parent_depth = hypotheses[parent_id]["depth"] if parent_id else -1

    for spec in hypothesis_specs:
        hid = _next_hypothesis_id(parent_id, hypotheses)
        node = new_hypothesis(
            hid=hid,
            statement=spec["statement"],
            probability=spec.get("probability", 50),
            parent_id=parent_id,
            depth=parent_depth + 1,
            round_num=ledger["current_round"],
            rationale=spec.get("rationale", ""),
        )
        hypotheses[hid] = node
        created.append(node)

        if parent_id is None:
            ledger["root_hypothesis_ids"].append(hid)
        else:
            parent = hypotheses[parent_id]
            parent["sub_hypothesis_ids"].append(hid)

    # Auto-select the highest probability as verifying
    if created:
        best = max(created, key=lambda h: h["probability"])
        best["status"] = "verifying"
        best["selected"] = True
        ledger["active_path"].append(best["id"])

    return created


def select_path(
    ledger: DiagnosisLedger,
    selected_id: str,
    rationale: str = "",
    deprioritized: list[dict] | None = None,
) -> None:
    """Select one hypothesis to deepen, mark others as deprioritized."""
    if deprioritized is None:
        deprioritized = []
    hypotheses = ledger["hypotheses"]
    if selected_id not in hypotheses:
        raise ValueError(f"假设 {selected_id} 不存在")

    # Mark selected
    selected = hypotheses[selected_id]
    selected["selected"] = True
    if selected["status"] == "pending":
        selected["status"] = "verifying"
    if rationale:
        selected["rationale"] = rationale

    # Update active_path: remove siblings, keep ancestors
    parent_id = selected["parent_id"]
    siblings: list[str] = []
    if parent_id is None:
        siblings = ledger["root_hypothesis_ids"]
    else:
        siblings = hypotheses[parent_id]["sub_hypothesis_ids"]

    # Rebuild active_path: ancestors only
    new_path: list[str] = []
    cursor = selected_id
    while cursor:
        new_path.insert(0, cursor)
        node = hypotheses[cursor]
        cursor = node["parent_id"]
    ledger["active_path"] = new_path

    # Mark non-selected siblings as deprioritized
    for sid in siblings:
        if sid == selected_id:
            continue
        sibling = hypotheses[sid]
        if sibling["status"] in ("pending", "verifying"):
            sibling["status"] = "deprioritized"
    # Apply explicit deprioritized reasons
    for dep in deprioritized:
        dep_id = dep.get("id", "")
        if dep_id in hypotheses:
            hypotheses[dep_id]["status"] = "deprioritized"
            hypotheses[dep_id]["rationale"] = dep.get("reason", "")


def record_finding(
    ledger: DiagnosisLedger,
    hypothesis_id: str,
    verdict: Verdict,
    evidence_summary: str,
    probability_update: int,
    new_insights: str = "",
) -> None:
    """Record a verification finding for a hypothesis."""
    hypotheses = ledger["hypotheses"]
    if hypothesis_id not in hypotheses:
        raise ValueError(f"假设 {hypothesis_id} 不存在")

    node = hypotheses[hypothesis_id]
    node["probability"] = max(0, min(100, probability_update))

    supports = verdict == "confirmed"
    if evidence_summary:
        node["evidence"].append(new_evidence(
            source="coordinator",
            summary=evidence_summary,
            supports=supports,
        ))

    if verdict == "confirmed":
        node["status"] = "confirmed"
        ledger["_inconclusive_streak"] = 0
    elif verdict == "refuted":
        node["status"] = "refuted"
        ledger["_inconclusive_streak"] = 0
    else:  # inconclusive
        ledger["_inconclusive_streak"] += 1
        # After 3+ consecutive inconclusive, mark as dead_end to break
        # the verify loop — derive_phase would otherwise keep returning
        # "verify" because the node status remained "verifying".
        if ledger["_inconclusive_streak"] >= 3:
            node["status"] = "dead_end"

    if new_insights:
        node["rationale"] = (node.get("rationale", "") + " | 新发现: " + new_insights).strip(" |")


def add_evidence_to_active(
    ledger: DiagnosisLedger,
    source: str,
    summary: str,
    supports: bool = True,
    tool_call_id: str | None = None,
    tool_name: str | None = None,
) -> None:
    """Add evidence to the current active hypothesis (auto from tool calls)."""
    if not ledger["active_path"]:
        return
    active_id = ledger["active_path"][-1]
    node = ledger["hypotheses"].get(active_id)
    if node is None:
        return
    node["evidence"].append(new_evidence(source, summary, supports, tool_call_id))
    if tool_name and tool_name not in node["verification_tools"]:
        node["verification_tools"].append(tool_name)


# ── Phase derivation ──

def derive_phase(ledger: DiagnosisLedger) -> DiagnosisPhase:
    """Derive the current diagnosis phase from ledger state."""
    # Multi-root: if some roots are confirmed but others still pending,
    # stay in evaluate to continue diagnosing remaining symptoms.
    root_causes = ledger.get("root_causes", [])
    root_ids = ledger.get("root_hypothesis_ids", [])
    if root_causes and root_ids:
        confirmed_ids = set(ledger.get("root_cause_hypothesis_ids", []))
        hypotheses = ledger.get("hypotheses", {})
        for hid in root_ids:
            if hid in confirmed_ids:
                continue
            node = hypotheses.get(hid)
            if node and node.get("status") not in ("refuted", "dead_end"):
                return "evaluate"  # still has pending root hypotheses
    if ledger.get("root_cause") or ledger.get("report"):
        return "report"
    if ledger.get("exhausted"):
        return "report"

    # Skill mode
    if ledger.get("skill_mode"):
        return _derive_skill_phase(ledger)

    return _derive_default_phase(ledger)


def _derive_default_phase(ledger: DiagnosisLedger) -> DiagnosisPhase:
    if ledger.get("fault_profile") is None:
        # If hypotheses have already been committed, UNDERSTAND is complete
        # even if fault_profile wasn't explicitly populated.  Fall through
        # to regular phase logic based on hypothesis status.
        if ledger.get("hypotheses"):
            pass  # fall through — hypotheses exist, derive phase from their status
        elif ledger.get("current_round", 0) >= 2:
            # LLM had at least one full round but still hasn't called
            # commit_hypotheses — force advance to stop re-collecting data.
            return "hypothesize"
        else:
            return "understand"

    hypotheses: dict = ledger["hypotheses"]
    if not hypotheses:
        return "hypothesize"

    # Check if active path has an unverified hypothesis
    if ledger["active_path"]:
        active_id = ledger["active_path"][-1]
        active = hypotheses.get(active_id)
        if active and active["status"] == "verifying":
            return "verify"

    # Check current layer for verdict completion
    current_layer = _get_current_layer(ledger)
    all_have_verdict = all(
        hypotheses[hid]["status"] in ("confirmed", "refuted", "dead_end")
        for hid in current_layer
    )

    if all_have_verdict:
        # Check exit conditions
        confirmed = [hypotheses[hid] for hid in current_layer
                     if hypotheses[hid]["status"] == "confirmed"]
        if confirmed:
            best = max(confirmed, key=lambda h: h["probability"])
            if best["probability"] >= 80:
                # Check if it can be deepened (no sub-hypotheses yet)
                if not best["sub_hypothesis_ids"]:
                    if _inconclusive_exhausted(ledger):
                        return "report"
                    return "report"
                return "evaluate"
            return "evaluate"
        # All refuted
        if _can_backtrack(ledger):
            return "backtrack"
        return "report"

    return "evaluate"


def _derive_skill_phase(ledger: DiagnosisLedger) -> DiagnosisPhase:
    """Skill mode: UNDERSTAND → SKILL_VERIFY → EVALUATE → REPORT."""
    if ledger.get("fault_profile") is None:
        if ledger.get("current_round", 0) >= 2:
            return "skill_verify"
        return "understand"
    # In skill mode, we stay in skill_verify until done
    return "skill_verify"


def _get_current_layer(ledger: DiagnosisLedger) -> list[str]:
    """Get hypothesis IDs at the current exploration depth."""
    if not ledger["active_path"]:
        return ledger["root_hypothesis_ids"]
    active_id = ledger["active_path"][-1]
    node = ledger["hypotheses"].get(active_id)
    if node and node["sub_hypothesis_ids"]:
        return node["sub_hypothesis_ids"]
    # Check siblings at same depth
    parent_id = node["parent_id"] if node else None
    if parent_id is None:
        return ledger["root_hypothesis_ids"]
    return ledger["hypotheses"][parent_id]["sub_hypothesis_ids"]


def _can_backtrack(ledger: DiagnosisLedger) -> bool:
    """Check if there are deprioritized or pending siblings to backtrack to."""
    for node in ledger["hypotheses"].values():
        if node["status"] in ("deprioritized", "pending"):
            return True
    return False


def _inconclusive_exhausted(ledger: DiagnosisLedger) -> bool:
    """Check if evidence is saturated (3+ consecutive inconclusive)."""
    return ledger.get("_inconclusive_streak", 0) >= 3


def backtrack(ledger: DiagnosisLedger) -> str | None:
    """Backtrack to the nearest deprioritized or pending hypothesis.

    Returns new active ID or None if exhausted.
    """
    ledger["_backtrack_count"] += 1

    # Walk up the active path to find a node with backtrackable siblings
    path = list(ledger["active_path"])
    while path:
        current_id = path[-1]
        current = ledger["hypotheses"].get(current_id)
        if current is None:
            path.pop()
            continue
        parent_id = current["parent_id"]

        # Look for deprioritized or pending siblings
        if parent_id is None:
            siblings = ledger["root_hypothesis_ids"]
        else:
            siblings = ledger["hypotheses"][parent_id]["sub_hypothesis_ids"]

        for sid in siblings:
            sibling = ledger["hypotheses"][sid]
            if sibling["status"] in ("deprioritized", "pending"):
                # Found a backtrack target
                sibling["status"] = "verifying"
                sibling["selected"] = True
                # Rebuild active path
                new_path: list[str] = []
                cursor = sid
                while cursor:
                    new_path.insert(0, cursor)
                    cursor = ledger["hypotheses"][cursor]["parent_id"]
                ledger["active_path"] = new_path
                return sid

        path.pop()

    # No backtrack target found
    ledger["exhausted"] = True
    return None


# ── Context rendering (injected into system message each round) ──

# Scaffolding tools that are NOT diagnostic — excluded from history rendering
_SCAFFOLDING_TOOLS_BUILTIN = frozenset({
    "write_file", "read_file", "edit_file",
    "write_todos", "read_todos",
    "ls", "glob", "grep",
    "commit_hypotheses", "select_path", "record_finding", "backtrack",
    "list_diagnostic_capabilities", "get_system_overview",
})

def render_ledger_context(ledger: DiagnosisLedger | None) -> str:
    """Render a compact, structured summary of the ledger for LLM context."""
    if not ledger:
        return ""

    lines: list[str] = ["<diagnosis_ledger>"]

    # Current status
    phase = derive_phase(ledger)
    active_path_str = " → ".join(ledger["active_path"]) if ledger["active_path"] else "(无)"
    lines.append(f"## 当前诊断状态")
    lines.append(f"- 阶段: {phase} | 步骤: {ledger['current_round']} | 活动路径: {active_path_str}")
    lines.append("")

    # Fault profile
    fp = ledger.get("fault_profile")
    if fp:
        lines.append("## 故障画像")
        lines.append(f"- 实体: {', '.join(fp.get('entities', []))}")
        lines.append(f"- 症状: {', '.join(fp.get('symptoms', []))}")
        if fp.get("timeline"):
            lines.append(f"- 时间线: {fp['timeline']}")
        if fp.get("recent_changes"):
            lines.append(f"- 最近变更: {fp['recent_changes']}")
        lines.append("")

    # Hypothesis tree
    if ledger["hypotheses"]:
        lines.append("## 假设树")
        _render_hypothesis_tree(lines, ledger, ledger["root_hypothesis_ids"], indent=0)
        lines.append("")

    # Refuted hypotheses summary
    refuted = [h for h in ledger["hypotheses"].values()
               if h["status"] in ("refuted", "deprioritized", "dead_end")]
    if refuted:
        lines.append("## 已排除假设")
        for h in refuted:
            reason = h.get("rationale", "") or "(无记录)"
            lines.append(f"- {h['id']} {h['statement']}: {reason}")
        lines.append("")

    # Root causes (multi-root support)
    root_causes: list[str] = ledger.get("root_causes", [])
    if root_causes:
        if len(root_causes) == 1:
            lines.append(f"## 根因: {root_causes[0]}")
        else:
            lines.append("## 已确认根因")
            for i, rc in enumerate(root_causes, 1):
                lines.append(f"{i}. {rc}")
        lines.append("")
    elif ledger.get("root_cause"):
        lines.append(f"## 根因: {ledger['root_cause']}")
        lines.append("")

    # ── Diagnostic history: tools called per round with key findings ──
    # Renders a concise timeline so the LLM knows what has been done
    # and does not re-call tools whose results are already recorded.
    rounds_data = ledger.get("rounds", [])
    if rounds_data:
        lines.append("## 诊断历史 (已调用工具)")
        # Group by round number
        round_map: dict[int, list[str]] = {}
        for rd in rounds_data:
            rn = rd.get("round", 0)
            if rn not in round_map:
                round_map[rn] = []
            for tc in rd.get("tools_called", []):
                if tc not in _SCAFFOLDING_TOOLS_BUILTIN and tc != "task":
                    round_map[rn].append(tc)
        for rn in sorted(round_map.keys()):
            tools = round_map[rn]
            if tools:
                # Deduplicate within round
                unique = list(dict.fromkeys(tools))
                lines.append(f"- 第{rn}轮: {', '.join(unique)}")
        if any("task" in rd.get("tools_called", []) for rd in rounds_data):
            lines.append("- (含委派专家调用)")
        lines.append("")

    # Current step guidance
    guidance = _phase_guidance(phase, ledger)
    if guidance:
        lines.append("## 本步要求")
        lines.append(guidance)

    lines.append("</diagnosis_ledger>")
    return "\n".join(lines)


def _render_hypothesis_tree(
    lines: list[str],
    ledger: DiagnosisLedger,
    ids: list[str],
    indent: int,
) -> None:
    """Recursively render the hypothesis tree."""
    prefix = "  " * indent
    for hid in ids:
        node = ledger["hypotheses"].get(hid)
        if node is None:
            continue
        marker = " ★" if node["selected"] else ""
        active_marker = " ← 当前聚焦" if (node["selected"] and node["status"] == "verifying") else ""
        lines.append(
            f"{prefix}### {node['id']} [{node['status']} p={node['probability']}%]{marker} "
            f"{node['statement']}{active_marker}"
        )
        # Evidence
        for ev in node["evidence"]:
            support_tag = "支持" if ev["supports"] else "反驳"
            lines.append(f"{prefix}  证据: {ev['summary']} ({support_tag})")
        # Verification tools
        if node["verification_tools"]:
            lines.append(f"{prefix}  已验证工具: {', '.join(node['verification_tools'])}")
        # Rationale
        if node.get("rationale") and node["status"] in ("refuted", "deprioritized", "confirmed"):
            lines.append(f"{prefix}  理由: {node['rationale'][:150]}")
        # Sub-hypotheses
        if node["sub_hypothesis_ids"]:
            _render_hypothesis_tree(lines, ledger, node["sub_hypothesis_ids"], indent + 1)


def _phase_guidance(phase: DiagnosisPhase, ledger: DiagnosisLedger) -> str:
    """Generate guidance text for the current phase."""
    if phase == "understand":
        return (
            "你当前处于 UNDERSTAND 阶段。\n"
            "- 从用户输入提取故障画像（实体、症状、时间线、变更、已尝试操作）\n"
            "- 根据症状选择 Argus 模块并行查询：\n"
            "  CPU/负载 → query_argus_cpu()\n"
            "  内存/OOM → query_argus_memory()\n"
            "  磁盘IO → query_argus_disk()\n"
            "  网络丢包/DNS → query_argus_network()\n"
            "  K8s → query_argus_kubernetes()\n"
            "- 关注指标突变时间点和并发异常（同一分钟多条红线→可能独立根因）\n"
            "- 可 read_file 读取实体 HISTORY.md 关联历史\n"
            "- ⚠ 查看上方「诊断历史」，禁止重复调用已执行过的工具\n"
            "- 完成后必须调用 commit_hypotheses 提交初始假设"
        )
    if phase == "hypothesize":
        # Detect if we were force-promoted (fault_profile still None but round>=2)
        if ledger.get("fault_profile") is None:
            return (
                "【系统强制推进】你已在 UNDERSTAND 阶段停留超过1轮但尚未提交假设。\n"
                "数据采集已完成，禁止重复调用相同诊断工具。\n"
                "- 基于上一轮已获取的数据，立即提出最多3个假设（按概率降序）\n"
                "- 必须立即调用 commit_hypotheses 提交假设，否则诊断无法推进"
            )
        return (
            "你当前处于 HYPOTHESIZE 阶段。\n"
            "- 基于当前证据，提出最多3个可能性最大的假设（按概率降序）\n"
            "- 每个假设标注概率(0-100)和依据\n"
            "- 完成后必须调用 commit_hypotheses 提交假设"
        )
    if phase == "verify":
        active_id = ledger["active_path"][-1] if ledger["active_path"] else "?"
        active = ledger["hypotheses"].get(active_id, {})
        stmt = active.get("statement", "?")
        return (
            f"你当前处于 VERIFY 阶段，聚焦验证 {active_id}: {stmt}\n"
            "- 主机假设 → task(\"host-expert\", ...) 委派验证\n"
            "- K8s 假设 → task(\"k8s-expert\", ...) 委派验证\n"
            "- GPU 假设 → task(\"gpu-expert\", ...) 委派验证\n"
            "- 委派时明确\"验证假设{active_id}\"并传入假设上下文\n"
            "- 收到结果后必须调用 record_finding 记录结论\n"
            "- ⚠ 查看上方「诊断历史」和假设的「证据」字段，禁止重复调用已有结果的工具\n"
            "- 禁止调用与当前假设无关的工具"
        )
    if phase == "evaluate":
        pending_hint = ""
        root_cause_hyp_ids = ledger.get("root_cause_hypothesis_ids", [])
        root_ids = ledger.get("root_hypothesis_ids", [])
        pending = [h for h in root_ids if h not in root_cause_hyp_ids
                   and ledger["hypotheses"].get(h, {}).get("status") not in ("refuted", "dead_end")]
        if root_cause_hyp_ids and pending:
            pending_hint = f"\n- ⚠ 仍有待验证的假设: {', '.join(pending)}，使用 select_path 切换验证"
        return (
            "你当前处于 EVALUATE 阶段。\n"
            "- 评估本层所有假设的验证结果\n"
            "- 调用 select_path 选择1条最接近根因的路径深入\n"
            "- 若根因已确认(p≥80%且足够具体) → 进入 REPORT\n"
            "- 若需深化 → 在 confirmed 假设下 HYPOTHESIZE\n"
            "- 若全部 refuted → 评估是否回溯"
            + pending_hint
        )
    if phase == "skill_verify":
        skill_ids = ledger.get("skill_ids", [])
        return (
            f"你当前处于技能驱动模式（{', '.join(skill_ids)}）。\n"
            "- 严格按 SKILL.md 的 Workflow 步骤顺序执行\n"
            "- 每步完成后用 record_finding 记录发现\n"
            "- 技能步骤完成后进入 EVALUATE；若结论不明确，降级为默认假设循环"
        )
    if phase == "backtrack":
        return (
            "你当前处于 BACKTRACK 阶段。\n"
            "- 当前路径已走入死胡同，需要回溯\n"
            "- 从 deprioritized 假设中选择次优路径重新验证\n"
            "- 调用 select_path 选择回溯目标"
        )
    if phase == "report":
        n_causes = len(ledger.get("root_causes", []))
        multi_hint = (
            f"\n- 已确认 {n_causes} 个根因，报告中需分别列出每个根因的证据链和修复建议"
            if n_causes > 1 else ""
        )
        return (
            "你当前处于 REPORT 阶段。\n"
            "- 基于诊断台账生成报告（台账即证据链）\n"
            "- 报告必须包含: 根因（如有多个，按条目分别列出）、证据链、排除的假设及排除原因"
            + multi_hint +
            f"\n- 使用 write_file 将报告写入 {{report_path}}\n"
            f"- 使用 write_file 将台账写入 {{ledger_path}}（JSON格式）"
        )
    return ""


# ── Serialization ──

def ledger_to_json(ledger: DiagnosisLedger) -> str:
    """Serialize ledger to JSON string."""
    return json.dumps(ledger, ensure_ascii=False, indent=2, default=str)


def ledger_from_json(data: str) -> DiagnosisLedger:
    """Deserialize ledger from JSON string."""
    return DiagnosisLedger(json.loads(data))


# ── Round recording ──

def record_round(
    ledger: DiagnosisLedger,
    phase: DiagnosisPhase,
    tools_called: list[str],
    action_summary: str = "",
    key_findings: str = "",
    next_plan: str = "",
) -> None:
    """Record a tool call within the current diagnosis round.

    NOTE: current_round is synced from the middleware's _model_call_count
    (via before_model hook), NOT incremented here. This ensures round
    numbers reflect actual LLM invocations, not individual tool calls.
    """
    focus_id = ledger["active_path"][-1] if ledger["active_path"] else None
    ledger["rounds"].append(RoundRecord(
        round=ledger["current_round"],
        phase=phase,
        focus_hypothesis_id=focus_id,
        action_summary=action_summary,
        tools_called=tools_called,
        key_findings=key_findings,
        next_plan=next_plan,
    ))


# ── Exit condition checking ──

def check_exit_conditions(ledger: DiagnosisLedger) -> tuple[bool, str, str | None]:
    """Check if diagnosis should exit to REPORT.

    Returns (should_exit, reason, confirmed_hypothesis_id).
    Multi-root-cause: exits only when ALL root hypotheses have reached a
    terminal state AND at least one is confirmed.  If some are confirmed
    but others are still pending/verifying, the LLM is encouraged to
    continue diagnosing the remaining symptoms.
    """
    hypotheses: dict = ledger["hypotheses"]
    if not hypotheses:
        return False, "", None

    root_ids: list[str] = ledger.get("root_hypothesis_ids", [])

    # ── Multi-root: assess each root hypothesis independently ──
    if root_ids:
        pending_ids: list[str] = []
        confirmed_ids: list[str] = []
        for hid in root_ids:
            node = hypotheses.get(hid)
            if not node:
                continue
            if node["status"] == "confirmed" and node["probability"] >= 80:
                confirmed_ids.append(hid)
            elif node["status"] in ("pending", "verifying"):
                pending_ids.append(hid)

        if confirmed_ids and pending_ids:
            # Some roots confirmed, others still pending → continue
            return False, (
                f"部分根因已确认: {', '.join(confirmed_ids)}"
                f"，仍需验证: {', '.join(pending_ids)}"
            ), confirmed_ids[0]

        if confirmed_ids and not pending_ids:
            # All roots resolved, at least one confirmed → exit
            best = hypotheses[confirmed_ids[0]]
            return True, (
                f"根因确认: {best['id']} {best['statement']}"
                f" (p={best['probability']}%)"
            ), confirmed_ids[0]

    # ── Single-root / legacy: first confirmed hypothesis triggers exit ──
    # Condition 1: Root cause confirmed — a confirmed hypothesis with p>=80%
    # that either has no sub-hypotheses (leaf) or has confirmed sub-hypotheses.
    for node in hypotheses.values():
        if node["status"] != "confirmed" or node["probability"] < 80:
            continue
        if not node["sub_hypothesis_ids"]:
            return True, f"根因确认: {node['id']} {node['statement']} (p={node['probability']}%)", node["id"]
        # Has sub-hypotheses — check if any sub is confirmed
        for sid in node["sub_hypothesis_ids"]:
            sub = hypotheses[sid]
            if sub["status"] == "confirmed" and not sub["sub_hypothesis_ids"]:
                return True, f"根因确认: {sub['id']} {sub['statement']} (p={sub['probability']}%)", sub["id"]

    # Condition 2: All hypotheses exhausted
    all_terminal = all(
        h["status"] in ("refuted", "dead_end", "confirmed")
        for h in hypotheses.values()
    )
    if all_terminal and not _can_backtrack(ledger):
        confirmed = [h for h in hypotheses.values() if h["status"] == "confirmed"]
        if not confirmed:
            return True, "假设穷尽: 所有假设已验证但未确认根因", None

    # Condition 3: Evidence saturated (3+ consecutive inconclusive)
    if ledger.get("_inconclusive_streak", 0) >= 3:
        return True, "证据饱和: 连续3次验证结果不明确", None

    return False, "", None
