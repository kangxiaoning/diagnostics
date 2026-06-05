from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from deepagents import create_deep_agent
from langchain.chat_models import init_chat_model

from diagnostics.agent.prompt import SYSTEM_PROMPT
from diagnostics.config import Settings
from diagnostics.tools import get_agent_tools


def build_agent(settings: Settings | None = None, extra_tools: Sequence[Any] = ()):
    settings = settings or Settings.from_env()
    model = init_chat_model(
        settings.model,
        model_provider="openai",
        base_url=settings.base_url,
        api_key=settings.api_key,
        temperature=settings.temperature,
    )
    return create_deep_agent(
        model=model,
        tools=get_agent_tools(extra_tools),
        system_prompt=SYSTEM_PROMPT,
    )
