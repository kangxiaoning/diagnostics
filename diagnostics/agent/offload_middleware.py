"""Tool offload middleware — configurable per-tool context compression.

Uses `wrap_model_call` / `awrap_model_call` to scan messages right before
they are sent to the LLM.  When a ToolMessage from a configured tool
exceeds the token threshold, the full content is written to the shared
backend filesystem and the in-request message is replaced with a compact
head+tail preview plus the file path.  The LLM can then use built-in
read_file / grep tools to retrieve specific information on demand.

Because offloading runs in the *model-call* pipeline (not the *tool-execution*
pipeline), it never interferes with other tool-execution middlewares such as
ledger recording, dedup caching, or scope guarding.  Those middlewares always
see the original, unmodified tool result.

Design goals:
- Only uses public langchain/deepagents APIs (no private _* imports)
- Shares the same BackendProtocol instance as FilesystemMiddleware so
  that offloaded files are accessible via read_file / grep / ls
- Configurable tool name set and per-tool token limit
- Pipe-isolated: tool-execution middlewares see full results; the LLM
  receives compressed previews — no ordering dependency.

See: private/OFFLOAD_MIDDLEWARE_DESIGN.md (if created)
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from typing import TYPE_CHECKING

from langchain.agents.middleware.types import (
    AgentMiddleware,
    ModelRequest,
    ModelResponse,
)
from langchain_core.messages import AnyMessage, ToolMessage

from deepagents.backends.protocol import BackendProtocol

if TYPE_CHECKING:
    from collections.abc import Awaitable

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
    """Configurable per-tool offload middleware — runs in the model-call pipeline.

    When the agent is about to call the LLM, this middleware scans the
    pending messages for oversized ToolMessages from configured diagnostic
    tools.  Full content is written to the shared backend filesystem and
    the in-request message is replaced with a compact head+tail preview.

    Because the hook is `wrap_model_call` (not `awrap_tool_call`), the
    original ToolMessage in LangGraph state is *never* modified.  Only the
    copy sent to the LLM is compressed.  Tool-execution middlewares
    (ledger, dedup, scope guard, etc.) always see the unaltered result.

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

    # ── Model-call interception (wrap_model_call pipeline) ──────────────

    # Tools whose results must never be offloaded (filesystem scaffolding).
    # Offloading their results would create an infinite loop: read_file
    # returns preview → LLM calls read_file again → preview again → ...
    # Deepagents itself excludes these in its own TOO_LARGE_TOOL_MSG.
    _OFFLOAD_BLACKLIST: frozenset[str] = frozenset({
        "read_file", "write_file", "edit_file",
        "grep", "glob", "ls",
        "write_todos", "read_todos",
    })

    # Sentinel string in offloaded messages to prevent double-offloading
    # when wrap_model_call is invoked multiple times on the same messages
    # (e.g. during SummarizationMiddleware retry-on-overflow).
    _OFFLOAD_SENTINEL = "[Tool result offloaded]"

    def _should_offload(self, msg: ToolMessage) -> bool:
        """Return True if `msg` is a candidate for offloading.

        Does NOT check size — callers are responsible for that fast-path.
        """
        tool_name = msg.name or ""
        if tool_name not in self._tool_names:
            return False
        if tool_name in self._OFFLOAD_BLACKLIST:
            return False
        content = msg.content if isinstance(msg.content, str) else str(msg.content or "")
        if self._OFFLOAD_SENTINEL in content:
            return False  # already offloaded
        return True

    def _try_offload_one_sync(self, msg: ToolMessage) -> ToolMessage:
        """Check size and, if oversized, write to backend + return preview.

        Synchronous variant — uses `backend.write`.
        """
        content = msg.content if isinstance(msg.content, str) else str(msg.content or "")
        if len(content) <= self._char_limit:
            return msg
        tool_name = msg.name or ""
        tool_call_id = msg.tool_call_id or "unknown"
        safe_id = _sanitize_id(tool_call_id)
        file_path = f"{self._prefix}/{safe_id}"

        try:
            write_result = self._backend.write(file_path, content)
            if write_result is not None and write_result.error:
                logger.warning("Offload write failed for %s: %s", tool_call_id, write_result.error)
                return self._fallback_truncate(msg, content, tool_name)
        except Exception:
            logger.warning("Offload write exception for %s", tool_call_id, exc_info=True)
            return self._fallback_truncate(msg, content, tool_name)

        return self._build_offloaded_message(msg, content, tool_name, file_path)

    async def _try_offload_one_async(self, msg: ToolMessage) -> ToolMessage:
        """Check size and, if oversized, write to backend + return preview.

        Asynchronous variant — uses `backend.awrite`.
        """
        content = msg.content if isinstance(msg.content, str) else str(msg.content or "")
        if len(content) <= self._char_limit:
            return msg
        tool_name = msg.name or ""
        tool_call_id = msg.tool_call_id or "unknown"
        safe_id = _sanitize_id(tool_call_id)
        file_path = f"{self._prefix}/{safe_id}"

        try:
            write_result = await self._backend.awrite(file_path, content)
            if write_result is not None and write_result.error:
                logger.warning("Offload write failed for %s: %s", tool_call_id, write_result.error)
                return self._fallback_truncate(msg, content, tool_name)
        except Exception:
            logger.warning("Offload write exception for %s", tool_call_id, exc_info=True)
            return self._fallback_truncate(msg, content, tool_name)

        return self._build_offloaded_message(msg, content, tool_name, file_path)

    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelResponse:
        """Scan messages for oversized tool results and offload before the LLM sees them.

        Only the in-request message list is modified via `request.override`.
        State messages are never touched, so tool-execution middlewares
        (ledger, dedup, scope guard) always receive the original data.
        """
        messages = request.messages
        if not messages:
            return handler(request)

        # Scan messages and replace oversized ToolMessages with previews.
        modified = False
        new_messages: list[AnyMessage] = []
        for msg in messages:
            if isinstance(msg, ToolMessage) and self._should_offload(msg):
                replacement = self._try_offload_one_sync(msg)
                if replacement is not msg:
                    modified = True
                new_messages.append(replacement)
            else:
                new_messages.append(msg)

        if modified:
            return handler(request.override(messages=new_messages))
        return handler(request)

    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelResponse:
        """Async variant of `wrap_model_call`."""
        messages = request.messages
        if not messages:
            return await handler(request)

        modified = False
        new_messages: list[AnyMessage] = []
        for msg in messages:
            if isinstance(msg, ToolMessage) and self._should_offload(msg):
                replacement = await self._try_offload_one_async(msg)
                if replacement is not msg:
                    modified = True
                new_messages.append(replacement)
            else:
                new_messages.append(msg)

        if modified:
            return await handler(request.override(messages=new_messages))
        return await handler(request)

    # ── Core offload logic (shared by sync + async paths) ───────────────

    def _build_offloaded_message(
        self,
        original: ToolMessage,
        content: str,
        tool_name: str,
        file_path: str,
    ) -> ToolMessage:
        """Build a replacement ToolMessage with preview (pure, no I/O)."""
        tool_call_id = original.tool_call_id or "unknown"

        lines = content.splitlines()
        total_lines = len(lines)
        approx_tokens = total_lines * 20  # rough estimate
        head_preview, tail_preview, skipped = _create_preview(
            lines, self._head_lines, self._tail_lines,
        )
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
            additional_kwargs=dict(original.additional_kwargs),
        )
