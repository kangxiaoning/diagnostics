"""ChatOpenAI subclass that emits ollama-compatible max_tokens.

Shared by the main agent model (factory.py) and the mock Argus
classifier (tools/mock/argus.py).
"""

from __future__ import annotations

import os
from contextvars import ContextVar

from langchain_openai import ChatOpenAI

# ── Optional chain-of-thought switch (design document §12) ──────────
# Some hosted OpenAI-compatible endpoints emit a reasoning stream by
# default (e.g. the DeepSeek online API).  Two consequences make it
# hostile to this tool-calling agent loop: (1) when `tools` is present
# the endpoint requires every previous turn's reasoning content to be
# echoed back and answers 400 otherwise; (2) the reasoning stream shares
# the output budget, so a long CoT can truncate the final answer.
# Opt-in by configuration: the fields are only emitted when the operator
# sets DIAGNOSTICS_THINKING, leaving backends that do not know them
# (ollama, LM Studio) byte-identical to before.
_THINKING_MODE = os.getenv("DIAGNOSTICS_THINKING", "").strip().lower()
_REASONING_EFFORT = os.getenv("DIAGNOSTICS_REASONING_EFFORT", "").strip().lower()

# ── Optional model residency (design document §12) ──────────────────
# A cold runner pays the full model load before the first token: measured
# 29s for a 35B MoE on this host (2026-09-09 session 59e7da10 — 3.5% of
# round-1 wall time, every first request of a working day).  Backends
# that expose a keep-alive/TTL knob take it in extra_body
# (e.g. keep_alive="-1" pins the model).  Off by default: the field is
# unknown to plain OpenAI-compatible servers, so it is only emitted when
# the operator opts in — same discipline as the thinking switch above.
_KEEP_ALIVE = os.getenv("DIAGNOSTICS_KEEP_ALIVE", "").strip()

# ── Reasoning-loop guard (v3.45.11) ──────────────────────────────────
# Local thinking models get stuck repeating a long block inside their
# reasoning.  Measured four times on 2026-10-22/23: 22,385 / 25,990 / 30,041
# characters of reasoning that re-emitted the same paragraph, always ending in
# a timeout with zero content and zero tool calls (ollama logged
# ``context canceled``).  Every occurrence was in a decision phase
# (hypothesize / evaluate) — the phases that compare candidates rather than
# execute tools.
#
# This is the same failure the industry documents for local/MoE thinking
# models ("runaway reasoning loops where the model repeats itself inside a
# reasoning block", Reasoning Guard; dsh-loop-detector's anchor-block check),
# and the documented remedy is a deterministic streaming check plus a hard
# stop — not another prompt request.
_LOOP_MIN_CHARS = int(os.getenv("DIAGNOSTICS_LOOP_MIN_CHARS", "8000"))
"""Reasoning length before loop detection engages (normal turns stay < 8k)."""

_LOOP_ANCHOR_CHARS = int(os.getenv("DIAGNOSTICS_LOOP_ANCHOR_CHARS", "256"))
"""Length of the tail block that must reappear earlier in the same response."""

_LOOP_SCAN_LIMIT = int(os.getenv("DIAGNOSTICS_LOOP_SCAN_LIMIT", "24000"))
"""Rolling buffer cap; older text is dropped so the scan stays O(n)."""

_REASONING_BUF: ContextVar[str] = ContextVar("reasoning_stream_buf", default="")
"""Per-request reasoning buffer (contextvars keeps concurrent calls apart)."""


class ReasoningLoopError(RuntimeError):
    """A single response repeated a long reasoning block (v3.45.11).

    Raised from the streaming hook so it propagates out of the model call and
    lands in the G7' retry path, whose second attempt already disables
    thinking (v3.45.7) — the combination turns an infinite loop into one
    bounded retry instead of a 480 s timeout.
    """


def _guard_reasoning_stream(delta: str) -> None:
    """Accumulate reasoning deltas and raise when a long block repeats."""
    buf = _REASONING_BUF.get() + delta
    if len(buf) > _LOOP_SCAN_LIMIT:
        buf = buf[-_LOOP_SCAN_LIMIT:]
    _REASONING_BUF.set(buf)
    if len(buf) < _LOOP_MIN_CHARS:
        return
    tail = buf[-_LOOP_ANCHOR_CHARS:]
    if buf[:-_LOOP_ANCHOR_CHARS].find(tail) != -1:
        raise ReasoningLoopError(
            f"reasoning repeated a {_LOOP_ANCHOR_CHARS}-char block "
            f"after {len(buf)} chars — stopping the response instead of "
            f"letting it run into the timeout")


