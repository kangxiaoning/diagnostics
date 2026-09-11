"""ChatOpenAI subclass that emits ollama-compatible max_tokens.

Shared by the main agent model (factory.py) and the mock Argus
classifier (tools/mock/argus.py).
"""

from __future__ import annotations

import os

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
