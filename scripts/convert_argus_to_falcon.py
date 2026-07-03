"""Convert scenario argus Markdown data → Open-Falcon JSON format (in-place).

Parses existing Markdown table lambdas in scenario_data/*.py and replaces them
with Open-Falcon structured JSON dicts.

Usage:
    uv run python scripts/convert_argus_to_falcon.py          # convert all
    uv run python scripts/convert_argus_to_falcon.py --dry-run # preview only
    uv run python scripts/convert_argus_to_falcon.py --check   # verify format

Before (Markdown lambda):
    "argus_cpu": lambda: \"\"\"## Argus CPU — prod-web-01
    **时间范围**: 15:00 ~ 15:09  **粒度**: 1min
    | 时间 | CPU% | Load | ...
    ...

After (Open-Falcon dict):
    "argus_cpu": {
        "endpoint": "prod-web-01",
        "step": 60,
        "data": [
            {"endpoint": "prod-web-01", "counter": "cpu.usage.percent",
             "dstype": "GAUGE", "step": 60,
             "Values": [{"timestamp": 1710000000, "value": 2}, ...]},
            ...
        ]
    }
"""

from __future__ import annotations

import ast
import json
import re
import sys
from pathlib import Path
from typing import Any

# ── Counter name mapping: Markdown column header → Open-Falcon counter ──

_COUNTER_MAP: dict[str, list[tuple[str, str]]] = {
    # argus_cpu columns
    "argus_cpu": [
        ("CPU%", "cpu.usage.percent"),
        ("Load", "load.1min"),
        ("iowait%", "cpu.iowait.percent"),
        # Top Process column (col 4) is text-only metadata, skip it
    ],
    # argus_memory columns (OOM Events column is text, gets filtered as NaN)
    "argus_memory": [
        ("Mem%", "mem.used.percent"),
        ("Swap%", "mem.swap.percent"),
        ("Available(MiB)", "mem.available.mib"),
        ("Available(GiB)", "mem.available.gib"),
    ],
    # argus_disk columns
    "argus_disk": [
        ("Util%", "disk.util.percent"),
        ("IOPS(r/s)", "disk.read.iops"),
        ("IOPS(w/s)", "disk.write.iops"),
        ("await(ms)", "disk.await.ms"),
    ],
    # argus_network columns
    "argus_network": [
        ("RX(Mbps)", "net.rx.mbps"),
        ("TX(Mbps)", "net.tx.mbps"),
        ("丢包", "net.drops"),
        ("重传%", "net.retransmit.percent"),
        ("ESTAB", "net.estab"),
    ],
    # argus_nodes columns
    "argus_nodes": [
        ("NotReady", "k8s.node.notready"),
        ("PodRestarts", "k8s.pod.restarts"),
        ("Evictions", "k8s.pod.evictions"),
        ("Pending", "k8s.pod.pending"),
    ],
    # argus_services columns
    "argus_services": [
        ("API Lat(ms)", "k8s.api.latency.ms"),
        ("etcd Ldr", "k8s.etcd.leader"),
        ("DB(MB)", "k8s.etcd.db.size.mb"),
        ("DNS Lat(ms)", "k8s.dns.latency.ms"),
        ("DNS Err", "k8s.dns.errors"),
    ],
}

# ── Parsing helpers ────────────────────────────────────────────────────────

_HEADER_RE = re.compile(r'## Argus\s+\S+\s*[—\-]\s*(.+?)$', re.MULTILINE)
_TIME_RE = re.compile(r'\*\*时间范围\*\*:\s*(\d{1,2}:\d{2})\s*~\s*(\d{1,2}:\d{2})')
_TABLE_ROW_RE = re.compile(r'^\|\s*(\d+)\s*\|(.+)\|$')
_DEFAULT_RE = re.compile(r'Argus:\s*无异常数据返回')


