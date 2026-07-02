"""Tool offload middleware — configurable per-tool context compression.

When a configured tool returns a result exceeding the token threshold,
the full content is written to the shared backend filesystem and the
ToolMessage is replaced with a compact head+tail preview plus the file
path.  The LLM can then use built-in read_file / grep tools to retrieve
specific information on demand.

Design goals:
- Only uses public langchain/deepagents APIs (no private _* imports)
- Shares the same BackendProtocol instance as FilesystemMiddleware so
  that offloaded files are accessible via read_file / grep / ls
- Configurable tool name set and per-tool token limit

See: private/OFFLOAD_MIDDLEWARE_DESIGN.md (if created)
"""

from __future__ import annotations

import logging
import re
from collections.abc import Awaitable, Callable

from langchain.agents.middleware.types import (
    AgentMiddleware,
    ModelRequest,
    ModelResponse,
)
from langchain_core.messages import ToolMessage
from langgraph.prebuilt.tool_node import ToolCallRequest
from langgraph.types import Command

from deepagents.backends.protocol import BackendProtocol

logger = logging.getLogger(__name__)

# ── Constants ──────────────────────────────────────────────────────────────

_NUM_CHARS_PER_TOKEN = 4
"""Approximate characters per token (matches deepagents convention)."""

_LARGE_TOOL_RESULTS_PREFIX = "/large_tool_results"
"""Virtual filesystem path prefix for offloaded tool results."""

_HEAD_LINES = 5
"""Number of lines to show from the start of an offloaded result."""

_TAIL_LINES = 5
"""Number of lines to show from the end of an offloaded result."""

# Message template shown to the LLM when a tool result is offloaded.
# Mirrors the structure of deepagents' TOO_LARGE_TOOL_MSG but is
# self-contained to avoid private-API coupling.
_OFFLOAD_NOTICE = """\
[Tool result offloaded] The result of tool call "{tool_call_id}" ({tool_name}) \
was too large ({total_lines} lines, ~{approx_tokens} tokens) and has been saved to:

  {file_path}

A preview of the first {_head} and last {_tail} lines is shown below. \
Use read_file(path, offset, limit) to page through the full content, or \
grep(pattern, path="{file_path}") to search for specific information.
{keyword_hint}
--- preview (head {_head} lines) ---
{head_preview}

... [{skipped_lines} lines truncated] ...

--- preview (tail {_tail} lines) ---
{tail_preview}
"""

# System prompt snippet injected once to inform the LLM about the mechanism.
_OFFLOAD_SYSTEM_PROMPT = """\

## Tool Result Offloading

Some diagnostic tools may return very large results. When this happens, the \
full content is automatically saved to a file and you receive a compact preview \
instead. You can then:
- Use `read_file(path, offset=0, limit=200)` to page through the full content
- Use `grep(pattern, path="<the file path>")` to search for specific keywords

When suggested search keywords are provided, they are merely common fault \
indicators — **not an exhaustive list**. You should:
1. Start with the suggested keywords for quick triage
2. Read the head/tail preview carefully — look for error codes, timestamps, \
   metric names, or anomaly indicators not in the suggested list
3. Grep for any pattern YOU find relevant based on your diagnostic reasoning

This is intentional — do NOT re-call the tool to get the full result. \
Always use read_file or grep on the offloaded file path.\
"""

# Sanitize tool_call_id for use as a filename (keep only safe chars)
_SAFE_ID_RE = re.compile(r'[^a-zA-Z0-9_\-]')


def _sanitize_id(raw_id: str) -> str:
    """Convert a tool_call_id to a safe filename component."""
    return _SAFE_ID_RE.sub("_", raw_id)[:80] or "unknown"


def _create_preview(lines: list[str], head: int, tail: int) -> tuple[str, str, int]:
    """Create head and tail previews from a list of lines.

    Returns:
        Tuple of (head_preview, tail_preview, skipped_line_count).
    """
    if len(lines) <= head + tail:
        # Small enough — no truncation needed
        head_text = "\n".join(lines)
        return head_text, "", 0

    head_lines = lines[:head]
    tail_lines = lines[-tail:]
    skipped = len(lines) - head - tail

    # Add line numbers for readability
    head_text = "\n".join(f"{i+1:>4}| {line}" for i, line in enumerate(head_lines))
    tail_start = len(lines) - tail + 1
    tail_text = "\n".join(
        f"{tail_start + i:>4}| {line}" for i, line in enumerate(tail_lines)
    )
    return head_text, tail_text, skipped


