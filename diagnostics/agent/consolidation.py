"""Background memory consolidation — deduplicate and merge learnings.

Run periodically (e.g., daily cron) to:
1. Read LEARNINGS.md and remove duplicate entries
2. Merge similar cases under unified patterns
3. Extract reusable diagnostic patterns from /diagnosis_report.md history
4. Keep LEARNINGS.md concise and high-signal
"""

from __future__ import annotations

import pathlib
import re
from datetime import datetime


AGENT_DATA_ROOT = pathlib.Path(__file__).resolve().parent.parent.parent / "agent_data"
LEARNINGS_FILE = AGENT_DATA_ROOT / "LEARNINGS.md"


def _parse_entries(content: str) -> list[tuple[str, str, str | None]]:
    """Parse LEARNINGS.md into list of (header, body, root_cause) tuples."""
    entries: list[tuple[str, str, str | None]] = []
    pattern = re.compile(
        r"###\s+(.*?)\n(.*?)(?=\n###|\n<!--|\Z)", re.DOTALL
    )
    for m in pattern.finditer(content):
        header = m.group(1).strip()
        body = m.group(2).strip()
        # Extract root cause for dedup
        rc_match = re.search(r"\*\*根因\*\*：(.+)", body)
        root_cause = rc_match.group(1).strip() if rc_match else None
        if body:  # Skip empty entries
            entries.append((header, body, root_cause))
    return entries


def _is_duplicate(e1: tuple, e2: tuple) -> bool:
    """Check if two entries describe the same root cause pattern."""
    _, _, rc1 = e1
    _, _, rc2 = e2
    if rc1 and rc2:
        # Simple fuzzy: first 30 chars of root cause match
        return rc1[:30] == rc2[:30]
    return False


def consolidate() -> str:
    """Run consolidation and return the cleaned LEARNINGS.md content."""
    if not LEARNINGS_FILE.exists():
        return "No LEARNINGS.md found."

    content = LEARNINGS_FILE.read_text(encoding="utf-8")

    # Split header and entries section
    parts = content.split("---", 1)
    if len(parts) < 2:
        return "Invalid LEARNINGS.md format (missing --- separator)."

    header = parts[0].strip()
    entries_section = parts[1] if len(parts) > 1 else ""

    entries = _parse_entries(entries_section)

    # Remove duplicates
    seen: list[tuple[str, str, str | None]] = []
    unique: list[tuple[str, str, str | None]] = []
    removed = 0

    for entry in entries:
        if any(_is_duplicate(entry, s) for s in seen):
            removed += 1
            continue
        seen.append(entry)
        unique.append(entry)

    # Limit to last 30 entries
    if len(unique) > 30:
        unique = unique[-30:]

    # Rebuild
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    rebuilt_entries = "\n\n".join(
        f"### {h}\n{b}" for h, b, _ in unique
    )

    cleaned = (
        f"{header}\n\n---\n\n"
        f"{rebuilt_entries}\n\n"
        f"<!-- Last consolidation: {timestamp} -->\n"
        f"<!-- Total unique entries: {len(unique)}, removed duplicates: {removed} -->\n"
    )

    LEARNINGS_FILE.write_text(cleaned, encoding="utf-8")
    return (
        f"Consolidation complete: {len(unique)} unique entries, "
        f"{removed} duplicates removed."
    )


if __name__ == "__main__":
    result = consolidate()
    print(result)
