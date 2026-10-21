"""Closed failure contracts for tool results (design document §8 G19/G25).

Production script-dispatch tools (the OSP family) return the execution log
line rather than a business result:

    {"log": "... osp_script-----: {'succeed': [], 'failed': [{'vm_name':
     'CQX-L0150660', 'status': 'status: done, code: OK, err: run command err:
     exit status 2, stderr: ping: www.baidu.com: Name or service not known'}]}",
     "stream": "stderr", "time": "..."}

Two properties make prose heuristics unusable for this shape:

1. The verdict is STRUCTURAL — success list empty AND failure list non-empty —
   and it is length-independent.  The same failure was measured at 395
   characters (inside the 500-character guard that classifies "long content"
   as diagnostic data) and would flip to "success" as soon as the payload grew
   past that guard.  A verdict must not depend on how long a vm_name is.
2. The failure is DETERMINISTIC: a DNS name that does not resolve never
   resolves, so granting the usual one-shot real-retry budget only burns a
   model turn per attempt and feeds the repeat-call loop.

Both verdicts live here so the dedup middleware (G25 caching) and the
zero-yield watchdog (G19) cannot drift apart.
"""

from __future__ import annotations

import re

# ── Structural failure contracts ───────────────────────────────────────────
# "STATUS: failed" is the contract the production tool layer is expected to
# emit after normalization; the OSP shapes below cover the pre-normalization
# payloads still observed in the field.
_STATUS_FAILED_RE = re.compile(
    r"^\s*STATUS\s*:\s*failed\b", re.IGNORECASE | re.MULTILINE)
_SUCCEED_EMPTY_RE = re.compile(r"['\"]succeed['\"]\s*:\s*\[\s*\]")
_FAILED_NONEMPTY_RE = re.compile(r"['\"]failed['\"]\s*:\s*\[\s*[{\[\"']")
_EXIT_STATUS_RE = re.compile(r"exit\s+status\s+\d+", re.IGNORECASE)
_STDERR_MARK_RE = re.compile(r"stderr\s*:", re.IGNORECASE)

# ── Deterministic failures (retry can never change the outcome) ────────────
# A retry budget assumes a transient fault.  These markers state a permanent
# condition: the name does not resolve, the binary/path does not exist, the
# caller lacks permission.  Retrying them is a pure waste of a model turn.
_DETERMINISTIC_RE = re.compile(
    r"name or service not known|"
    r"\bnxdomain\b|"
    r"command not found|"
    r"no such file or directory|"
    r"permission denied|"
    r"invalid (?:destination|hostname|target)",
    re.IGNORECASE,
)

# Markers read back into receipts, in preference order: a structured failure
# line first, then the raw stderr text, then the generic "err:" tail.
_REASON_MARKERS = ("FAILED:", "REASON:", "stderr:", "err:")


def is_structural_failure(content: str) -> bool:
    """True when the payload STATES a failure through a closed structure.

    Deliberately blind to length and to open-ended keywords: this predicate
    is the "contract" branch of the failure classification and must return
    the same verdict for the same failure regardless of payload size.
    """
    if not content:
        return False
    if _STATUS_FAILED_RE.search(content):
        return True
    if (_SUCCEED_EMPTY_RE.search(content)
            and _FAILED_NONEMPTY_RE.search(content)):
        return True
    # Weaker, still mechanical: a command exited non-zero and produced stderr.
    if _EXIT_STATUS_RE.search(content) and _STDERR_MARK_RE.search(content):
        return True
    return False


def is_deterministic_failure(content: str) -> bool:
    """True when retrying the identical call cannot possibly succeed."""
    return bool(content) and bool(_DETERMINISTIC_RE.search(content))


def failure_reason(content: str, limit: int = 200) -> str:
    """One-line failure reason for a ToolMessage receipt.

    Never returns the whole payload: duplicate long text is itself a
    repetition amplifier (design document §11, v3.37.4/v3.37.5).
    """
    if not content:
        return ""
    lowered = content.lower()
    for marker in _REASON_MARKERS:
        idx = lowered.find(marker.lower())
        if idx < 0:
            continue
        tail = content[idx + len(marker):]
        tail = tail.strip(" \t\r\n'\"\\")
        if tail:
            return tail[:limit]
    first = next(
        (line.strip() for line in content.splitlines() if line.strip()), "")
    return first[:limit]
