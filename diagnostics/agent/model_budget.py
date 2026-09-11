"""Output-budget-derived thresholds (v3.39.0).

Deployment target (2026-09-11): ONE output cap shared by both supported
backends — ``qwen3.6-35b-a3b`` (262K context) and ``deepseek-v4-flash``
(1M context).  Those two differ ~4x in context window, so **nothing here may
derive from the context size**; only the output cap is common by
construction, and every threshold below is a fraction of it.

Port-as-is: set ``DIAGNOSTICS_MAX_OUTPUT_TOKENS`` (default 32768) and run.

Why the output cap dominates: on BOTH backends ``max_tokens`` *includes* the
chain of thought, so a runaway reasoning stream can consume the whole budget
and leave nothing for the conclusion (observed 2026-09-11, scenario 34:
71419 reasoning characters, 0 content, 0 tool_calls).

The floor keeps small/legacy setups — e.g. the 16384-token mock runs —
behaving exactly as before this module existed.
"""
from __future__ import annotations

import os

# Single knob.  Deployment sets 32768 (32K); mock runs keep 16384 via env.
MAX_OUTPUT_TOKENS = int(os.getenv("DIAGNOSTICS_MAX_OUTPUT_TOKENS", "32768"))

# Mixed zh/en diagnostic output tokenizes at roughly this many characters per
# token (zh ~1.5, en ~4).  Used only to convert token budgets into the
# character counts the middleware actually observes.
CHARS_PER_TOKEN = float(os.getenv("DIAGNOSTICS_CHARS_PER_TOKEN", "2.25"))

_REASONING_PRESSURE_FLOOR = 8000


def reasoning_pressure_chars() -> int:
    """Character threshold at which a turn's reasoning is "too long".

    Half the output budget (32768 tokens -> 36864 chars).  Half is
    deliberate: the remaining half must still fit the conclusion tool call
    and its evidence payload.  The floor preserves legacy behaviour for
    small caps.
    """
    return max(
        _REASONING_PRESSURE_FLOOR,
        int(MAX_OUTPUT_TOKENS * 0.5 * CHARS_PER_TOKEN),
    )


def truncation_risk_chars() -> int:
    """Character threshold above which a turn is likely to be cut off.

    The output cap is a hard ceiling shared by reasoning and content, so a
    single turn approaching it is the leading indicator of a ``length``
    finish (the reactive recovery path stays in place regardless).
    """
    return int(MAX_OUTPUT_TOKENS * 0.9 * CHARS_PER_TOKEN)
