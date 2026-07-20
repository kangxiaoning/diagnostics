"""ChatOpenAI subclass that emits ollama-compatible max_tokens.

Shared by the main agent model (factory.py) and the mock Argus
classifier (tools/mock/argus.py).
"""

from __future__ import annotations

from langchain_openai import ChatOpenAI


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
        return payload
