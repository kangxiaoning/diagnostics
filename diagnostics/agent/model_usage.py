"""Backend-agnostic extraction of model-response stop reasons and usage.

Two facts motivate a shared helper instead of per-middleware parsing
(design document §8 G28, 2026-09-08):

- ``finish_reason == "length"`` means the response was cut off at the
  output cap, so any structured return it should have produced is
  missing.  Left unhandled, the caller receives a fallback text and
  cannot tell "nothing was found" from "nothing was said".
- Usage travels in different shapes across backends: langchain's
  normalized ``usage_metadata``, OpenAI-compatible
  ``response_metadata["token_usage"]``, or a raw ``usage`` object left in
  the response metadata.  Reporting zeros for all of them is what made
  output-budget exhaustion invisible in this project.

Nothing here is model- or vendor-specific: unknown shapes degrade to
zeros and callers keep working (a missing counter must never become a
failure source).
"""

from __future__ import annotations

from typing import Any


def messages_of(response: Any) -> list[Any]:
    """Flatten a ModelResponse into its result message list."""
    result = getattr(response, "result", None)
    if result is None:
        return []
    return list(result) if isinstance(result, list) else [result]


def finish_reason_of(message: Any) -> str:
    """Stop reason of *message* ("" when the backend reports none)."""
    meta = getattr(message, "response_metadata", None) or {}
    reason = meta.get("finish_reason")
    return reason if isinstance(reason, str) else ""


def is_truncated(message: Any) -> bool:
    return finish_reason_of(message) == "length"


def first_truncated(response: Any) -> Any | None:
    """First message of *response* that was cut off at the output cap."""
    for message in messages_of(response):
        if is_truncated(message):
            return message
    return None


def reasoning_chars(message: Any) -> int:
    """Character length of the reasoning stream carried by *message*.

    v3.37.3 (observed 2026-09-11, scenario 34): a truncated turn reported
    ``out=16384, reasoning=?`` — the provider exposed NO reasoning token
    counter, yet the captured response body carried a 71419-character
    ``message.reasoning`` field (0 content, 0 tool_calls).  Token counters
    are optional across OpenAI-compatible endpoints, so the observable
    fact must be the stream itself: this returns its character length,
    which lets the G28 log distinguish "the answer was cut off" from
    "the thinking stream exhausted the shared budget".
    """
    for attr in ("reasoning", "reasoning_content"):
        v = getattr(message, attr, None)
        if isinstance(v, str) and v.strip():
            return len(v)
    extra = getattr(message, "additional_kwargs", None)
    if isinstance(extra, dict):
        for key in ("reasoning", "reasoning_content", "thinking"):
            v = extra.get(key)
            if isinstance(v, str) and v.strip():
                return len(v)
            if isinstance(v, dict):
                inner = v.get("content") or v.get("text")
                if isinstance(inner, str) and inner.strip():
                    return len(inner)
    return 0


def extract_usage(message: Any) -> tuple[int, int, int]:
    """Best-effort ``(input, output, reasoning)`` token counts.

    Also reads ``model_extra`` (pydantic models keep unknown provider
    fields there), which is where some OpenAI-compatible gateways leave
    the ``usage`` object.
    """
    tokens_in = 0
    tokens_out = 0
    tokens_reason = 0
    try:
        usage = getattr(message, "usage_metadata", None) or {}
        tokens_in += int(usage.get("input_tokens") or 0)
        tokens_out += int(usage.get("output_tokens") or 0)
        details = usage.get("output_token_details") or {}
        tokens_reason += int(details.get("reasoning_tokens") or 0)

        meta = getattr(message, "response_metadata", None) or {}
        for source in (meta.get("token_usage"), meta.get("usage")):
            if not isinstance(source, dict):
                continue
            tokens_in += int(source.get("prompt_tokens") or 0)
            tokens_out += int(source.get("completion_tokens") or 0)
            details = source.get("completion_tokens_details") or {}
            tokens_reason += int(details.get("reasoning_tokens") or 0)
        extra = getattr(meta, "model_extra", None) or {}
        usage = extra.get("usage") if isinstance(extra, dict) else None
        if isinstance(usage, dict):
            tokens_in += int(usage.get("prompt_tokens") or 0)
            tokens_out += int(usage.get("completion_tokens") or 0)
    except Exception:  # noqa: BLE001 — accounting must never break a call
        pass
    return tokens_in, tokens_out, tokens_reason
