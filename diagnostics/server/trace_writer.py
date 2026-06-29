"""Trace writer — saves user input, LLM output, tool calls with results as Markdown.

Tool results are inserted immediately after the corresponding tool_start header,
using index tracking for correct ordering regardless of event arrival timing.
LLM text chunks are accumulated and flushed as single paragraphs.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

TRACES_DIR = Path(__file__).resolve().parent.parent.parent / "agent_data" / "traces"


def _render_trace_hypothesis(
    lines: list[str],
    hypotheses: dict,
    hid: str,
    indent: int,
) -> None:
    """Recursively render a hypothesis node for the trace file."""
    node = hypotheses.get(hid)
    if not node:
        return
    prefix = "  " * indent
    star = " ★" if node.get("selected") else ""
    status = node.get("status", "pending")
    prob = node.get("probability", 0)
    stmt = node.get("statement", "")
    lines.append(f"{prefix}- `{hid}` [{status} p={prob}%]{star} {stmt}")
    for ev in node.get("evidence", []):
        tag = "支持" if ev.get("supports") else "反驳"
        lines.append(f"{prefix}  - ({tag}) {ev.get('summary', '')[:100]}")
    for sid in node.get("sub_hypothesis_ids", []):
        _render_trace_hypothesis(lines, hypotheses, sid, indent + 1)


class TraceWriter:
    """Persist diagnostic traces: user input → LLM text → tool calls → results.

    Usage::

        trace = TraceWriter(report_path, entity_type="kubernetes", entity_name="prod-us-east")
        trace.start(user_message="集群 prod-us-east worker-5/8 NotReady...")
        trace.llm_text(round_num, text)
        trace.tool_start(round_num, tool_name, tool_args, description)
        trace.tool_end(round_num, tool_name, output_full)
        trace.finalize()
    """

    def __init__(self, report_path: str, **info: str) -> None:
        TRACES_DIR.mkdir(parents=True, exist_ok=True)
        stem = Path(report_path).stem if report_path else "trace"
        self._path = TRACES_DIR / f"{stem}-trace.md"
        self._info = info
        self._started_at = datetime.now(timezone.utc)
        self._lines: list[str] = []
        self._current_round = 0
        self._llm_buffer: list[str] = []          # accumulate streaming chunks
        self._llm_header_inserted: bool = False   # "### LLM 输出" header flag
        self._tool_result_slots: dict[int, list[str]] = {}
        self._tool_call_id_to_slot: dict[str, int] = {}  # tool_call_id → slot index
        self._ledger_sections: list[str] = []     # hypothesis tree snapshots

    # ── Public API ──

    def start(self, user_message: str = "") -> None:
        ts = self._started_at.strftime("%Y-%m-%d %H:%M:%S UTC")
        parts = [f"# 诊断追踪\n\n**时间**: {ts}"]
        if self._info:
            parts.append("  ".join(
                f"**{k}**: {v}" for k, v in self._info.items()
            ))
        if user_message:
            parts.append(f"\n## 用户输入\n\n{user_message}")
        parts.append("")
        self._lines = parts

    def llm_text(self, round_num: int, text: str) -> None:
        """Accumulate LLM streaming chunks for the current round.

        Flushed to a single paragraph when a tool_start or round change occurs.
        Filters out synthetic 🔧/📊 markers injected by streaming.py.
        """
        cleaned = _clean_llm_text(text)
        if not cleaned:
            return
        self._ensure_round(round_num)
        self._llm_buffer.append(cleaned)

    def tool_start(
        self,
        round_num: int,
        tool_name: str,
        tool_args: str,
        description: str = "",
        tool_call_id: str = "",
    ) -> None:
        self._ensure_round(round_num)
        self._flush_llm_buffer()
        desc = f" — {description}" if description else ""
        parts = [f"#### 🔧 **{tool_name}**{desc}"]
        if tool_args:
            parts.append(f"> 参数: `{tool_args}`")
        parts.append("")
        header = "\n".join(parts)
        self._lines.append(header)
        idx = len(self._lines) - 1
        self._lines.append("> ⏳ 执行中…\n")
        self._tool_result_slots[idx] = ["", "> ⏳ 执行中…", ""]
        if tool_call_id:
            self._tool_call_id_to_slot[tool_call_id] = idx

    def tool_end(
        self,
        round_num: int,
        tool_name: str,
        output_full: str,
        tool_call_id: str = "",
    ) -> None:
        _ = round_num, tool_name
        if not output_full:
            return
        summary = output_full[:3000].rstrip()
        suffix = (
            f"\n\n> ... (truncated, {len(output_full)} chars total)"
            if len(output_full) > 3000 else ""
        )
        indented = "> " + summary.replace("\n", "\n> ") + suffix
        result_lines = ["", indented, ""]

        # ── Match by tool_call_id when available (parallel calls) ──
        # Batch tool calls (e.g. 5× record_finding) execute in parallel via
        # asyncio.  tool_end events may arrive in a different order than
        # tool_start.  Matching by tool_call_id ensures correct pairing.
        target_idx: int | None = None
        if tool_call_id and tool_call_id in self._tool_call_id_to_slot:
            idx = self._tool_call_id_to_slot.pop(tool_call_id)
            if idx in self._tool_result_slots:
                target_idx = idx

        # Fallback: FIFO (no tool_call_id provided or not found)
        if target_idx is None:
            for idx in sorted(self._tool_result_slots):
                old = self._tool_result_slots[idx]
                if old[1].startswith("> ⏳"):
                    target_idx = idx
                    break

        if target_idx is not None:
            self._lines[target_idx + 1] = result_lines[0]
            self._lines.insert(target_idx + 2, result_lines[1])
            self._lines.insert(target_idx + 3, result_lines[2])
            self._tool_result_slots.pop(target_idx, None)
            _fix_slots_after_insert(self._tool_result_slots, target_idx, 2)
            # Rebuild id→slot mapping after index shift
            self._rebuild_id_slot_map(target_idx, 2)

    def ledger_update(self, ledger: dict) -> None:
        """Record a diagnosis ledger snapshot (hypothesis tree evolution).

        Called when commit_hypotheses / select_path / record_finding fires.
        Accumulates compact snapshots that are appended to the trace on finalize.
        """
        if not ledger or not isinstance(ledger, dict):
            return
        phase = ledger.get("current_phase", "?")
        round_num = ledger.get("current_round", 0)
        active = ledger.get("active_path", [])
        hypotheses = ledger.get("hypotheses", {})
        root_ids = ledger.get("root_hypothesis_ids", [])

        lines = [f"### 📋 台账更新 (步骤 {round_num}, 阶段: {phase})"]
        if active:
            lines.append(f"**活动路径**: {' → '.join(active)}")
        if root_ids:
            lines.append("**假设树**:")
            for hid in root_ids:
                _render_trace_hypothesis(lines, hypotheses, hid, 0)
        if ledger.get("root_cause"):
            lines.append(f"**🎯 根因**: {ledger['root_cause']}")

        self._ledger_sections.append("\n".join(lines))

    def finalize(self) -> None:
        self._flush_llm_buffer()
        for idx in list(self._tool_result_slots):
            old = self._tool_result_slots[idx]
            if old[1].startswith("> ⏳"):
                self._lines[idx + 1] = "> (无输出)"
                self._tool_result_slots.pop(idx, None)
        duration = (datetime.now(timezone.utc) - self._started_at).total_seconds()
        self._lines.insert(1, f"**耗时**: {duration:.1f}s\n")
        # Append hypothesis tree evolution if any
        if self._ledger_sections:
            self._lines.append("\n## 假设树演变\n")
            for section in self._ledger_sections:
                self._lines.append(section)
                self._lines.append("")
        self._lines.append(
            f"\n---\n*追踪文件自动生成于 "
            f"{datetime.now(timezone.utc).isoformat()}*"
        )
        content = "\n".join(self._lines)
        self._path.write_text(content, encoding="utf-8")
        logger.info("Trace written to %s (%d lines, %.1fs)",
                     self._path, len(self._lines), duration)

    # ── Internal ──

    def _ensure_round(self, round_num: int) -> None:
        if round_num != self._current_round:
            self._flush_llm_buffer()
            self._current_round = round_num
            self._llm_header_inserted = False
            self._lines.append(f"\n## 第{round_num}轮\n")

    def _rebuild_id_slot_map(self, base: int, offset: int) -> None:
        """Re-sync _tool_call_id_to_slot after line insertions shifted indices."""
        for tid, idx in list(self._tool_call_id_to_slot.items()):
            if idx > base:
                self._tool_call_id_to_slot[tid] = idx + offset

    def _flush_llm_buffer(self) -> None:
        if not self._llm_buffer:
            return
        text = "".join(self._llm_buffer).strip()
        self._llm_buffer = []
        if not text:
            return
        if not self._llm_header_inserted:
            self._lines.append(f"### LLM 输出\n")
            self._llm_header_inserted = True
        self._lines.append(text)


def _clean_llm_text(text: str) -> str:
    """Remove synthetic tool call markers injected by streaming.py."""
    import re
    text = re.sub(r'\n?\n?🔧 .+', '', text)
    text = re.sub(r'\n?\n?📊 .+', '', text)
    return text.strip()


def _fix_slots_after_insert(
    slots: dict[int, list[str]], base: int, offset: int
) -> None:
    to_update = sorted(k for k in slots if k > base)
    for k in reversed(to_update):
        slots[k + offset] = slots.pop(k)
