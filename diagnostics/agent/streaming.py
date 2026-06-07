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


from diagnostics.server.step_tracker import TOOL_LABELS as _TOOL_LABELS


def _make_tool_label(name: str) -> str:
    """Map a tool function name to a Chinese display label."""
    return _TOOL_LABELS.get(name, name)


def _make_tool_description(name: str, args: dict[str, Any]) -> str:
    """Generate a tool description from label and arguments when LLM doesn't provide one."""
    label = _TOOL_LABELS.get(name, name)
    if args:
        # Pick the most descriptive arg value
        for key in ("subagent_type", "path", "namespace", "profile", "filename"):
            val = args.get(key)
            if isinstance(val, str) and val.strip():
                return f"{label}({val})"
        # Generic: first non-empty string value
        for v in args.values():
            if isinstance(v, str) and v.strip() and v.strip() != "{}":
                return f"{label}({v})"
    return label


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

    report-writer → answering (visible diagnostic report)
    all others   → thinking  (collapsible reasoning section)
    """
    if len(path) <= 1 or path[0] == "coordinator":
        return "answering"  # main agent → answer section
    # Extract tool_call_id from ns path (e.g. "tools:call_abc123" → "call_abc123")
    call_id = path[0]
    if ":" in call_id:
        call_id = call_id.split(":", 1)[1]
    name = state._task_call_id_to_name.get(call_id, "")
    return "answering" if name == "report-writer" else "thinking"


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
    addl = getattr(message, "additional_kwargs", {}) or {}
    rc = addl.get("reasoning_content", "")
    if isinstance(rc, str):
        return rc
    return ""


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
    _think_tag_buf: str = ""
    _has_reasoning: bool = False
    # <step> tag parsing state
    _step_tag_buf: str = ""
    _pending_step_descriptions: list[str] = field(default_factory=list)
    # Accumulated think text for fallback description extraction
    _accumulated_think: list[str] = field(default_factory=list)
    _current_round_started: bool = True  # first round hasn't started yet
    # Map tool_call_id → subagent_name for phase routing
    _task_call_id_to_name: dict[str, str] = field(default_factory=dict)


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
                stream_mode=["messages", "updates"],
                subgraphs=True,
            ):
                await chunk_queue.put(raw)
            await chunk_queue.put(None)
        except asyncio.CancelledError:
            pass

    producer_task = asyncio.ensure_future(_producer())

    async def _drain(task: asyncio.Task) -> None:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

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
            )
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
        msg_type = _message_type(message)

        if msg_type in ("AIMessageChunk", "AIMessage"):
            # ① Extract reasoning_content from additional_kwargs (DeepSeek-R1)
            reasoning_text = _extract_reasoning_text(message)
            if reasoning_text:
                state._has_reasoning = True
                logger.debug("[round=%d] Reasoning chunk: %d chars, first 200: %r",
                             state.round_number, len(reasoning_text), reasoning_text[:200])
                events.append(AgentEvent("text_delta", {
                    "text": reasoning_text, "path": path,
                    "round": state.round_number, "phase": "thinking",
                }))

            # ② Extract content text
            text = _extract_text(message)
            tool_calls = getattr(message, "tool_calls", None)

            if text:
                logger.debug("[round=%d] LLM chunk: %d chars, first 200: %r",
                             state.round_number, len(text), text[:200])

                # ②a  Parse <step> tags FIRST from raw text — before think/answer split.
                #     This ensures step descriptions are captured regardless of whether
                #     the LLM puts <step> inside or outside <think> blocks.
                prev_step_count = len(state._pending_step_descriptions)
                clean_text = _parse_step_tags(text, state)
                new_steps_added = len(state._pending_step_descriptions) > prev_step_count

                # ②b  Then split think/answer from the cleaned text
                think_text, answer_text = _parse_think_tags(clean_text, state)

                if think_text or answer_text:
                    logger.debug("[round=%d] Parsed: think=%d chars (%.80r) answer=%d chars (%.80r)",
                                 state.round_number,
                                 len(think_text), think_text[:80] if think_text else "",
                                 len(answer_text), answer_text[:80] if answer_text else "")

                # Determine default phase: subagents use name-based routing,
                # main agent uses think/answer split
                subagent_phase = _subagent_phase(path, state) if not is_coordinator else None

                if think_text:
                    state._has_reasoning = True
                    state._accumulated_think.append(think_text)
                    phase = subagent_phase or "thinking"
                    events.append(AgentEvent("text_delta", {
                        "text": think_text, "path": path,
                        "round": state.round_number, "phase": phase,
                    }))

                if answer_text:
                    phase = subagent_phase or "answering"
                    events.append(AgentEvent("text_delta", {
                        "text": answer_text, "path": path,
                        "round": state.round_number, "phase": phase,
                    }))
                elif not think_text and not clean_text.strip() and new_steps_added:
                    # Step tags were parsed — emit the NEW steps as text so
                    # frontend think sections and answer body have content.
                    # Only emit when steps were actually added in THIS chunk
                    # to avoid repeating the same text for every mid-tag chunk.
                    new_steps = state._pending_step_descriptions[prev_step_count:]
                    step_text = "\n".join(new_steps)
                    if step_text.strip():
                        state._accumulated_think.append(step_text)
                        if is_coordinator and not subagent_phase:
                            events.append(AgentEvent("text_delta", {
                                "text": step_text, "path": path,
                                "round": state.round_number, "phase": "thinking",
                            }))
                        events.append(AgentEvent("text_delta", {
                            "text": step_text, "path": path,
                            "round": state.round_number,
                            "phase": subagent_phase or "answering",
                        }))
                elif not think_text and clean_text.strip():
                    # No think tags at all — route by agent.
                    # For coordinator: also emit as thinking so the frontend
                    # creates per-round collapsible sections for each LLM turn.
                    phase = subagent_phase or "answering"
                    if is_coordinator and not subagent_phase:
                        events.append(AgentEvent("text_delta", {
                            "text": clean_text, "path": path,
                            "round": state.round_number, "phase": "thinking",
                        }))
                    events.append(AgentEvent("text_delta", {
                        "text": clean_text, "path": path,
                        "round": state.round_number, "phase": phase,
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
                    state.round_number += 1
                    tool_names = [_extract_tool_name(tc) for tc in tc_list
                                  if _extract_tool_name(tc)]
                    logger.info("[round=%d] LLM issued %d tool calls: %s",
                                state.round_number, len(tool_names), tool_names)
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

                # Build descriptions: pop from <step> tags first,
                # fall back to extracting from accumulated think text
                tool_count = len([tc for tc in tc_list
                                  if _extract_tool_id(tc) and _extract_tool_name(tc)])
                step_descs = _build_tool_descriptions(state, tool_count)

                idx = 0
                for tc in tc_list:
                    tc_id = _extract_tool_id(tc)
                    tc_name = _extract_tool_name(tc)
                    tc_args = _extract_tool_args(tc)
                    # Log file tools and task for args inspection
                    if tc_name in ("read_file", "write_file", "edit_file",
                                   "write_todos", "task", "glob", "grep", "ls"):
                        logger.debug("[round=%d] %s args: %s",
                                     state.round_number, tc_name, tc_args)
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
                            description = step_descs[idx] if idx < len(step_descs) else ""
                            idx += 1
                            if tc_name == "task" and not tc_args:
                                # Parse expert name from ALL available text sources
                                sources = [description] + state._pending_step_descriptions[-2:] + list(state._accumulated_think[-3:])
                                if text:
                                    sources.append(text)  # raw LLM chunk (may contain "委派 XXX")
                                fallback_text = " ".join(s for s in sources if s)
                                expert, task_desc = _parse_task_delegation(fallback_text)
                                if expert:
                                    tc_args = {"subagent_type": expert}
                                    if task_desc:
                                        description = task_desc
                                logger.info("[round=%d] task delegation parsed: expert=%s, sources=%d, from_text=%.80s",
                                            state.round_number, expert, len([s for s in sources if s]),
                                            fallback_text[:80])
                            elif tc_name in ("read_file", "write_file", "edit_file") and not tc_args:
                                # Parse file path from step description
                                path = _parse_file_path(description)
                                if path:
                                    tc_args = {"path": path}
                                    description = f"操作: {path}"
                                    logger.info("[round=%d] %s parsed: path=%s",
                                                state.round_number, tc_name, path)
                            if not description:
                                if tc_name == "task":
                                    sub = tc_args.get("subagent_type", "")
                                    description = f"委派给 {sub}" if sub else ""
                                else:
                                    description = _make_tool_description(tc_name, tc_args)
                                logger.warning("[round=%d] No description for '%s', "
                                             "generated from args: %r",
                                             state.round_number, label, description[:80])
                            events.append(AgentEvent("tool_start", {
                                "id": tc_id, "name": tc_name, "args": tc_args,
                                "label": label, "path": path,
                                "round": state.round_number,
                                "description": description,
                            }))
                            # Emit tool call to answer section (execution trace)
                            step_info = description or label
                            events.append(AgentEvent("text_delta", {
                                "text": f"\n\n🔧 {step_info}",
                                "path": path,
                                "round": state.round_number,
                                "phase": "answering",
                            }))

        elif msg_type == "ToolMessage":
            tc_id = getattr(message, "tool_call_id", "")
            if tc_id and tc_id not in state.done_tools:
                state.done_tools.add(tc_id)
                output = _extract_text(message)
                info = state.active_tools.pop(tc_id, None)
                if info:
                    logger.info("[round=%d] Tool done: %s", state.round_number, info["label"])
                events.append(AgentEvent("tool_end", {
                    "id": tc_id,
                    "name": info["name"] if info else "unknown",
                    "label": info["label"] if info else "工具调用",
                    "path": info["path"] if info else path,
                    "output": output[:2000],
                    "output_full": output,
                    "round": state.round_number,
                }))
                # Emit tool result summary to answer section
                label = info["label"] if info else "工具调用"
                summary = _summarize_output(output)
                events.append(AgentEvent("text_delta", {
                    "text": f"\n📊 {label} 返回: {summary}",
                    "path": path,
                    "round": state.round_number,
                    "phase": "answering",
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
                                state.done_tools.add(tc_id)
                                output = _extract_text(last)
                                info = state.active_tools.pop(tc_id, None)
                                if info:
                                    logger.info("[round=%d] Tool done (updates): %s",
                                                state.round_number, info["label"])
                                events.append(AgentEvent("tool_end", {
                                    "id": tc_id,
                                    "name": info["name"] if info else "unknown",
                                    "label": info["label"] if info else "工具调用",
                                    "path": info["path"] if info else path,
                                    "output": output[:2000],
                                    "output_full": output,
                                    "round": state.round_number,
                                }))
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
                # Also check for structured_response in updates
                if isinstance(value, dict) and "structured_response" in value:
                    sr = value["structured_response"]
                    logger.info("[round=%d] Structured response received", state.round_number)
                    events.append(AgentEvent("structured_response", _serialize_sr(sr)))

    return events


def _route_answer_text(text: str, path: list[str], state: _EventState,
                       events: list[AgentEvent]) -> None:
    """Parse <step> tags from text and emit remaining text as text_delta."""
    remaining = _parse_step_tags(text, state)

    if remaining.strip():
        events.append(AgentEvent("text_delta", {
            "text": remaining, "path": path,
            "round": state.round_number, "phase": "answering",
        }))


def _build_tool_descriptions(state: _EventState, tool_count: int) -> list[str]:
    """Build descriptions for upcoming tool calls.

    Priority:
      1. Pop from <step> tags parsed by LLM
      2. Extract from accumulated think text per tool
      3. Generate from tool name + args (if available in active_tools)
    """
    descs: list[str] = []

    # ① Consume <step> tags first
    from_step = 0
    for _ in range(tool_count):
        if state._pending_step_descriptions:
            descs.append(state._pending_step_descriptions.pop(0))
            from_step += 1

    # ② For remaining slots, extract from accumulated think text
    remaining = tool_count - len(descs)
    from_think = 0
    if remaining > 0:
        for i in range(remaining):
            desc = _extract_tool_desc_from_text(state, i, remaining)
            if desc:
                descs.append(desc)
                from_think += 1

    # ③ Log what we got
    think_text = "".join(state._accumulated_think)
    logger.info("[round=%d] Descriptions built: %d tools, %d from <step>, %d from think "
               "(think_text=%d chars: %r)",
               state.round_number, tool_count, from_step, from_think,
               len(think_text), think_text[:120] if think_text else "(empty)")
    state._accumulated_think = []

    return descs


def _extract_tool_desc_from_text(state: _EventState,
                                 tool_index: int, total_tools: int) -> str:
    """Extract a tool description from the accumulated think text.

    For the FIRST tool of a round, take the last sentence of the think text
    (which usually states the immediate next action).
    For subsequent tools, try to find multi-step indicators.
    """
    full_text = "".join(state._accumulated_think).strip()
    if not full_text:
        return ""

    # Split into sentences by Chinese/English punctuation
    sentences = _split_sentences(full_text)
    if not sentences:
        return ""

    if total_tools == 1:
        # Single tool: use the last substantive sentence
        for s in reversed(sentences):
            if len(s) > 6:
                return s[:100]

    # Multiple tools: try to find action items
    # Look for numbered items, "需要/首先/然后/其次/接着/最后" patterns
    action_keywords = ["需要", "首先", "然后", "其次", "接着", "最后",
                       "检查", "执行", "查看", "采集", "读取", "排查",
                       "运行", "确认", "验证", "获取", "诊断"]
    action_sentences = [s for s in sentences
                        if any(kw in s for kw in action_keywords) and len(s) > 4]

    if len(action_sentences) >= total_tools:
        return action_sentences[tool_index][:100]

    # As fallback for multi-tool with index > 0, use the sentence before last
    if tool_index > 0 and len(sentences) >= 2:
        return sentences[-2][:100]

    # Use last long sentence
    for s in reversed(sentences):
        if len(s) > 6:
            return s[:100]

    return sentences[-1][:100] if sentences else ""


def _split_sentences(text: str) -> list[str]:
    """Split text into cleaned sentences."""
    import re
    parts = re.split(r'[。；\n]', text)
    return [s.strip() for s in parts if s.strip()]


# ════════════════ Tag parsers ════════════════

def _parse_think_tags(text: str, state: _EventState) -> tuple[str, str]:
    """Parse <think>...</think> or <Thinking>...</Thinking> tags."""
    state._think_tag_buf += text
    open_tags = ["<think>", "<Thinking>", "<thinking>"]
    close_tags = ["</think>", "</Thinking>", "</thinking>"]

    think_parts: list[str] = []
    answer_parts: list[str] = []
    buf = state._think_tag_buf
    found_any_tag = False

    while buf:
        best_start, best_open, best_close = -1, "", ""
        for ot, ct in zip(open_tags, close_tags):
            idx = buf.find(ot)
            if idx != -1 and (best_start == -1 or idx < best_start):
                best_start, best_open, best_close = idx, ot, ct

        if best_start == -1:
            longest_partial = max(
                (_partial_tag_suffix(buf, ot) for ot in open_tags), default=0)
            if longest_partial:
                prefix = buf[:-longest_partial]
                if prefix:
                    answer_parts.append(prefix)
                state._think_tag_buf = buf[-longest_partial:]
            else:
                if not found_any_tag:
                    state._think_tag_buf = ""
                    return "", ""
                answer_parts.append(buf)
                state._think_tag_buf = ""
            break

        found_any_tag = True
        if best_start > 0:
            answer_parts.append(buf[:best_start])

        end_idx = buf.find(best_close, best_start + len(best_open))
        if end_idx == -1:
            state._think_tag_buf = buf[best_start:]
            break

        content = buf[best_start + len(best_open):end_idx]
        think_parts.append(content)
        buf = buf[end_idx + len(best_close):]
        state._think_tag_buf = buf

    if not state._think_tag_buf:
        state._think_tag_buf = ""

    return "".join(think_parts), "".join(answer_parts)


def _parse_step_tags(text: str, state: _EventState) -> str:
    """Parse <step>...</step> tags, accumulate descriptions, return clean text.

    Called on raw text BEFORE think/answer split, so <step> tags are captured
    regardless of whether the LLM places them inside or outside <think> blocks.
    """
    if not text:
        return text
    state._step_tag_buf += text
    buf = state._step_tag_buf
    open_tag = "<step>"
    close_tag = "</step>"
    clean_parts: list[str] = []
    found_count = 0

    while buf:
        start_idx = buf.find(open_tag)
        if start_idx == -1:
            partial = _partial_tag_suffix(buf, open_tag)
            if partial:
                clean_parts.append(buf[:-partial])
                state._step_tag_buf = buf[-partial:]
            else:
                clean_parts.append(buf)
                state._step_tag_buf = ""
            break

        if start_idx > 0:
            clean_parts.append(buf[:start_idx])

        end_idx = buf.find(close_tag, start_idx + len(open_tag))
        if end_idx == -1:
            state._step_tag_buf = buf[start_idx:]
            break

        content = buf[start_idx + len(open_tag):end_idx].strip()
        if content:
            state._pending_step_descriptions.append(content)
            found_count += 1
        buf = buf[end_idx + len(close_tag):]
        state._step_tag_buf = buf

    if found_count:
        logger.info("[round=%d] Parsed %d <step> tag(s), total pending=%d",
                     state.round_number, found_count,
                     len(state._pending_step_descriptions))

    if not state._step_tag_buf:
        state._step_tag_buf = ""

    return "".join(clean_parts)


def _partial_tag_suffix(buffer: str, token: str) -> int:
    for length in range(min(len(buffer), len(token) - 1), 0, -1):
        if token.startswith(buffer[-length:]):
            return length
    return 0


def _serialize_sr(sr: Any) -> dict[str, Any]:
    """Serialize a Pydantic structured_response to a plain dict."""
    if hasattr(sr, "model_dump"):
        return sr.model_dump()
    elif hasattr(sr, "dict"):
        return sr.dict()
    elif isinstance(sr, dict):
        return sr
    return {"value": str(sr)}


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

    # Emit any remaining buffered text as the final answer (diagnosis report)
    if state._think_tag_buf.strip():
        events.append(AgentEvent("text_delta", {
            "text": state._think_tag_buf,
            "path": ["coordinator"], "round": state.round_number,
            "phase": "answering",
        }))
    else:
        # If no buffered text but LLM was working, emit accumulated think as report
        report = "".join(state._accumulated_think).strip()
        if report:
            events.append(AgentEvent("text_delta", {
                "text": f"\n## 诊断报告\n\n{report[-2000:]}",
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
