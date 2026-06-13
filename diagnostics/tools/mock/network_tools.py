"""Network diagnostic tool."""

from langchain_core.tools import tool

from diagnostics.tools.data import network_data
from diagnostics.tools.scenarios import _active_scenario


@tool
def check_network() -> str:
    """Check network connections (ss), interface stats, errors, drops, and TCP retransmits."""
    return network_data(_active_scenario)
