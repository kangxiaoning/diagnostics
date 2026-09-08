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

    def _get_request_payload(self, input_, *, stop=None, **kwargs):
        payload = super()._get_request_payload(input_, stop=stop, **kwargs)
        mct = payload.get("max_completion_tokens")
        if mct is not None and "max_tokens" not in payload:
            payload["max_tokens"] = mct
        if _THINKING_MODE in ("enabled", "disabled"):
            # Vendor-specific fields travel in extra_body: langchain
            # splats the payload into the OpenAI client as keyword
            # arguments, so unknown top-level keys raise TypeError.
            extra_body = payload.get("extra_body") or {}
            extra_body["thinking"] = {"type": _THINKING_MODE}
            if _THINKING_MODE == "enabled" and _REASONING_EFFORT:
                extra_body["reasoning_effort"] = _REASONING_EFFORT
            payload["extra_body"] = extra_body
        return payload
