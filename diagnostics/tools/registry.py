from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from diagnostics.tools.linux_diagnostics import get_linux_diagnostic_tools


def get_agent_tools(extra_tools: Sequence[Any] = ()) -> list[Any]:
    """Return tools exposed to the main agent.

    MCP adapters can be added here later and merged into the returned list.
    Keeping this as the single registry avoids coupling the main agent factory
    to individual tool modules.
    """
    return [
        *get_linux_diagnostic_tools(),
        *extra_tools,
    ]
