"""One-full-explanation receipts for interception channels (v3.37.5).

Problem this solves
-------------------
Several guards answer EVERY blocked call with a long, near-identical
message: the G19/G19-ext hard blocks (``⛔ 系统强制收尾…``), the G26
low-gain hard block, and the five file-governance rejections.  Inside one
delegation the model can trigger the same guard repeatedly (measured
2026-09-11: 9 budget hard blocks in a day, with the same delegation hit
twice — each message 113-156 characters sharing an identical prefix).

Duplicate content is the textbook definition of context pollution
(irrelevant/stale/**duplicated** information competing with the signal),
and repeating a near-identical span also feeds the self-reinforcing
degeneration documented for low-temperature decoding (arXiv:2512.04419:
"self-reinforcement effect combined with greedy decoding … the model
becomes trapped"; scenario 34 showed a 69x-repeated reasoning loop).

Design
------
The interception itself is unchanged — every blocked call is still
blocked.  Only the EXPLANATION is given once per (delegation, guard kind);
later triggers return a fixed short receipt.  This mirrors the dedup
channel's v3.37.4 receipt and keeps the guard's guidance available without
paying for duplicate long text in the context.

Portability: no backend/sampling assumption is involved (deliberately so —
repetition-penalty knobs are environment-specific and are NOT relied upon).
"""

from __future__ import annotations

from collections import OrderedDict

# Deliberately short and IDENTICAL on every repeat: the long explanation is
# one message above, so this only needs to (a) confirm the block still
# stands and (b) name the way out.
SHORT_RECEIPT = (
    "[系统] 同类拦截已说明（见前文）：请勿重发，立即收尾并在 coverage_gaps 声明缺口。"
)

# Same bound as the ledger's tracked-delegation cap: a long session must
# not grow this registry without limit.
_MAX_TRACKED_CONTEXTS = 64


class ReceiptRegistry:
    """Per-context 'explain once, then a fixed one-liner' registry.

    Context key = the delegation key (or the Coordinator's fixed key), so
    parallel delegations never consume each other's explanations.
    """

    def __init__(self) -> None:
        self._seen: OrderedDict[str, set[str]] = OrderedDict()

    def receipt(self, context_key: str, kind: str, full: str,
                short: str = SHORT_RECEIPT) -> str:
        """Return *full* the first time (context_key, kind) is seen, else *short*."""
        seen = self._seen.get(context_key)
        if seen is None:
            seen = set()
        if kind in seen:
            self._seen.move_to_end(context_key)
            return short
        seen.add(kind)
        self._seen[context_key] = seen
        self._seen.move_to_end(context_key)
        while len(self._seen) > _MAX_TRACKED_CONTEXTS:
            self._seen.popitem(last=False)
        return full

    def kinds_seen(self, context_key: str) -> set[str]:
        """Test/diagnostic helper: which guard kinds already explained."""
        return set(self._seen.get(context_key) or ())
