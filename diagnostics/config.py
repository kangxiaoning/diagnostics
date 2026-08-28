from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ROOT_DIR = Path(__file__).resolve().parent.parent
load_dotenv(ROOT_DIR / ".env")
STATIC_DIR = ROOT_DIR / "static"


@dataclass(frozen=True)
class Settings:
    model: str = "qwen/qwen3.6-35b-a3b"
    base_url: str = "http://127.0.0.1:1234"
    api_key: str = "lm-studio"
    api_key_configured: bool = False
    temperature: float = 0.2
    # 16384 covers REPORT-phase generation AND reasoning-model subagent
    # summaries: qwen3.6's thinking stream consumes output budget, so the
    # former 8192 cap let a subagent's final summary be truncated at
    # finish=length (content=0B) — losing all decisive evidence
    # (2026-08-11, scenario 38).  Structured expert returns
    # (prompt-contract JSON) are compact, so the larger cap primarily
    # protects the reasoning stream + full report.  Honored by ollama
    # via OllamaChatOpenAI's top-level max_tokens passthrough.
    max_tokens: int = 16384
    max_history_messages: int = 16
    app_title: str = "Linux Diagnostics Agent"

    @classmethod
    def from_env(cls) -> "Settings":
        configured_api_key = (
            os.getenv("DIAGNOSTICS_API_KEY")
            or os.getenv("LM_STUDIO_API_KEY")
            or os.getenv("LM_API_TOKEN")
        )
        return cls(
            model=os.getenv("DIAGNOSTICS_MODEL", cls.model),
            base_url=openai_base_url(os.getenv("DIAGNOSTICS_BASE_URL", cls.base_url)),
            api_key=configured_api_key or cls.api_key,
            api_key_configured=configured_api_key is not None,
            temperature=float(os.getenv("DIAGNOSTICS_TEMPERATURE", str(cls.temperature))),
            max_tokens=int(os.getenv("DIAGNOSTICS_MAX_TOKENS", str(cls.max_tokens))),
            max_history_messages=int(
                os.getenv("DIAGNOSTICS_MAX_HISTORY_MESSAGES", str(cls.max_history_messages))
            ),
        )


def openai_base_url(base_url: str) -> str:
    normalized = base_url.rstrip("/")
    if normalized.endswith("/v1"):
        return normalized
    return f"{normalized}/v1"
