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

Identity contract (v3.41.1)
---------------------------
The state's first HumanMessage is NOT the raw instruction: the task
wrapper prepends the system context layers (session baseline, entity
hints, pre-resolved parameters, budget block — design document
§5.2/§7.3) and the Coordinator's instruction follows
``_INSTRUCTION_MARKER``.  That head is identical for every delegation of
the same expert inside one session, so the former ``text[:800]`` window
covered system context only: the digest identified the SESSION CONTEXT,
not the delegation, and same-family delegations silently shared one
guard-state bucket (G19/G19-ext counters, G24 hit counts, G11 read
budget, G26 fingerprints, guidance ledger, interception receipts).  The
digest therefore covers the WHOLE message now; only the label is read
after the marker, which is also what "委派任务前 24 字 slug" in the
design document promises.

Residual: two delegations whose wrapped message is byte-identical (the
same instruction re-issued to the same expert within one session, in
sequence or in parallel) still share one key — a content-derived
identity cannot separate them; only an explicit per-invocation
identifier could.

Format: ``del:<digest12>:<slug>`` — slug = first ~24 chars of the
delegation instruction (after the marker when the message is wrapped),
whitespace-collapsed.  Fallback (no HumanMessage in state):
``del:unknown``.
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
# The task wrapper separates the injected system context from the
# Coordinator's own instruction with this marker; the label is read after
# it so the slug names the TASK instead of the session baseline.
_INSTRUCTION_MARKER = "[Coordinator 委派指令]"

_UNKNOWN_KEY = "del:unknown"

_WS_RE = re.compile(r"\s+")


def _first_human_text(state: Any) -> str:
    """Text of the delegation's initial HumanMessage.

    Each task() invocation seeds the subagent state with exactly one
    HumanMessage.  That message is the system context wrap FOLLOWED by the
    instruction (see the identity contract in the module docstring), so it
    is not a unique per-delegation string on its own: identity relies on
    the whole message, and two byte-identical delegations remain
    indistinguishable (documented residual).
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


def _instruction_text(text: str) -> str:
    """Label source: the Coordinator instruction inside a wrapped message.

    Falls back to the whole text for unwrapped delegations (direct
    invocation, tests), so the label degrades to the pre-wrap behaviour
    instead of disappearing.
    """
    idx = text.find(_INSTRUCTION_MARKER)
    if idx < 0:
        return text
    return text[idx + len(_INSTRUCTION_MARKER):]


def delegation_key(state: Any) -> str:
    """Per-delegation key shared by every subagent middleware.

    Identity = digest of the ENTIRE first HumanMessage (see the identity
    contract in the module docstring: its head is session context shared
    by all delegations of one expert, so a truncated prefix would key the
    guards by context instead of by delegation).  Labels come from the
    instruction when the message carries the wrapper marker.
    """
    text = _first_human_text(state)
    if not text:
        return _UNKNOWN_KEY
    digest = hashlib.sha1(
        text.encode("utf-8", "replace")
    ).hexdigest()[:_DIGEST_LEN]
    slug = _WS_RE.sub(" ", _instruction_text(text).strip())[:_SLUG_LEN]
    return f"del:{digest}:{slug}" if slug else f"del:{digest}"


def delegation_key_from_request(request: Any) -> str:
    """``delegation_key`` for middleware hooks, which see a request."""
    return delegation_key(getattr(request, "state", None) or {})


def delegation_text_from_request(request: Any) -> str:
    """Delegation instruction text for middleware hooks.

    Public accessor for the same first-HumanMessage derivation the key
    uses: callers that need to READ the instruction (e.g. the evidence
    ledger parsing the target hypothesis out of it) must not re-derive
    it, or the two views can drift.
    """
    return _first_human_text(getattr(request, "state", None) or {})
