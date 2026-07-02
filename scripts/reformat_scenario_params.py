"""Reformat test-scenarios-full.md: one input param per line."""
import re
from pathlib import Path

f = Path(__file__).resolve().parent.parent / "private" / "test-scenarios-full.md"
lines = f.read_text(encoding="utf-8").split("\n")
out = []

for line in lines:
    m = re.match(r'^\*\*输入参数\*\*: (.*)$', line)
    if not m:
        out.append(line)
        continue

    # Parse comma-separated key:value pairs
    raw = m.group(1)
    # Split on "`, `" to get individual pairs
    pairs = re.findall(r'`([^`]+)`', raw)
    bullet_lines = ["**输入参数**:"]
    for p in pairs:
        # p is like: 'cluster_name: "prod-us-east"'
        bullet_lines.append(f"- `{p}`")
    out.extend(bullet_lines)

f.write_text("\n".join(out) + "\n", encoding="utf-8")
print(f"Done: reformatted {f.name}")