class ToolOffloadMiddleware(AgentMiddleware):
    """Configurable per-tool offload middleware.

    When a tool in `tool_names` returns a result exceeding `token_limit`
    tokens, the full content is written to the shared backend filesystem
    and the ToolMessage is replaced with a compact preview + file path.

    Args:
        tool_names: Set of tool names that should be subject to offload.
            Tools NOT in this set pass through unchanged.
        token_limit: Maximum token count before offload triggers.
            Default 8000 (~32000 chars).
        backend: The shared BackendProtocol instance (must be the same
            instance used by FilesystemMiddleware).
        prefix: Filesystem path prefix for offloaded files.
            Default "/large_tool_results".
        head_lines: Number of preview lines from the start. Default 5.
        tail_lines: Number of preview lines from the end. Default 5.
    """

    def __init__(
        self,
        *,
        tool_names: set[str],
        token_limit: int = 8000,
        backend: BackendProtocol,
        prefix: str = _LARGE_TOOL_RESULTS_PREFIX,
        head_lines: int = _HEAD_LINES,
        tail_lines: int = _TAIL_LINES,
        hint_keywords: dict[str, list[str]] | None = None,
    ) -> None:
        self._tool_names = frozenset(tool_names)
        self._char_limit = token_limit * _NUM_CHARS_PER_TOKEN
        self._backend = backend
        self._prefix = prefix.rstrip("/")
        self._head_lines = head_lines
        self._tail_lines = tail_lines
        # Per-tool keyword hints: shown in offload notice to guide LLM grep.
        # These are *suggestions*, not exhaustive — the LLM is free to search
        # for any pattern it deems relevant.
        self._hint_keywords: dict[str, list[str]] = hint_keywords or {}
        self._offload_count = 0  # cross-call counter (safe: middleware is single-agent)
        logger.info(
            "ToolOffloadMiddleware initialized: tools=%s, token_limit=%d, char_limit=%d, hints=%d tools",
            sorted(self._tool_names), token_limit, self._char_limit,
            len(self._hint_keywords),
        )

    # ── System prompt injection ────────────────────────────────────────────

    def modify_model_request(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelRequest],
    ) -> ModelRequest:
        """Inject offload usage instructions into the system prompt."""
        modified = handler(request)
        if modified.system_message is not None:
            existing = modified.system_message.content or ""
            if "Tool Result Offloading" not in existing:
                from langchain_core.messages import SystemMessage
                modified = modified.override(
                    system_message=SystemMessage(
                        content=existing + "\n\n" + _OFFLOAD_SYSTEM_PROMPT,
                    ),
                )
        return modified

    # ── Tool call interception ─────────────────────────────────────────────

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command]],
    ) -> ToolMessage | Command:
        """Intercept tool results and offload if configured and oversized."""
        result = await handler(request)

        tool_name = request.tool_call.get("name", "")
        if tool_name not in self._tool_names:
            return result

        # ── Never offload scaffolding / filesystem tools ──
        # These tools let the LLM access file content — offloading their
        # results would create an infinite loop: read_file returns preview,
        # LLM calls read_file again to get full content, preview again, ...
        # Deepagents itself excludes these in its TOO_LARGE_TOOL_MSG.
        _OFFLOAD_BLACKLIST = frozenset({
            "read_file", "write_file", "edit_file",
            "grep", "glob", "ls",
            "write_todos", "read_todos",
        })
        if tool_name in _OFFLOAD_BLACKLIST:
            return result

        # Only process ToolMessage (skip Command objects from sub-agents)
        if not isinstance(result, ToolMessage):
            return result

        content = result.content
        if not isinstance(content, str):
            content = str(content) if content else ""

        # Fast-path: under the limit → no offload
        if len(content) <= self._char_limit:
            return result

        # ── Offload triggered ──────────────────────────────────────────
        return await self._offload(result, content, tool_name)

    # ── Core offload logic ─────────────────────────────────────────────────

    async def _offload(
        self,
        original: ToolMessage,
        content: str,
        tool_name: str,
    ) -> ToolMessage:
        """Write full content to backend and return a preview replacement."""
        tool_call_id = original.tool_call_id or "unknown"
        safe_id = _sanitize_id(tool_call_id)
        file_path = f"{self._prefix}/{safe_id}"

        # Write to the shared backend filesystem
        try:
            write_result = await self._backend.awrite(file_path, content)
            if write_result is not None and write_result.error:
                logger.warning(
                    "Offload write failed for %s: %s", tool_call_id, write_result.error,
                )
                # Fall back to truncated content in-place (no file)
                return self._fallback_truncate(original, content, tool_name)
        except Exception as e:
            logger.warning("Offload write exception for %s: %s", tool_call_id, e)
            return self._fallback_truncate(original, content, tool_name)

        # Build preview
        lines = content.splitlines()
        total_lines = len(lines)
        approx_tokens = total_lines * 20  # rough estimate
        head_preview, tail_preview, skipped = _create_preview(
            lines, self._head_lines, self._tail_lines,
        )

        # Build keyword hint for this tool (if configured)
        keyword_hint = self._build_keyword_hint(tool_name, file_path)

        if not tail_preview:
            # Content fits within head+tail, no truncation marker needed
            notice = (
                f"[Tool result offloaded] The result of tool call "
                f"\"{tool_call_id}\" ({tool_name}) was saved to:\n\n"
                f"  {file_path}\n\n"
                f"Use read_file or grep to access the full content.\n"
                f"{keyword_hint}\n"
                f"--- content ({total_lines} lines) ---\n{head_preview}"
            )
        else:
            notice = _OFFLOAD_NOTICE.format(
                tool_call_id=tool_call_id,
                tool_name=tool_name,
                total_lines=total_lines,
                approx_tokens=approx_tokens,
                file_path=file_path,
                _head=self._head_lines,
                _tail=self._tail_lines,
                head_preview=head_preview,
                tail_preview=tail_preview,
                skipped_lines=skipped,
                keyword_hint=keyword_hint,
            )

        self._offload_count += 1
        logger.info(
            "Offloaded tool result: %s (%s) → %s (%d lines → preview)",
            tool_name, tool_call_id, file_path, total_lines,
        )

        # Build replacement ToolMessage preserving identity fields
        return ToolMessage(
            content=notice,
            tool_call_id=original.tool_call_id,
            name=original.name,
            id=original.id,
            status=original.status,
            additional_kwargs=dict(original.additional_kwargs),
        )

    def _build_keyword_hint(self, tool_name: str, file_path: str) -> str:
        """Build a keyword hint section for the offload notice.

        Returns an empty string if no hints are configured for this tool.
        The wording encourages the LLM to use hints as a starting point
        while also exploring freely.
        """
        keywords = self._hint_keywords.get(tool_name)
        if not keywords:
            return ""
        # Format: suggest grep commands for each keyword
        grep_examples = "\n".join(
            f'  grep("{kw}", path="{file_path}")' for kw in keywords
        )
        return (
            "\nSuggested search keywords (these are common fault indicators, "
            "NOT exhaustive — based on the preview below, grep for any other "
            "patterns you notice such as error codes, abnormal metrics, or "
            "suspicious timestamps):\n"
            f"{grep_examples}\n"
        )

    def _fallback_truncate(
        self,
        original: ToolMessage,
        content: str,
        tool_name: str,
    ) -> ToolMessage:
        """Fallback when backend write fails: truncate in-place."""
        lines = content.splitlines()
        preview_lines = lines[: self._head_lines + self._tail_lines]
        preview = "\n".join(preview_lines)
        truncated_notice = (
            f"[Tool result truncated] {tool_name} returned {len(lines)} lines "
            f"but offload failed. Showing preview only:\n\n{preview}\n\n"
            f"... [{max(0, len(lines) - len(preview_lines))} lines omitted] ..."
        )
        return ToolMessage(
            content=truncated_notice,
            tool_call_id=original.tool_call_id,
            name=original.name,
            id=original.id,
            status=original.status,
        )
