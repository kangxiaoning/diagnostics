from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class AgentEvent:
    name: str
    payload: dict[str, Any]


async def stream_agent_events(
    agent: Any,
    messages: list[Any],
    cancel_event: asyncio.Event,
    session_id: str,
) -> AsyncIterator[AgentEvent]:
    async for raw_chunk in agent.astream(
        {"messages": messages},
        stream_mode=["messages", "updates"],
        subgraphs=True,
    ):
        if cancel_event.is_set():
            yield AgentEvent("cancelled", {"session_id": session_id})
            return

        for event_name, payload in format_stream_chunk(raw_chunk):
            yield AgentEvent(event_name, payload)


def format_stream_chunk(raw_chunk: Any) -> list[tuple[str, dict[str, Any]]]:
    namespace: list[str] = []
    mode = "data"
    data = raw_chunk

    if isinstance(raw_chunk, tuple):
        if len(raw_chunk) == 3:
            namespace, mode, data = list(raw_chunk[0]), raw_chunk[1], raw_chunk[2]
        elif len(raw_chunk) == 2 and isinstance(raw_chunk[0], str):
            mode, data = raw_chunk
        elif len(raw_chunk) == 2:
            namespace = list(raw_chunk[0]) if isinstance(raw_chunk[0], (tuple, list)) else [str(raw_chunk[0])]
            if isinstance(raw_chunk[1], tuple) and len(raw_chunk[1]) == 2:
                mode, data = raw_chunk[1]
            else:
                data = raw_chunk[1]

    if mode == "messages":
        message = data[0] if isinstance(data, tuple) and data else data
        text = message_text(message)
        tool_calls = getattr(message, "tool_calls", None)
        result: list[tuple[str, dict[str, Any]]] = []
        if text:
            result.append(("token", {"text": text, "namespace": namespace}))
        if tool_calls:
            result.append(("tool_call", {"tool_calls": jsonable(tool_calls), "namespace": namespace}))
        return result

    if mode == "updates":
        return [("update", {"data": summarize_update(data), "namespace": namespace})]

    return [("debug", {"mode": mode, "data": jsonable(data), "namespace": namespace})]


def message_text(message: Any) -> str:
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


def summarize_update(data: Any) -> Any:
    if not isinstance(data, dict):
        return jsonable(data)

    summary: dict[str, Any] = {}
    for node_name, value in data.items():
        if isinstance(value, dict) and "messages" in value:
            messages = value["messages"]
            last = messages[-1] if messages else None
            summary[node_name] = {
                "message_type": last.__class__.__name__ if last is not None else None,
                "content": message_text(last)[:1200] if last is not None else "",
                "tool_calls": jsonable(getattr(last, "tool_calls", None)),
            }
        else:
            summary[node_name] = jsonable(value)
    return summary


def jsonable(value: Any) -> Any:
    try:
        json.dumps(value)
        return value
    except TypeError:
        if isinstance(value, dict):
            return {str(key): jsonable(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [jsonable(item) for item in value]
        return repr(value)
