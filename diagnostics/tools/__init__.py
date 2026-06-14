"""Diagnostic tools package.

Directory structure:
    live/   — Production tools (real commands against infrastructure)
    mock/   — Mock tools (scenario-driven local testing)
    scripts/— Shell scripts shared between mock and live

Production tools:
    from diagnostics.tools import get_agent_tools, get_k8s_live_tools

Mock tools (local testing):
    from diagnostics.tools.mock import get_mock_tools, get_k8s_mock_tools
"""

from diagnostics.tools.registry import get_agent_tools, get_k8s_live_tools

__all__ = ["get_agent_tools", "get_k8s_live_tools"]