def _parse_header(text: str) -> tuple[str, str, str]:
    """Parse header to get endpoint, start_time, end_time."""
    endpoint = ""
    start_time = "00:00"
    end_time = "00:10"
    m = _HEADER_RE.search(text)
    if m:
        endpoint = m.group(1).strip()
    m = _TIME_RE.search(text)
    if m:
        start_time = m.group(1)
        end_time = m.group(2)
    return endpoint, start_time, end_time


def _time_to_minutes(t: str) -> int:
    """Convert HH:MM to minutes since midnight."""
    parts = t.split(":")
    return int(parts[0]) * 60 + int(parts[1])


def _generate_timestamps(start_hhmm: str, num_points: int, step_sec: int = 60) -> list[int]:
    """Generate absolute-ish timestamps for mock data.
    Uses a fixed base date (2026-01-01) plus the start time.
    """
    import datetime
    base = datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc)
    start_min = _time_to_minutes(start_hhmm)
    base = base.replace(hour=start_min // 60, minute=start_min % 60, second=0)
    return [int((base + datetime.timedelta(seconds=i * step_sec)).timestamp())
            for i in range(num_points)]


def _parse_numeric(val: str) -> float | None:
    """Extract numeric value from a table cell, handling text like '1.5', '⚠ 5.2G', etc."""
    if val is None:
        return None
    val = val.strip()
    if val in ("—", "-", "", "N/A", "n/a"):
        return 0.0  # treat missing as 0
    # Remove leading emoji/non-numeric markers but keep minus and digits
    cleaned = re.sub(r'^[^\d.\-]+', '', val)
    cleaned = re.sub(r'[^\d.\-]+$', '', cleaned)
    # Remove trailing units like 'G', 'M', 'K'
    cleaned = re.sub(r'[a-zA-Z]+$', '', cleaned)
    try:
        return float(cleaned)
    except ValueError:
        return None


def _parse_markdown_table(text: str, argus_key: str) -> list[list[float | None]]:
    """Parse a markdown table, return list of rows with parsed values.

    Returns empty list if table not found.
    """
    # Find table section: from first | header line to ### 摘要
    lines = text.split("\n")
    table_start = -1
    table_end = len(lines)
    header_count = 0
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("|") and "---" not in stripped:
            if table_start < 0:
                table_start = i
            # Count header rows (they contain column names, not numeric data)
            # Header row typically has non-numeric first column value
            cols = [c.strip() for c in stripped.split("|")[1:-1]]
            if cols and not cols[0].isdigit():
                header_count += 1
                continue
        if stripped.startswith("###"):
            table_end = i
            break
        if table_start >= 0 and not stripped.startswith("|") and i > table_start + header_count:
            table_end = i
            break

    if table_start < 0:
        return []

    # Now parse numeric rows (skip header + separator rows)
    rows = []
    for line in lines[table_start:table_end]:
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue
        cols = [c.strip() for c in stripped.split("|")[1:-1]]
        if not cols:
            continue
        # Skip header rows: first column is not a digit
        if not cols[0].isdigit():
            continue
        values = [_parse_numeric(c) for c in cols[1:]]  # skip time column
        rows.append(values)
    return rows


def _infer_counters_from_row(argus_key: str, sample_row: list[float | None]) -> list[str]:
    """Infer Open-Falcon counter names from a sample table row.

    Uses COUNTER_MAP for known columns; extra columns get auto-named.
    Non-numeric columns (like Top Process) that can't be parsed are
    skipped by the converter (no values to write).
    """
    mapping = _COUNTER_MAP.get(argus_key, [])
    counters = []
    for i in range(len(sample_row)):
        if i < len(mapping):
            counters.append(mapping[i][1])
        else:
            # Extra columns auto-named (they'll be empty and skipped)
            counters.append(f"{argus_key.replace('argus_', 'metric.')}.col{i}")
    # Remove duplicate counter names (keep first)
    seen: set[str] = set()
    result: list[str] = []
    for c in counters:
        if c not in seen:
            seen.add(c)
            result.append(c)
    return result


def _convert_one(text: str, argus_key: str) -> dict[str, Any] | None:
    """Convert a single Markdown argus data string to Open-Falcon JSON dict.

    Returns None if the text is the DEFAULT no-data placeholder.
    """
    if _DEFAULT_RE.search(text):
        return {"endpoint": "", "step": 60, "data": []}

    endpoint, start_time, end_time = _parse_header(text)
    rows = _parse_markdown_table(text, argus_key)
    if not rows:
        return None

    timestamps = _generate_timestamps(start_time, len(rows))
    counters = _infer_counters_from_row(argus_key, rows[0])

    data = []
    for col_idx, counter in enumerate(counters):
        values = []
        for row_idx, row in enumerate(rows):
            if col_idx < len(row) and row[col_idx] is not None:
                values.append({
                    "timestamp": timestamps[row_idx],
                    "value": row[col_idx],
                })
        if values:
            data.append({
                "endpoint": endpoint,
                "counter": counter,
                "dstype": "GAUGE",
                "step": 60,
                "Values": values,
            })

    result: dict[str, Any] = {
        "endpoint": endpoint,
        "step": 60,
        "data": data,
    }
    return result


# ── File-level conversion ─────────────────────────────────────────────────

def _extract_lambda_body(node: ast.Lambda) -> str:
    """Extract the string body of a lambda: lambda: \"\"\"...\"\"\""""
    body = node.body
    if isinstance(body, ast.Constant) and isinstance(body.value, str):
        return body.value
    return ""


def _format_dict_value(d: dict[str, Any], indent: int = 8) -> str:
    """Format a dict as Python source code with proper indentation."""
    json_str = json.dumps(d, ensure_ascii=False, indent=4)
    # Indent each line by 'indent' spaces
    lines = json_str.split("\n")
    indented = "\n".join(" " * indent + line for line in lines)
    return indented.strip()


def convert_file(filepath: Path, dry_run: bool = False) -> tuple[int, int]:
    """Convert one scenario file. Returns (converted, total) counts."""
    source = filepath.read_text(encoding="utf-8")
    tree = ast.parse(source)

    # Find the SPEC dict
    argus_assignments: list[tuple[int, int, str, str]] = []  # (start, end, key, value)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        for k, v in zip(node.keys, node.values):
            if not isinstance(k, ast.Constant) or not isinstance(k.value, str):
                continue
            if not k.value.startswith("argus_"):
                continue
            if not isinstance(v, ast.Lambda):
                continue
            text = _extract_lambda_body(v)
            if not text:
                continue
            # Find exact byte offsets
            argus_assignments.append((v.lineno, v.end_lineno or v.lineno,
                                       k.value, text))

    if not argus_assignments:
        return 0, 0

    lines = source.split("\n")
    offset = 0  # track line shifts from replacements
    converted = 0
    total = len(argus_assignments)

    for start_no, end_no, key, text in sorted(argus_assignments, key=lambda x: x[0]):
        # Adjust for previous replacements
        adj_start = start_no - 1 + offset  # 0-indexed
        adj_end = end_no - 1 + offset

        new_data = _convert_one(text, key)
        if new_data is None:
            continue

        # Build replacement: just the dict literal (not a lambda)
        dict_str = _format_dict_value(new_data)
        replacement = f'"{key}": {dict_str},'

        # Find the original line(s) and replace
        old_block = "\n".join(lines[adj_start:adj_end + 1])
        # We need to replace up to (but not including) the next entry or closing brace
        # Find the trailing comma after the lambda
        for i in range(adj_end, min(adj_end + 2, len(lines))):
            # Check if this lambda line already has a trailing comma
            if i < len(lines):
                stripped = lines[i].strip()
                # If the lambda ends with a comma on the same line or next line
                pass

        # Simple approach: replace from the lambda key to the closing """,)
        # The lambda key starts on the line before or same line
        # More robust: find the block from key assignment to end of lambda
        key_line = adj_start
        # Find the line with the key assignment
        for i in range(adj_start, adj_end + 1):
            if key in lines[i]:
                key_line = i
                break

        old_lines_count = (adj_end - key_line + 1)
        # Remove old lines
        del lines[key_line:key_line + old_lines_count]
        # Insert new lines
        new_lines = replacement.split("\n")
        for i, nl in enumerate(new_lines):
            lines.insert(key_line + i, nl)

        offset += len(new_lines) - old_lines_count
        converted += 1

    if not dry_run:
        filepath.write_text("\n".join(lines), encoding="utf-8")
        print(f"  ✓ {filepath.name}: {converted}/{total} converted")
    else:
        print(f"  [DRY RUN] {filepath.name}: would convert {converted}/{total}")

    return converted, total


# ── Main ───────────────────────────────────────────────────────────────────

def main():
    import argparse
    parser = argparse.ArgumentParser(description="Convert argus mock data to Open-Falcon format")
    parser.add_argument("--dry-run", action="store_true", help="Preview only, don't write files")
    parser.add_argument("--check", action="store_true", help="Verify converted files parse correctly")
    parser.add_argument("--file", type=str, help="Convert a single file (e.g., memory_leak.py)")
    args = parser.parse_args()

    data_dir = Path(__file__).resolve().parent.parent / "diagnostics" / "tools" / "mock" / "scenario_data"

    if args.check:
        _verify(data_dir)
        return

    if args.file:
        filepath = data_dir / args.file
        if not filepath.exists():
            print(f"Error: {filepath} not found")
            sys.exit(1)
        convert_file(filepath, dry_run=args.dry_run)
        return

    total_converted = 0
    total_keys = 0
    for f in sorted(data_dir.glob("*.py")):
        if f.name.startswith("_"):
            continue
        c, t = convert_file(f, dry_run=args.dry_run)
        total_converted += c
        total_keys += t

    print(f"\nTotal: {total_converted}/{total_keys} argus keys converted across files")


def _verify(data_dir: Path):
    """Verify all converted files are valid Python and data is parseable."""
    from diagnostics.tools.mock.argus_format import FalconData

    errors = []
    for f in sorted(data_dir.glob("*.py")):
        if f.name.startswith("_"):
            continue
        try:
            source = f.read_text()
            tree = ast.parse(source)
            for node in ast.walk(tree):
                if isinstance(node, ast.Dict):
                    for k, v in zip(node.keys, node.values):
                        if (isinstance(k, ast.Constant) and isinstance(k.value, str)
                                and k.value.startswith("argus_")):
                            if isinstance(v, ast.Dict):
                                # This is the new format — verify we can parse it
                                pass  # verified by import below
        except SyntaxError as e:
            errors.append(f"  ✗ {f.name}: SyntaxError: {e}")

    # Also try to actually load and parse the data
    for f in sorted(data_dir.glob("*.py")):
        if f.name.startswith("_"):
            continue
        try:
            spec_text = f.read_text()
            # Find SPEC dict values for argus keys
            for key in ["argus_cpu", "argus_memory", "argus_disk",
                         "argus_network", "argus_nodes", "argus_services"]:
                # Quick check: is this key a dict (new format)?
                if f'"{key}": {{' in spec_text or f"'{key}': {{" in spec_text:
                    # Import the module and check
                    import importlib
                    mod_name = f"diagnostics.tools.mock.scenario_data.{f.stem}"
                    try:
                        mod = importlib.import_module(mod_name)
                        spec = getattr(mod, "SPEC", {})
                        val = spec.get(key)
                        if isinstance(val, dict):
                            FalconData.from_dict(val)
                        elif val is not None:
                            errors.append(f"  ✗ {f.name}: {key} is not dict: {type(val)}")
                    except Exception as e:
                        errors.append(f"  ✗ {f.name}: {key} parse error: {e}")
        except Exception as e:
            errors.append(f"  ✗ {f.name}: {e}")

    if errors:
        print("Verification errors:")
        for e in errors:
            print(e)
    else:
        print("✓ All converted files pass verification")


if __name__ == "__main__":
    main()
