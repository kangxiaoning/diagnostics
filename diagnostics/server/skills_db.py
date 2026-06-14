"""Skills SQLite database — stores and serves skill metadata for / command."""

from __future__ import annotations

import logging
import pathlib
import re
import sqlite3
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

DB_PATH = pathlib.Path(__file__).resolve().parent.parent.parent / "agent_data" / "skills.db"

# Category inference: map skill names to categories
_CATEGORY_MAP: dict[str, str] = {
    "cpu-diagnosis": "host",
    "memory-diagnosis": "host",
    "disk-io-diagnosis": "host",
    "network-diagnosis": "host",
    "system-health-check": "host",
    "gpu-diagnosis": "gpu",
    "kubernetes-diagnosis": "kubernetes",
    "container-runtime-diagnosis": "kubernetes",
    "control-plane-diagnosis": "kubernetes",
    "etcd-diagnosis": "kubernetes",
    "coredns-diagnosis": "kubernetes",
    "cross-layer-diagnosis": "cross-layer",
    "arp-cache-diagnosis": "kernel-network",
    "conntrack-diagnosis": "kernel-network",
    "mtu-misconfig-diagnosis": "kernel-network",
    "softirq-starvation": "kernel-network",
    "tcp-listen-overflow": "kernel-network",
    "grpc-connection-leak": "kernel-network",
    "kernel-parameter-drops": "kernel-network",
}


@dataclass(frozen=True)
class SkillInfo:
    id: str
    name: str
    description: str
    category: str


def _get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db() -> None:
    """Create the skills table if it doesn't exist."""
    conn = _get_connection()
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS skills (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                category TEXT NOT NULL DEFAULT 'other'
            )
        """)
        conn.commit()
        logger.info("Skills database initialized at %s", DB_PATH)
    finally:
        conn.close()


def _parse_frontmatter(text: str) -> dict[str, str]:
    """Extract YAML-like frontmatter fields from SKILL.md content."""
    result: dict[str, str] = {}
    if not text.startswith("---"):
        return result
    end = text.find("---", 3)
    if end == -1:
        return result
    fm = text[3:end]
    # Extract name:
    m = re.search(r'^name:\s*(.+)$', fm, re.MULTILINE)
    if m:
        result["name"] = m.group(1).strip()
    # Extract description (single-line or multi-line with >)
    m = re.search(r'^description:\s*"?([^"\n]+)"?\s*$', fm, re.MULTILINE)
    if m:
        result["description"] = m.group(1).strip()
    else:
        # Multi-line description with >
        m = re.search(r'^description:\s*>\s*\n((?:\s{2}.+\n?)*)', fm, re.MULTILINE)
        if m:
            desc = re.sub(r'\s{2,}', ' ', m.group(1)).strip()
            result["description"] = desc
    return result


def sync_skills_from_disk(skills_dir: str | pathlib.Path | None = None) -> int:
    """Scan the skills directory and sync SKILL.md metadata into the database.

    Returns the number of skills synced.
    """
    if skills_dir is None:
        skills_dir = pathlib.Path(__file__).resolve().parent.parent.parent / "agent_data" / "skills"

    skills_path = pathlib.Path(skills_dir)
    if not skills_path.is_dir():
        logger.warning("Skills directory not found: %s", skills_path)
        return 0

    conn = _get_connection()
    count = 0
    try:
        for skill_dir in sorted(skills_path.iterdir()):
            if not skill_dir.is_dir():
                continue
            skill_id = skill_dir.name
            md_file = skill_dir / "SKILL.md"
            if not md_file.is_file():
                continue

            content = md_file.read_text(encoding="utf-8")
            fm = _parse_frontmatter(content)

            name = fm.get("name", skill_id)
            description = fm.get("description", "")
            category = _CATEGORY_MAP.get(skill_id, "other")

            conn.execute(
                "INSERT OR REPLACE INTO skills (id, name, description, category) VALUES (?, ?, ?, ?)",
                (skill_id, name, description, category),
            )
            count += 1

        conn.commit()
        logger.info("Synced %d skills from %s", count, skills_path)
    finally:
        conn.close()
    return count


def get_all_skills() -> list[dict[str, Any]]:
    """Return all skills from the database."""
    conn = _get_connection()
    try:
        rows = conn.execute(
            "SELECT id, name, description, category FROM skills ORDER BY category, id"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def search_skills(query: str, limit: int = 20) -> list[dict[str, Any]]:
    """Search skills by id, name, or description."""
    conn = _get_connection()
    try:
        q = f"%{query}%"
        rows = conn.execute(
            "SELECT id, name, description, category FROM skills "
            "WHERE id LIKE ? OR name LIKE ? OR description LIKE ? "
            "ORDER BY category, id LIMIT ?",
            (q, q, q, limit),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()