class OllamaChatOpenAI(ChatOpenAI):
    """Emit legacy top-level ``max_tokens`` alongside ``max_completion_tokens``.

    ollama's OpenAI-compatible endpoint silently ignores
    ``max_completion_tokens`` and only honors top-level ``max_tokens``
    (empirically verified against ollama 0.32.1 on 2026-07-19:
    ``max_tokens=5`` truncates at exactly 5 tokens / finish=length,
    while ``max_completion_tokens=5`` and ``extra_body.max_tokens=5``
    are both ignored).  The same day a runaway generation decoded
    10401 tokens despite the 4096 ``max_completion_tokens`` cap,
    burning the full 480s call timeout.  The base payload builder
    unconditionally renames max_tokens → max_completion_tokens, so
    re-add the legacy field after the fact.
    """

    # Per-instance reasoning-effort override (v3.37.3).  The wrap-up path
    # (G28 recovery after an output-truncated turn) switches to a
    # "none"-effort variant of this model so the reasoning stream cannot
    # exhaust the output budget a second time.  Instance-scoped on purpose:
    # the same model object serves every other turn untouched, and no
    # global switch is flipped for the whole session.
    reasoning_effort_override: str | None = None

    def _get_request_payload(self, input_, *, stop=None, **kwargs):
        # Each request starts with an empty reasoning buffer: the loop guard
        # must judge one response at a time, never carry state across turns.
        _REASONING_BUF.set("")
        payload = super()._get_request_payload(input_, stop=stop, **kwargs)
        mct = payload.get("max_completion_tokens")
        if mct is not None and "max_tokens" not in payload:
            payload["max_tokens"] = mct
        # Reasoning-effort (v3.37.3): emitted whenever an effort is in force
        # — module-level default (operator opt-in) or the per-instance
        # override used by the wrap-up path.  Empirically verified against
        # ollama (qwen3.6:35b-mlx) on 2026-09-11: ``reasoning_effort="none"``
        # yields 0 reasoning tokens, whereas ``thinking={"type":"disabled"}``
        # is accepted (200) but IGNORED — it keeps thinking.
        effort = self.reasoning_effort_override or _REASONING_EFFORT
        if effort:
            extra_body = payload.get("extra_body") or {}
            extra_body["reasoning_effort"] = effort
            payload["extra_body"] = extra_body
        elif _THINKING_MODE in ("enabled", "disabled"):
            # Vendor-specific fields travel in extra_body: langchain
            # splats the payload into the OpenAI client as keyword
            # arguments, so unknown top-level keys raise TypeError.
            extra_body = payload.get("extra_body") or {}
            extra_body["thinking"] = {"type": _THINKING_MODE}
            payload["extra_body"] = extra_body
        if _KEEP_ALIVE:
            extra_body = payload.get("extra_body") or {}
            extra_body["keep_alive"] = _KEEP_ALIVE
            payload["extra_body"] = extra_body
        return payload

    # ── Preserve provider reasoning (v3.37.7, R2 observability) ──────
    # LangChain's ChatOpenAI targets the official OpenAI schema and, by its
    # own documentation, neither extracts nor preserves non-standard
    # response fields such as ``message.reasoning`` / ``reasoning_content``
    # (langchain issue #34706, #36413).  The provider we run against
    # (ollama + qwen3) DOES return them: the captured body of a truncated
    # turn carried 71419 characters of ``message.reasoning`` while the
    # middleware logged ``reasoning_chars=0`` — i.e. the G28 log could not
    # tell "the answer was cut off" from "the thinking stream ate the whole
    # budget", which is exactly the distinction the truncation fix needs.
    #
    # Implemented as a post-processing hook on the RAW response: it never
    # changes what the model sees, only what we can observe.  EXTRACTION
    # failures are swallowed (a missing/oddly-shaped field must not become
    # a call failure source); the framework's own parse errors still
    # propagate untouched, since those are real errors.
    def _create_chat_result(self, response, generation_info=None):
        result = super()._create_chat_result(response, generation_info)
        try:
            if isinstance(response, dict):
                data = response
            else:
                try:
                    data = response.model_dump()
                except Exception:
                    data = {}
            for gen, choice in zip(result.generations, (data or {}).get("choices") or []):
                msg = getattr(gen, "message", None)
                if msg is None:
                    continue
                raw_msg = (choice or {}).get("message") or {}
                reason = raw_msg.get("reasoning") or raw_msg.get("reasoning_content")
                if isinstance(reason, str) and reason.strip():
                    msg.additional_kwargs["reasoning"] = reason
                    msg.additional_kwargs["reasoning_chars"] = len(reason)
        except Exception:
            pass
        return result

    # ── Preserve provider reasoning on the STREAMING path (v3.44.0) ──
    # The hook above only covers `invoke`: it post-processes a complete
    # response.  The agent runs on `astream`, where every chunk is built by
    # `_convert_chunk_to_generation_chunk`, and that helper drops
    # provider-specific delta fields — the class docstring states the scope
    # ("non-standard response fields ... are not extracted") and issues
    # #34706 / #36413 record the same gap for third-party endpoints.
    # Measured on this backend (2026-10-22): the wire chunk carries
    # `{"delta": {"reasoning": "..."}}`, so without this hook the thinking
    # stream never reaches the frontend and the think section renders the
    # answer text instead (indistinguishable from a repeated turn).
    #
    # Written into `additional_kwargs` only.  `_convert_message_to_dict`
    # does not forward non-standard keys, so the model never sees its own
    # reasoning echoed back — this is an observability hook, not a
    # behaviour change; extraction failures stay swallowed for the same
    # reason as the non-streaming hook.
    def _convert_chunk_to_generation_chunk(
        self,
        chunk,
        default_chunk_class,
        base_generation_info,
    ):
        result = super()._convert_chunk_to_generation_chunk(
            chunk, default_chunk_class, base_generation_info
        )
        if result is None:
            return None
        try:
            for choice in (chunk or {}).get("choices") or []:
                delta = (choice or {}).get("delta") or {}
                reason = delta.get("reasoning_content") or delta.get("reasoning")
                if isinstance(reason, str) and reason:
                    result.message.additional_kwargs["reasoning_content"] = reason
                    # v3.45.11: stop a looping response instead of letting it
                    # consume the whole call budget (see the guard's docstring).
                    _guard_reasoning_stream(reason)
                    break
        except ReasoningLoopError:
            # Must propagate: the generic swallow below would turn the guard
            # into a no-op.
            raise
        except Exception:
            pass
        return result
