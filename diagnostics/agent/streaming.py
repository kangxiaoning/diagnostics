from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AgentEvent:
    name: str
    payload: dict[str, Any]


class _ProducerError:
    """Queue marker carrying a producer-side exception to the consumer.

    Distinct from astream's tuple chunks (checked with isinstance), so
    the consumer can surface the failure as an "error" AgentEvent and
    terminate the stream gracefully instead of hanging forever.
    """

    def __init__(self, exc: Exception) -> None:
        self.exc = exc


from diagnostics.server.step_tracker import TOOL_LABELS as _TOOL_LABELS
from diagnostics.agent.ollama_chat import ReasoningLoopError  # v3.63.0 预算强制截断


def _make_tool_label(name: str) -> str:
    """Map a tool function name to a Chinese display label."""
    return _TOOL_LABELS.get(name, name)


def _format_args_summary(tc_name: str, tc_args: dict) -> str:
    """Build a compact parameter summary for the think-stream tool line.

    Same-named calls on different entities (e.g. get_argus_os_cpu_metrics on
    master-1 vs worker-3) must be distinguishable in the text stream,
    which otherwise shows only the tool label.  Long values (delegation
    descriptions, file contents, etc.) are truncated to keep the line
    readable.
    """
    if not tc_args:
        return ""
    if tc_name == "task":
        sub = (tc_args.get("subagent_type") or tc_args.get("subagent_name")
               or tc_args.get("name") or "")
        return f" → {sub}" if sub else ""
    parts = []
    for k, v in tc_args.items():
        sv = str(v).strip()
        if not sv or sv == "{}":
            continue
        if len(sv) > 40:
            sv = sv[:37] + "…"
        parts.append(f"{k}={sv}")
    return ": " + ", ".join(parts) if parts else ""


_ARG_NOISE_KEYS = ("start_time", "end_time")


def _brief_args_summary(tc_name: str, tc_args: dict) -> str:
    """Compact args summary for INFO log lines.

    Wraps `_format_args_summary`, filtering out start/end time — those
    are session-constant (identical across every call), pure noise when
    the goal is distinguishing same-named calls on different entities.
    """
    return _format_args_summary(
        tc_name, {k: v for k, v in (tc_args or {}).items()
                  if k not in _ARG_NOISE_KEYS})


def _tool_done_suffix(info: dict) -> str:
    """Build the discriminative suffix for "Tool done" log lines.

    Parallel experts and multi-entity collection issue same-named calls
    that complete in the same second; bare "Tool done: get_argus_os_cpu_metrics"
    lines then read as duplicate executions (2026-08-05 scenario-39
    round-1: 12 legitimate host-argus calls across 3 SCI nodes + 2 pod
    IPs looked like 3x duplication at INFO level).  Suffix carries the
    delegating expert and the entity-discriminating args.
    """
    bits: list[str] = []
    for elem in reversed(info.get("path") or []):
        if elem not in ("coordinator", "task"):
            bits.append(str(elem))
            break
    summary = _brief_args_summary(info.get("name", ""), info.get("args"))
    if summary:
        bits.append(summary.lstrip(": ") if not summary.startswith(" →")
                    else summary[3:])
    return f" [{'; '.join(bits)}]" if bits else ""


def _parse_file_path(text: str) -> str:
    """Extract file path from LLM text.

    Input:  "写入诊断报告到 /diagnosis_report.md"
    Output: "diagnosis_report.md"
    """
    if not text:
        return ""
    import re
    # Match: file path with extension
    m = re.search(r'(?:读取|写入|保存|编辑)\s*(?:文件|内容)?\s*[`"\'`]?([/\w.-]+\.[a-z]+)[`"\'`]?', text)
    if m:
        return m.group(1).rsplit("/", 1)[-1]
    # Generic: find any path-like pattern
    m = re.search(r'`?([/\w.-]+\.(?:md|txt|log|yaml|json))`?', text)
    return m.group(1).rsplit("/", 1)[-1] if m else ""


def _parse_task_delegation(text: str) -> tuple[str, str]:
    """Parse expert name and task description from LLM delegation text.

    Input:  "委派 memory-expert 深入分析 Spring Boot 内存使用模式"
    Output: ("memory-expert", "深入分析 Spring Boot 内存使用模式")

    Input:  "委派 kubernetes-expert 排查Pod重启原因"
    Output: ("kubernetes-expert", "排查Pod重启原因")
    """
    if not text or "委派" not in text:
        return "", ""
    import re
    # Match: 委派 <expert-name> <task-description>
    m = re.match(r"委派\s+([a-z][\w-]*)\s+(.+)", text)
    if m:
        return m.group(1), m.group(2).strip()
    # Simpler: just extract the expert name
    m = re.search(r"委派\s+([a-z][\w-]*)", text)
    if m:
        return m.group(1), text
    return "", ""


def _agent_path(namespace: tuple) -> list[str]:
    parts: list[str] = []
    for item in namespace:
        s = str(item)
        if s in ("messages", "updates", "tools", "agent", "call_model",
                 "model", "pre_model_hook", "post_model_hook", "__start__",
                 "__end__", "branch", "LangGraph"):
            continue
        parts.append(s)
    if not parts:
        parts.append("coordinator")
    return parts


