"""Server package.

`create_app` is re-exported lazily (PEP 562) rather than imported eagerly.
`diagnostics.agent.streaming` depends on `diagnostics.server.step_tracker`, and
importing any submodule executes this `__init__` first. An eager
`from diagnostics.server.app import create_app` therefore closed a cycle
(`agent.streaming` -> `server.step_tracker` -> `server/__init__` -> `server.app`
-> `agent.streaming`) that broke `import diagnostics.agent.streaming` whenever
it was the first module touched. Resolving the attribute on access keeps
`from diagnostics.server import create_app` working while leaving the package
`__init__` dependency-free.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from fastapi import FastAPI

__all__ = ["create_app"]


def __getattr__(name: str) -> Any:
    if name == "create_app":
        from diagnostics.server.app import create_app

        return create_app
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted(__all__)


if TYPE_CHECKING:  # pragma: no cover - typing surface only
    def create_app(*args: Any, **kwargs: Any) -> "FastAPI": ...
