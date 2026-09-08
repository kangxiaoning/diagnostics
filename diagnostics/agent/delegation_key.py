"""Stable, human-readable per-delegation identity — one derivation.

Every subagent middleware that keeps per-delegation state must agree on
ONE key: file-tool governance (A1-A4), the zero-yield watchdog (G19),
dedup repeat-hit escalation (G24), the low-information-gain gate (G26)
and the progress recorder / guidance injector.  The stores are shared
(the ExpertSessionLedger is written by the guidance middleware and read
by G26; dedup hit counters are written by dedup and read by guidance),
so divergent derivations would silently split one delegation's state in
two — the bug class this module exists to prevent.

Two properties the previous derivation (``f"del:{hash(text[:800])}"``,
duplicated in five places) lacked:

- STABLE: Python's built-in ``hash()`` for str is salted per process
  (PYTHONHASHSEED randomisation), so the same delegation was logged
  under a different — sometimes negative — number after every restart,
  and logs from two runs could never be correlated.  A SHA-1 digest is
  identical across processes and runs.
- READABLE: a bare digest cannot tell an operator which task or which
  expert it belongs to, so the key carries a short slug of the
  delegation instruction.  Observability, not semantics: the digest
  still decides identity, the slug only labels it.

Format: ``del:<digest12>:<slug>`` — slug = first ~24 chars of the
delegation instruction, whitespace-collapsed.  Fallback (no
HumanMessage in state): ``del:unknown``.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any

# Digest length: 12 hex chars = 48 bits — collision-free at the scale of
# a single diagnosis (tens of delegations), short enough to stay scannable.
_DIGEST_LEN = 12
# Label length: enough to recognise the task at a glance, bounded so a
# long instruction cannot bloat every log line that carries the key.
_SLUG_LEN = 24
# Text cap for the digest input: the instruction prefix is already
# uniquely identifying; hashing more costs CPU on every tool call.
_TEXT_CAP = 800

_UNKNOWN_KEY = "del:unknown"

_WS_RE = re.compile(r"\s+")


def _first_human_text(state: Any) -> str:
    """Text of the delegation's initial HumanMessage (task description).

    Each task() invocation seeds the subagent state with exactly one
    HumanMessage holding the (unique) delegation instruction, so it
    isolates concurrent and stale delegations deterministically.
    """
    try:
        messages = state.get("messages") if isinstance(state, dict) else None
    except Exception:
        return ""
    for msg in messages or []:
        if getattr(msg, "type", None) != "human":
            continue
        text = getattr(msg, "content", "")
        return text if isinstance(text, str) else str(text)
    return ""


def delegation_key(state: Any) -> str:
    """Per-delegation key shared by every subagent middleware."""
    text = _first_human_text(state)
    if not text:
        return _UNKNOWN_KEY
    digest = hashlib.sha1(
        text[:_TEXT_CAP].encode("utf-8", "replace")
    ).hexdigest()[:_DIGEST_LEN]
    slug = _WS_RE.sub(" ", text.strip())[:_SLUG_LEN]
    return f"del:{digest}:{slug}" if slug else f"del:{digest}"


def delegation_key_from_request(request: Any) -> str:
    """``delegation_key`` for middleware hooks, which see a request."""
    return delegation_key(getattr(request, "state", None) or {})