def _subagent_phase(path: list[str], state: _EventState) -> str:
    """Determine text_delta phase for a subagent based on its name.

    All subagent output → thinking (collapsible reasoning section).
    Main agent handles answer-body via _finalize.
    """
    if len(path) <= 1 or path[0] == "coordinator":
        return "answering"  # main agent → answer section
    return "thinking"


def _extract_text(message: Any) -> str:
    content = getattr(message, "content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict) and item.get("type") in {"text", "text_delta"}:
                parts.append(str(item.get("text", "")))
        return "".join(parts)
    return ""


def _extract_reasoning_text(message: Any) -> str:
    """Reasoning text carried by a chunk or a full message.

    Two spellings exist in the wild: DeepSeek-style providers use
    ``reasoning_content``; this project's own model wrapper stores the
    provider field as ``reasoning`` (see ollama_chat's response hook).
    Reading a single spelling silently drops the thinking stream on the
    other backend, and the fallback path (content rendered as thinking)
    then hands the model's own reasoning back as assistant history.
    """
    addl = getattr(message, "additional_kwargs", {}) or {}
    for key in ("reasoning_content", "reasoning"):
        rc = addl.get(key)
        if isinstance(rc, str) and rc:
            return rc
    return ""


# ── Duplicate-emission guards (streaming/reporting layer) ────────────
# LangGraph's messages channel replays a full AIMessage at on_llm_end in
# addition to the token chunks (langgraph/pregel/_messages.py).  Its own
# dedupe is keyed on message.id, so a provider that returns no stable id
# on streaming chunks turns that replay into a second complete copy — the
# frontend appends both and the think body shows one turn twice.  Two
# scoped guards:
#   ① id ledger: a full snapshot contributes only its unseen tail;
#   ② snapshot guard: an id-less replay of a block already rendered in
#      full this round is dropped.
# Token-level deltas are NEVER filtered — genuine model repetition inside
# a turn is evidence and must stay visible.
_DUP_BLOCK_MIN_CHARS = 64

_RESPONSE_REASONING_ALERT_CHARS = 12000
"""Per-RESPONSE reasoning length that triggers an overthinking warning.

Distinct from the per-round counter below: one runaway response is the failure
mode that actually times out (measured 2026-10-23 scenario 2 hypothesize:
22,385 chars in a single call, no tool call emitted, 480 s timeout).  Normal
single responses stay under ~7.9k, so 12k is the earliest safe signal.
"""

_REASONING_ALERT_CHARS = 25000
"""Per-turn CUMULATIVE reasoning length that triggers an overthinking warning.

The counter accumulates across every model call inside one round, so a normal
round with three calls lands around 12k even when no single response is long.
Measured 2026-10-23 (three replays of the same scenario): single-response
peaks were 4,890-7,882 characters, per-round totals reached ~12k, and the two
timed-out turns were 25,990 and 30,041 in ONE response.  The threshold is set
above the normal per-round ceiling and below the failure regime; the earlier
12k value fired on every healthy round (noise).
"""


def _resolve_new_text(
    state: "_EventState",
    text: str,
    *,
    message_id: str,
    channel: str,
    round_no: int,
    agent_key: str,
    snapshot: bool,
) -> str:
    """Return the portion of *text* not emitted yet ("" when fully seen).

    Display-side only: the ledgers never feed back into the model context.
    """
    if not text:
        return ""

    # ① Per-message ledger (channel-scoped: reasoning and content of the
    #    same message id are different streams).
    key = f"{message_id}:{channel}" if message_id else ""
    if key:
        prev = state._sent_by_msg_id.get(key, "")
        if prev:
            if text.startswith(prev) and (snapshot or len(text) > len(prev)):
                # Snapshot, or a cumulative-snapshot chunk: only the tail
                # we have not shown is new.  An equal-length repeat on the
                # chunk channel is a genuine model repetition and stays.
                text = text[len(prev):]
            elif snapshot and prev.startswith(text):
                # Shorter replay of a block already emitted in full.
                return ""
            if not text:
                return ""
        state._sent_by_msg_id[key] = prev + text

    # ② Snapshot guard.  Only whole snapshots are checked, and only when
    #    a block of at least _DUP_BLOCK_MIN_CHARS was already rendered, so
    #    a genuine repeated sentence inside the token stream is preserved.
    round_key = (round_no, agent_key)
    if snapshot and len(text) >= _DUP_BLOCK_MIN_CHARS:
        sent = state._sent_by_round.get(round_key, "")
        if sent:
            if text.strip() in sent:
                return ""
            # Whitespace-normalised comparison: a replay whose only
            # difference is line-break/space normalisation is still the
            # same block.  Direction is one-way (new inside sent), so a
            # snapshot that extends the turn is never dropped here.
            import re
            _new = re.sub(r"\s+", "", text)
            if _new and _new in re.sub(r"\s+", "", sent):
                return ""

    state._sent_by_round[round_key] = (
        state._sent_by_round.get(round_key, "") + text)
    return text


def _extract_tool_name(tc: Any) -> str:
    if isinstance(tc, dict):
        name = tc.get("name")
        if not name:
            name = tc.get("function", {}).get("name", "unknown")
        return name or "unknown"
    return getattr(tc, "name", "unknown")


def _extract_tool_args(tc: Any) -> dict[str, Any]:
    if isinstance(tc, dict):
        # Try flat format first, then nested function.arguments
        args = tc.get("args")
        # Empty dict also means lookup failed — check nested format
        if not args:
            args = tc.get("function", {}).get("arguments")
        if args is None:
            return {}
        if isinstance(args, str):
            try:
                return json.loads(args)
            except (json.JSONDecodeError, TypeError):
                return {}
        return args if isinstance(args, dict) else {}
    args = getattr(tc, "args", {})
    if isinstance(args, str):
        try:
            return json.loads(args)
        except (json.JSONDecodeError, TypeError):
            return {}
    return args if isinstance(args, dict) else {}


def _extract_tool_id(tc: Any) -> str:
    if isinstance(tc, dict):
        return tc.get("id", "")
    return getattr(tc, "id", "")


def _message_type(msg: Any) -> str:
    return msg.__class__.__name__


@dataclass
class _EventState:
    active_tools: dict[str, dict[str, Any]] = field(default_factory=dict)
    done_tools: set[str] = field(default_factory=set)
    seen_agents: set[str] = field(default_factory=set)
    coordinator_started: bool = False
    round_number: int = 1
    _plan_tag_buf: str = ""
    _has_reasoning: bool = False
    _current_round_started: bool = True  # first round hasn't started yet
    # Map tool_call_id → subagent_name for phase routing
    _task_call_id_to_name: dict[str, str] = field(default_factory=dict)
    # Track LangGraph node type for logging (round detection is in before_model hook)
    _last_node_type: str = ""
    # Buffer for the final answer body: (text, round_number) tuples.
    # In _finalize, only text from the LAST round is emitted — intermediate
    # reasoning and step descriptions belong in per-round think sections.
    _coordinator_text: list[tuple[str, int]] = field(default_factory=list)
    # Duplicate-emission guards (see _resolve_new_text): text already sent,
    # keyed by "<message_id>:<channel>" and by (round, agent).  Display-side
    # bookkeeping only — never fed back into the model context, never used
    # to decide phases or evidence.
    _sent_by_msg_id: dict[str, str] = field(default_factory=dict)
    _sent_by_round: dict[tuple[int, str], str] = field(default_factory=dict)


async def stream_agent_events(
    agent: Any,
    messages: list[Any],
    cancel_event: asyncio.Event,
    session_id: str,
) -> AsyncIterator[AgentEvent]:
    logger.info("[session=%s] Agent stream started, %d messages",
                session_id, len(messages))
    chunk_queue: asyncio.Queue[Any] = asyncio.Queue()
    state = _EventState()

    async def _producer() -> None:
        try:
            async for raw in agent.astream(
                {"messages": messages},
                stream_mode=["messages", "updates", "custom"],
                subgraphs=True,
            ):
                await chunk_queue.put(raw)
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            # Node-level failures (e.g. NotImplementedError raised for a
            # middleware Command(goto=...)) previously killed this task
            # silently: the exception was never retrieved and the None
            # sentinel never reached the consumer, which then hung on
            # chunk_queue.get() forever — observed as a 10-minute silent
            # stall on 2026-07-19.  Surface the failure and always emit
            # the sentinel so the stream ends promptly.
            logger.exception(
                "[session=%s] Stream producer failed: %s",
                session_id, exc,
            )
            await chunk_queue.put(_ProducerError(exc))
        finally:
            await chunk_queue.put(None)

    producer_task = asyncio.ensure_future(_producer())

    async def _drain(task: asyncio.Task) -> None:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception:
            pass  # Task already failed — don't re-raise its exception

    try:
        while True:
            if cancel_event.is_set():
                await _drain(producer_task)
                logger.info("[session=%s] Stream cancelled", session_id)
                yield AgentEvent("cancelled", {"session_id": session_id})
                return

            get_task = asyncio.ensure_future(chunk_queue.get())
            cancel_task = asyncio.ensure_future(cancel_event.wait())

            done, pending = await asyncio.wait(
                {get_task, cancel_task},
                return_when=asyncio.FIRST_COMPLETED,
                timeout=30.0,  # heartbeat: log if nothing happens for 30s
            )

            # Timeout — neither task completed.
            # IMPORTANT: do NOT drain get_task! Cancelling asyncio.Queue.get()
            # permanently loses the item. Instead, keep get_task alive and
            # wait for it below. Only drain cancel_task.
            if not done:
                for task in pending:
                    if task is cancel_task:
                        await _drain(task)
                logger.info(
                    "[session=%s] Waiting for response... "
                    "(round=%d, tools_active=%d, tools_done=%d)",
                    session_id, state.round_number,
                    len(state.active_tools),
                    len(state.done_tools),
                )
                yield AgentEvent("heartbeat", {
                    "session_id": session_id,
                    "round": state.round_number,
                    "tools_active": len(state.active_tools),
                    "tools_done": len(state.done_tools),
                })
                # Fall through to wait for get_task without timeout
                done = {get_task}
                pending = set()

            for task in pending:
                await _drain(task)

            if cancel_task in done:
                await _drain(producer_task)
                logger.info("[session=%s] Stream cancelled during wait", session_id)
                yield AgentEvent("cancelled", {"session_id": session_id})
                return

            raw = await get_task
            if raw is None:
                break
            if isinstance(raw, _ProducerError):
                exc = raw.exc
                yield AgentEvent("error", {
                    "session_id": session_id,
                    "error": f"{type(exc).__name__}: {exc}",
                })
                continue

            for evt in _process_chunk(raw, state, session_id):
                yield evt

        for evt in _finalize(state):
            yield evt

    except asyncio.CancelledError:
        await _drain(producer_task)
        raise


def _process_chunk(raw: Any, state: _EventState, session_id: str = "") -> list[AgentEvent]:
    namespace: tuple = ()
    mode = "data"
    data = raw

    if isinstance(raw, tuple):
        if len(raw) == 3:
            namespace, mode, data = tuple(raw[0]), raw[1], raw[2]
        elif len(raw) == 2 and isinstance(raw[0], str):
            mode, data = raw
        elif len(raw) == 2:
            ns = tuple(raw[0]) if isinstance(raw[0], (tuple, list)) else (str(raw[0]),)
            namespace = ns
            if isinstance(raw[1], tuple) and len(raw[1]) == 2:
                mode, data = raw[1]

    events: list[AgentEvent] = []
    path = _agent_path(namespace)
    agent_key = "/".join(path)

    if not state.coordinator_started:
        state.coordinator_started = True
        state.seen_agents.add("coordinator")
        events.append(AgentEvent("agent_start", {
            "name": "coordinator", "path": ["coordinator"], "round": state.round_number,
        }))

    if agent_key not in state.seen_agents and agent_key != "coordinator":
        state.seen_agents.add(agent_key)
        events.append(AgentEvent("agent_start", {
            "name": path[-1], "path": path, "round": state.round_number,
        }))

    if mode == "messages":
        # Only process main-agent (coordinator) tool calls.
        # Skip subagent tool calls to avoid duplicate tree nodes.
        is_coordinator = path and path[0] == "coordinator"
        message = data[0] if isinstance(data, tuple) and data else data
        metadata = data[1] if isinstance(data, tuple) and len(data) > 1 else {}

        # ── Node-type tracking (NOT round detection) ──
        # Round detection is now handled exclusively by the before_model hook
        # in DiagnosisLedgerMiddleware (emits round_transition via stream_writer).
        # We only track node_type here for logging/observability purposes.
        node_type = metadata.get("langgraph_node", "")
        if is_coordinator and node_type:
            state._last_node_type = node_type

        msg_type = _message_type(message)

        if msg_type in ("AIMessageChunk", "AIMessage"):
            # A full AIMessage is a snapshot of the whole turn, not a delta
            # (LangGraph emits one at on_llm_end); it must only contribute
            # the part the token stream has not shown yet.
            is_snapshot = msg_type == "AIMessage"
            msg_id = getattr(message, "id", "") or ""

            # ① Reasoning channel (reasoning_content / reasoning)
            reasoning_raw = _extract_reasoning_text(message)
            # ② Content channel
            content_raw = _extract_text(message)
            if (reasoning_raw and content_raw.strip()
                    and content_raw.strip() == reasoning_raw.strip()):
                # Provider mirrors the reasoning stream into content: the
                # same chunk on both channels would render twice.
                content_raw = ""

            reasoning_text = _resolve_new_text(
                state, reasoning_raw,
                message_id=msg_id, channel="reasoning",
                round_no=state.round_number, agent_key=agent_key,
                snapshot=is_snapshot,
            )
            if reasoning_text:
                state._has_reasoning = True
                # v3.45.7 overthinking observability: a turn that spends its
                # budget on reasoning emits no tool call, so the failure looks
                # like "nothing happened".  Measured 2026-10-23 (scenario 17,
                # evaluate): 25,990 / 30,041 characters of reasoning with zero
                # content and zero tool calls, twice.  Normal turns stay under
                # ~8k characters, so crossing the threshold is the earliest
                # visible signal of analysis paralysis.
                _rc = getattr(state, "_reasoning_chars", 0) + len(reasoning_text)
                state._reasoning_chars = _rc
                # v3.59.0: 轮级计数只做观测，不再 WARNING。实测 2026-10-27
                # run1/run2：25,001 / 25,002 字符分别由 21 / 17 次模型调用
                # 累加而成，单响应最大未超 12k（12k 告警 0 次）——这是正常
                # 工作量，把它报成 "looping" 属误报。真正的失控信号是单响应
                # 超长（下方 12k 告警）与重复块检测（ReasoningLoopError）。
                if (_rc > _REASONING_ALERT_CHARS
                        and not getattr(state, "_reasoning_alerted", False)):
                    state._reasoning_alerted = True
                    logger.debug(
                        "[round=%d] cumulative reasoning %d chars over this "
                        "round's calls (observability only; per-response "
                        "budget guard is separate)",
                        state.round_number, _rc)

                # v3.45.10 per-response observability.  The round-level counter
                # above accumulates several calls, so a single runaway response
                # stays hidden below 25k (measured 2026-10-23 scenario 2:
                # 22,385 chars in ONE hypothesize response — no alert fired,
                # and the call timed out at 480 s).  A new ``msg_id`` marks the
                # start of another model call, which resets this counter.
                if msg_id and msg_id != getattr(state, "_resp_reasoning_id", None):
                    state._resp_reasoning_id = msg_id
                    state._resp_reasoning_chars = 0
                    state._resp_reasoning_alerted = False
                _single = (getattr(state, "_resp_reasoning_chars", 0)
                           + len(reasoning_text))
                state._resp_reasoning_chars = _single
                if (_single > _RESPONSE_REASONING_ALERT_CHARS
                        and not getattr(state, "_resp_reasoning_alerted", False)):
                    state._resp_reasoning_alerted = True
                    logger.warning(
                        "[round=%d] Single-response reasoning reached %d chars "
                        "(> %d) — looping inside one call; aborting so the "
                        "retry can regenerate with thinking disabled",
                        state.round_number, _single,
                        _RESPONSE_REASONING_ALERT_CHARS)
                    # v3.63.0 (C)：预算强制截断。此前 12k 只告警，响应继续
                    # 生成到 12.3k–16.8k、或撞 256 字符重复块才被
                    # ReasoningLoopError 收走；两次复跑 2/2 命中 12k，且
                    # 失败轮 12.0–16.8k vs 正常轮 <8k 界限清晰。
                    # 依据：Budget Forcing（s1，思考超预算即注入结束符，
                    # 精度损失 <1% 时省 ~70% 成本）。
                    # 复用 ReasoningLoopError 通道 → 走既有 thinking-off 重试。
                    raise ReasoningLoopError(
                        f"single-response reasoning reached {_single} chars "
                        f"(> {_RESPONSE_REASONING_ALERT_CHARS}) — hard cap: "
                        "aborting instead of letting it run into the timeout")
                logger.debug("[round=%d] Reasoning chunk: %d chars, first 200: %r",
                             state.round_number, len(reasoning_text), reasoning_text[:200])
                events.append(AgentEvent("text_delta", {
                    "text": reasoning_text, "path": path,
                    "round": state.round_number, "phase": "thinking",
                }))

            text = _resolve_new_text(
                state, content_raw,
                message_id=msg_id, channel="content",
                round_no=state.round_number, agent_key=agent_key,
                snapshot=is_snapshot,
            )
            tool_calls = getattr(message, "tool_calls", None)

            if text:
                logger.debug("[round=%d] LLM chunk: %d chars, first 200: %r",
                             state.round_number, len(text), text[:200])

                subagent_phase = _subagent_phase(path, state) if not is_coordinator else None

                if is_coordinator and not subagent_phase:
                    state._coordinator_text.append((text, state.round_number))
                    events.append(AgentEvent("text_delta", {
                        "text": text, "path": path,
                        "round": state.round_number, "phase": "thinking",
                    }))
                else:
                    events.append(AgentEvent("text_delta", {
                        "text": text, "path": path,
                        "round": state.round_number,
                        "phase": subagent_phase or "answering",
                    }))

            # ③ Process tool calls — both main agent and subagent.
            #    Subagent tool calls show the expert's diagnostic work in the tree.
            if tool_calls:
                tc_list = tool_calls if isinstance(tool_calls, list) else [tool_calls]

                has_new_calls = any(
                    _extract_tool_id(tc) not in state.active_tools
                    for tc in tc_list
                    if _extract_tool_id(tc) and _extract_tool_name(tc)
                )
                if has_new_calls:
                    # Inline a compact args summary per call so same-named
                    # calls on different entities are distinguishable at
                    # INFO level (pairs with the "Tool done" suffix).
                    # Args may be empty on incremental-streaming backends —
                    # the summary then degrades to the bare name, same as
                    # the previous behavior.
                    tool_descs: list[str] = []
                    for tc in tc_list:
                        _n = _extract_tool_name(tc)
                        if not _n:
                            continue
                        _s = _brief_args_summary(_n, _extract_tool_args(tc))
                        if not _s:
                            tool_descs.append(_n)
                        elif _s.startswith(" →"):
                            tool_descs.append(f"{_n}{_s}")
                        else:
                            tool_descs.append(f"{_n}({_s.lstrip(': ')})")
                    logger.info("[round=%d] LLM issued %d tool calls: [%s]",
                                state.round_number, len(tool_descs),
                                ", ".join(tool_descs))
                    # Log subagent delegation details
                    for tc in tc_list:
                        tc_name = _extract_tool_name(tc)
                        if tc_name == "task":
                            # Dump full tc dict as JSON + message additional_kwargs
                            if isinstance(tc, dict):
                                logger.info("[round=%d] task tc full: %s",
                                            state.round_number,
                                            json.dumps(tc, default=str, ensure_ascii=False)[:500])
                            # Also check parent message additional_kwargs
                            msg_addl = getattr(message, "additional_kwargs", {}) or {}
                            if msg_addl:
                                logger.info("[round=%d] task msg.additional_kwargs keys=%s",
                                            state.round_number, list(msg_addl.keys()))
                            tc_args = _extract_tool_args(tc)
                            # Also try tool_calls from additional_kwargs
                            if not tc_args:
                                tc_list_raw = msg_addl.get("tool_calls", [])
                                for raw_tc in tc_list_raw:
                                    if isinstance(raw_tc, dict) and raw_tc.get("function", {}).get("name") == "task":
                                        fn_args_str = raw_tc.get("function", {}).get("arguments", "")
                                        if fn_args_str and isinstance(fn_args_str, str):
                                            try:
                                                tc_args = json.loads(fn_args_str)
                                                logger.info("[round=%d] task args from additional_kwargs: %s",
                                                            state.round_number, tc_args)
                                            except json.JSONDecodeError:
                                                pass
                                        break
                            subagent = (tc_args.get("subagent_type") or
                                       tc_args.get("subagent_name") or
                                       tc_args.get("name") or "?")
                            desc = (tc_args.get("description") or
                                   tc_args.get("prompt") or
                                   tc_args.get("instructions") or "?")
                            logger.info("[round=%d] → delegating to subagent '%s': %s",
                                        state.round_number, subagent,
                                        desc[:120] if desc else "(no description)")
                            # Remember call_id → subagent_name for phase routing
                            tc_id = _extract_tool_id(tc)
                            if tc_id and subagent != "?":
                                state._task_call_id_to_name[tc_id] = subagent
                                logger.info("[round=%d] → mapped call_id=%s → '%s'",
                                            state.round_number, tc_id, subagent)

                for tc in tc_list:
                    tc_id = _extract_tool_id(tc)
                    tc_name = _extract_tool_name(tc)
                    tc_args = _extract_tool_args(tc)
                    # Log every tool call with its args for traceability
                    # (previously only a small whitelist had it, so expert
                    # queries like get_argus_os_cpu_metrics were indistinguishable).
                    logger.debug("[round=%d] %s args: %s",
                                 state.round_number, tc_name,
                                 json.dumps(tc_args, ensure_ascii=False)[:300])
                    if tc_id and tc_name:
                        label = _make_tool_label(tc_name)
                        if tc_id in state.active_tools:
                            if tc_args:
                                old_args = state.active_tools[tc_id].get("args", {})
                                state.active_tools[tc_id]["args"] = tc_args
                                state.active_tools[tc_id]["label"] = label
                                # Emit update when args become available after first empty chunk
                                if not old_args:
                                    events.append(AgentEvent("tool_args_available", {
                                        "id": tc_id, "name": tc_name, "args": tc_args,
                                    }))
                        else:
                            state.active_tools[tc_id] = {
                                "name": tc_name, "args": tc_args,
                                "label": label, "path": path,
                            }
                            # Task delegation fallback when args are empty
                            if tc_name == "task" and not tc_args:
                                expert = _extract_expert_from_text(text)
                                if expert:
                                    tc_args = {"subagent_type": expert}
                            elif tc_name in ("read_file", "write_file", "edit_file") and not tc_args:
                                fp = _parse_file_path("")
                                if fp:
                                    tc_args = {"path": fp}
                            events.append(AgentEvent("tool_start", {
                                "id": tc_id, "name": tc_name, "args": tc_args,
                                "label": label, "path": path,
                                "round": state.round_number,
                                "description": "",
                            }))
                            # Emit tool call to the current round's think
                            # section, with a compact args summary so
                            # same-named calls on different entities are
                            # distinguishable in the text stream.
                            events.append(AgentEvent("text_delta", {
                                "text": f"\n\n🔧 {label}"
                                        f"{_format_args_summary(tc_name, tc_args)}",
                                "path": path,
                                "round": state.round_number,
                                "phase": "thinking",
                            }))

        elif msg_type == "ToolMessage":
            tc_id = getattr(message, "tool_call_id", "")
            if tc_id and tc_id not in state.done_tools:
                output = _extract_text(message)
                info = state.active_tools.pop(tc_id, None)
                # Skip orphaned tool_end events — the corresponding
                # tool_start was filtered out.  Do NOT add to
                # done_tools here — that would inflate the counter
                # and cause tools_active to go negative.
                if info is None:
                    logger.debug("[round=%d] Skipping orphaned ToolMessage (no tool_start): %s",
                                 state.round_number, tc_id)
                else:
                    state.done_tools.add(tc_id)
                    logger.info("[round=%d] Tool done: %s%s", state.round_number,
                                info["label"], _tool_done_suffix(info))
                    # Deduped nodes should be removed from the frontend tree
                    # so users don't see duplicated identical calls.
                    end_evt = AgentEvent("tool_end", {
                        "id": tc_id,
                        "name": info["name"],
                        "label": info["label"],
                        "path": info["path"],
                        "output": output[:2000],
                        "output_full": output,
                        "round": state.round_number,
                    })
                    if info.get("_deduped"):
                        end_evt.payload["_deduped"] = True
                    events.append(end_evt)
                    # Emit tool result summary to the current round's think
                    # section — keep the args summary so the result line
                    # matches its start line for same-named calls.
                    label = info["label"]
                    name = info.get("name", "")
                    args = info.get("args") or {}
                    summary = _summarize_output(output)
                    events.append(AgentEvent("text_delta", {
                        "text": f"\n📊 **{label}"
                                f"{_format_args_summary(name, args)}**: {summary}",
                        "path": path,
                        "round": state.round_number,
                        "phase": "thinking",
                    }))

    elif mode == "updates":
        if isinstance(data, dict):
            for _node_name, value in data.items():
                if isinstance(value, dict) and "messages" in value:
                    msgs = value["messages"]
                    if msgs:
                        last = msgs[-1]
                        if _message_type(last) == "ToolMessage":
                            tc_id = getattr(last, "tool_call_id", "")
                            if tc_id and tc_id not in state.done_tools:
                                output = _extract_text(last)
                                info = state.active_tools.pop(tc_id, None)
                                # Skip orphaned tool_end (no matching tool_start).
                                # Do NOT add to done_tools here — same reason
                                # as the messages-mode handler above.
                                if info is None:
                                    logger.debug("[round=%d] Skipping orphaned ToolMessage (updates): %s",
                                                 state.round_number, tc_id)
                                else:
                                    state.done_tools.add(tc_id)
                                    logger.info("[round=%d] Tool done (updates): %s%s",
                                                state.round_number, info["label"],
                                                _tool_done_suffix(info))
                                    end_evt = AgentEvent("tool_end", {
                                        "id": tc_id,
                                        "name": info["name"],
                                        "label": info["label"],
                                        "path": info["path"],
                                        "output": output[:2000],
                                        "output_full": output,
                                        "round": state.round_number,
                                    })
                                    if info.get("_deduped"):
                                        end_evt.payload["_deduped"] = True
                                    events.append(end_evt)
                # 🔑 Extract COMPLETE tool call args from model_request (updates mode).
                #    LM Studio/Qwen streaming sends empty args in messages mode;
                #    updates mode has the real, complete args from LangGraph state.
                if isinstance(value, dict) and "messages" in value:
                    for msg in value["messages"]:
                        tcs = getattr(msg, "tool_calls", None)
                        if tcs:
                            for tc in tcs:
                                tc_id = _extract_tool_id(tc)
                                tc_name = _extract_tool_name(tc)
                                tc_args = _extract_tool_args(tc)
                                if tc_id and tc_name and tc_args:
                                    info = state.active_tools.get(tc_id)
                                    if info and not info.get("args"):
                                        info["args"] = tc_args
                                        logger.info("[round=%d] Args from updates for '%s': %s",
                                                    state.round_number, tc_name, tc_args)
                                        events.append(AgentEvent("tool_args_available", {
                                            "id": tc_id, "name": tc_name, "args": tc_args,
                                        }))
                # Also check for structured_response in updates.
                # The key alone is not enough: a guard that clears the
                # structured response (design document §8 G27/G28) emits
                # the key with a None value, and reporting that as
                # "received" is exactly the false positive the guard is
                # trying to remove.
                if isinstance(value, dict) and value.get("structured_response") is not None:
                    sr = value["structured_response"]
                    logger.info("[round=%d] Structured response received", state.round_number)
                    events.append(AgentEvent("structured_response", _serialize_sr(sr)))

                # 🔑 Detect diagnosis ledger updates via custom stream
                # DiagnosisLedgerMiddleware emits ledger snapshots via stream_writer
                # when propose_hypotheses/select_path/record_finding tools fire.
                if isinstance(value, dict) and "_diagnosis_ledger" in value:
                    ledger = value["_diagnosis_ledger"]
                    if ledger and isinstance(ledger, dict):
                        from diagnostics.agent.ledger import derive_phase
                        logger.info("[round=%d] Diagnosis ledger updated (state): phase=%s, hypotheses=%d",
                                    state.round_number,
                                    derive_phase(ledger),
                                    len(ledger.get("hypotheses", {})))
                        events.append(AgentEvent("ledger_snapshot", {
                            "ledger": _sanitize_ledger_for_event(ledger),
                            "round": state.round_number,
                        }))

    elif mode == "custom":
        # Custom stream events — used by DiagnosisLedgerMiddleware to emit
        # ledger snapshots and report content via stream_writer.
        if isinstance(data, dict):
            if data.get("type") == "ledger_snapshot":
                ledger = data.get("ledger")
                if ledger and isinstance(ledger, dict):
                    from diagnostics.agent.ledger import derive_phase
                    logger.info("[round=%d] Diagnosis ledger updated (custom): phase=%s, hypotheses=%d",
                                state.round_number,
                                derive_phase(ledger),
                                len(ledger.get("hypotheses", {})))
                    events.append(AgentEvent("ledger_snapshot", {
                        "ledger": _sanitize_ledger_for_event(ledger),
                        "round": state.round_number,
                    }))
            elif data.get("type") == "report_content":
                content = data.get("content", "")
                if content:
                    logger.info("[round=%d] Report content captured from write_file (%d chars)",
                                state.round_number, len(content))
                    events.append(AgentEvent("text_delta", {
                        "text": content,
                        "path": ["coordinator"],
                        "round": state.round_number,
                        "phase": "answering",
                        "write_failed": bool(data.get("write_failed")),
                        "error": data.get("error", ""),
                    }))
            # ── Custom events from middleware hooks ──
            elif data.get("type") == "round_transition":
                # Emitted by before_model hook in DiagnosisLedgerMiddleware.
                # More reliable round detection than node-transition heuristic.
                new_round = data.get("round", 0)
                if new_round > state.round_number:
                    state.round_number = new_round
                    # Duplicate-guard buffers are per round; older entries
                    # can never be consulted again (cross-round similarity
                    # is deliberately NOT collapsed — it is evidence).
                    for _key in [k for k in state._sent_by_round
                                 if k[0] < new_round]:
                        state._sent_by_round.pop(_key, None)
                    logger.info("[round=%d] Round transition from before_model hook",
                                state.round_number)
                events.append(AgentEvent("round_transition", data))
            elif data.get("type") in ("tool_executing", "tool_completed"):
                # Precise tool timing from awrap_tool_call
                events.append(AgentEvent(data["type"], data))
            elif data.get("type") == "prompt_injection":
                # Prompt-side injection delta (phase guidance / safety
                # warnings / evidence audit) from awrap_model_call —
                # recorded into the trace file by the server, not
                # forwarded to the frontend.
                events.append(AgentEvent("prompt_injection", data))
            elif data.get("type") == "tool_args_corrected":
                # Emitted after correcting LLM-invented args.
                # Re-emit tool_args_available so
                # frontend tree nodes show correct (post-middleware) args.
                tc_id = data.get("id")
                tc_name = data.get("name", "")
                tc_args = data.get("args", {})
                if tc_id and tc_args:
                    info = state.active_tools.get(tc_id)
                    if info:
                        info["args"] = tc_args
                        events.append(AgentEvent("tool_args_available", {
                            "id": tc_id, "name": tc_name, "args": tc_args,
                        }))
            elif data.get("type") == "tool_dedup":
                # Emitted by dedup_middleware when a duplicate tool call
                # is caught (P0 cache / P1 in-flight / cross-agent).
                # Mark the node so tool_end carries the dedup flag; the
                # frontend keeps the node and renders a "dedup" badge.
                tc_id = data.get("id")
                if tc_id:
                    info = state.active_tools.get(tc_id)
                    if info:
                        info["_deduped"] = True

    return events


def _extract_expert_from_text(text: str) -> str:
    """Try to extract subagent name from raw LLM text."""
    if not text:
        return ""
    import re
    m = re.search(r'委派\s+([a-z][\w-]*)', text)
    return m.group(1) if m else ""


def _serialize_sr(sr: Any) -> dict[str, Any]:
    """Serialize a Pydantic structured_response to a plain dict."""
    if hasattr(sr, "model_dump"):
        return sr.model_dump()
    elif hasattr(sr, "dict"):
        return sr.dict()
    elif isinstance(sr, dict):
        return sr
    return {"value": str(sr)}


def _sanitize_ledger_for_event(ledger: dict[str, Any]) -> dict[str, Any]:
    """Strip internal fields from the ledger before sending to frontend.

    Also injects the derived ``current_phase`` — the ledger itself no
    longer persists it (design document §3), but the frontend banner
    still consumes this key, so we compute it on the way out.
    """
    internal_keys = {"_inconclusive_streak", "_backtrack_count"}
    out = {k: v for k, v in ledger.items() if k not in internal_keys}
    # Evidence excerpts (design document §6.6, v3.37.0) are for the report-time
    # LLM context and the persisted ledger file — drop them from UI payloads.
    try:
        from diagnostics.agent.evidence_ledger import drop_excerpts
        out = drop_excerpts(out)
    except Exception:
        pass  # best-effort; excerpts only affect payload size
    try:
        from diagnostics.agent.ledger import derive_phase
        out["current_phase"] = derive_phase(ledger)
    except Exception:
        pass  # best-effort; banner falls back to "understand"
    return out


# ════════════════ ToolMessage handling ════════════════

# (ToolMessage handling is in _process_chunk messages/updates sections above)


# ════════════════ Finalize ════════════════

def _summarize_output(output: str, max_len: int = 80) -> str:
    """Create a brief summary of tool output for the thinking section."""
    if not output:
        return "(无输出)"
    text = output.strip()
    if len(text) <= max_len:
        return text
    return text[:max_len].rstrip() + "…"


def _finalize(state: _EventState) -> list[AgentEvent]:
    events: list[AgentEvent] = []

    # Report content is captured from write_file via custom stream event
    # and emitted as text_delta(phase="answering") during streaming.
    # Fallback: if no report was captured, use plan_tag_buf if available.
    if state._plan_tag_buf.strip():
        events.append(AgentEvent("text_delta", {
            "text": state._plan_tag_buf,
            "path": ["coordinator"], "round": state.round_number,
            "phase": "answering",
        }))

    for tc_id, info in state.active_tools.items():
        if tc_id in state.done_tools:
            continue
        state.done_tools.add(tc_id)
        logger.warning("[round=%d] Finalize: closing unfinished tool %s",
                       state.round_number, info["label"])
        events.append(AgentEvent("tool_end", {
            "id": tc_id, "name": info["name"], "label": info["label"],
            "path": info["path"], "output": "", "output_full": "",
            "round": state.round_number,
        }))

    for agent_key in sorted(state.seen_agents, reverse=True):
        path = agent_key.split("/")
        events.append(AgentEvent("agent_end", {
            "path": path, "round": state.round_number,
        }))

    return events
