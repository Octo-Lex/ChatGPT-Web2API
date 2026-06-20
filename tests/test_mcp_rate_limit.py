"""Tests for the MCP server's rate-limit error handling.

MCP has no transport-level retry-after, so a persistent rate limit is
signaled as a CallToolResult with isError=True and a machine-readable
marker (rate_limit_exceeded, retry_after=N) in the text content. The
error info is in text rather than structuredContent because the MCP SDK
validates structuredContent against the tool's outputSchema, and the
error payload deliberately doesn't match the success schema.

The chat tools are also wrapped in retry_on_rate_limit so a transient
limit is retried transparently first; only a persistent limit reaches
the client.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from chatgpt_web2api.cdp_driver import RateLimitError
import chatgpt_web2api.mcp_server as mod


def _make_server_with_raising_driver(raises: Exception | None):
    """Build a real MCP server whose driver raises (or returns ok) on chat."""
    driver = MagicMock()
    # dismiss is best-effort
    driver.dismiss_rate_limit = AsyncMock(return_value=True)
    # select_model + navigation are no-ops
    driver.select_model = AsyncMock(return_value=True)
    driver.navigate_new_chat = AsyncMock()
    driver.navigate_conversation = AsyncMock()
    driver._current_conv_id = ""
    driver._current_model = None

    async def _stream(text, timeout=120):
        if raises is not None:
            raise raises
        from chatgpt_web2api.cdp_driver import StreamChunk
        yield StreamChunk(delta="ok")
        yield StreamChunk(delta="", finish_reason="stop")

    driver.send_and_stream = _stream

    mod._driver = driver
    mod._config = mod.Config.load(None)
    # No lock: avoids the cross-loop asyncio.Lock issue in the in-memory
    # transport (the lock is created on the setup loop, dead in the session
    # loop). Serialization isn't what these tests verify.
    mod._lock = None
    return mod.create_server(), driver


# ── Persistent rate limit -> structured isError result ────────

@pytest.mark.asyncio
async def test_mcp_chat_persistent_rate_limit_returns_structured_error(monkeypatch):
    """When every retry is throttled, call_tool returns isError with a
    machine-readable marker in the text content.

    The error info (rate_limited, retry_after) is embedded in the text rather
    than structuredContent because the MCP SDK validates structuredContent
    against the tool's outputSchema, and the error payload deliberately
    doesn't match any tool's success schema.
    """
    # Patch sleep so the (exhausted) retries don't make the test take minutes.
    import chatgpt_web2api.resilience as res
    async def _noop(_s): return None
    monkeypatch.setattr(res.asyncio, "sleep", _noop)

    # Raise on EVERY attempt → retries exhaust → RateLimitError propagates
    # to call_tool, which converts it to an error result.
    server, _ = _make_server_with_raising_driver(RateLimitError(retry_after=90))
    from mcp.shared.memory import create_connected_server_and_client_session

    async with create_connected_server_and_client_session(server) as session:
        await session.initialize()
        result = await session.call_tool(
            "chat_completion", {"message": "hi"}
        )

    assert result.isError is True
    # Machine-readable markers in the text content:
    text = result.content[0].text.lower()
    assert "rate" in text
    assert "90" in text  # retry_after value
    assert "rate_limit_exceeded" in text


# ── Transient rate limit -> transparent retry succeeds ────────

@pytest.mark.asyncio
async def test_mcp_chat_transient_rate_limit_retries_transparently(monkeypatch):
    """A rate limit that clears on retry is invisible: the tool succeeds normally.

    First attempt throttled, second succeeds → client sees a normal (non-error)
    result, no rate-limited signal. This is the 'make workflows practical' win.
    """
    import chatgpt_web2api.resilience as res
    async def _noop(_s): return None
    monkeypatch.setattr(res.asyncio, "sleep", _noop)

    driver = MagicMock()
    driver.dismiss_rate_limit = AsyncMock(return_value=True)
    driver.select_model = AsyncMock(return_value=True)
    driver.navigate_new_chat = AsyncMock()
    driver._current_conv_id = "conv-1"
    driver._current_model = None

    attempts = {"n": 0}

    async def _stream(text, timeout=120):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise RateLimitError(retry_after=1)
        from chatgpt_web2api.cdp_driver import StreamChunk
        yield StreamChunk(delta="recovered")
        yield StreamChunk(delta="", finish_reason="stop")

    driver.send_and_stream = _stream
    mod._driver = driver
    mod._config = mod.Config.load(None)
    mod._lock = None  # see _make_server_with_raising_driver re: cross-loop lock
    server = mod.create_server()

    from mcp.shared.memory import create_connected_server_and_client_session

    async with create_connected_server_and_client_session(server) as session:
        await session.initialize()
        result = await session.call_tool(
            "chat_completion", {"message": "hi"}
        )

    assert result.isError is not True  # transparent recovery
    assert "recovered" in result.structuredContent.get("content", "")
    assert attempts["n"] == 2  # retried exactly once
