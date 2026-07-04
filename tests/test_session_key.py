"""Tests for B1 session-key extraction (Step 2).

Verifies the resolution order:
  1. SSE session_id from request_context.request.query_params → f"sse:{id}"
  2. stdio → "stdio-singleton"
  3. Pool disabled → "singleton"
  4. Pool enabled + SSE + no session_id → None (fail-closed)
"""
from __future__ import annotations

from unittest.mock import MagicMock

from chatgpt_web2api.session_key import current_mcp_session_key


def _make_server_with_session_id(session_id: str | None) -> MagicMock:
    """Build a mock server whose request_context.request.query_params has the session_id."""
    server = MagicMock()
    if session_id is None:
        server.request_context.request.query_params.get.return_value = None
    else:
        server.request_context.request.query_params.get.return_value = session_id
    return server


def _make_server_no_request() -> MagicMock:
    """Build a mock server whose request_context.request is None."""
    server = MagicMock()
    server.request_context.request = None
    return server


def _make_server_lookup_error() -> MagicMock:
    """Build a mock server whose request_context raises LookupError."""
    server = MagicMock()
    # Use a property that raises when accessed.
    server._request_context_raises = LookupError("no context")
    # Patch request_context as a property on the mock.
    p = property(lambda self: (_ for _ in ()).throw(self._request_context_raises))
    type(server).request_context = p
    return server


class TestSseSessionId:
    def test_sse_with_session_id(self):
        server = _make_server_with_session_id("abc123def456")
        key = current_mcp_session_key(server, transport="sse", pool_enabled=True)
        assert key == "sse:abc123def456"

    def test_sse_with_session_id_pool_disabled(self):
        """Even with pool disabled, SSE session_id is returned (for diagnostics)."""
        server = _make_server_with_session_id("xyz789")
        key = current_mcp_session_key(server, transport="sse", pool_enabled=False)
        assert key == "sse:xyz789"


class TestStdioFallback:
    def test_stdio_returns_singleton(self):
        server = _make_server_no_request()
        key = current_mcp_session_key(server, transport="stdio", pool_enabled=False)
        assert key == "stdio-singleton"

    def test_stdio_pool_enabled_returns_singleton(self):
        """stdio never fails closed — it's always a singleton."""
        server = _make_server_no_request()
        key = current_mcp_session_key(server, transport="stdio", pool_enabled=True)
        assert key == "stdio-singleton"


class TestPoolDisabled:
    def test_sse_no_session_id_pool_disabled_returns_singleton(self):
        server = _make_server_with_session_id(None)
        key = current_mcp_session_key(server, transport="sse", pool_enabled=False)
        assert key == "singleton"


class TestFailClosed:
    def test_sse_no_session_id_pool_enabled_returns_none(self):
        """Pool-enabled SSE with no session_id → None (fail-closed)."""
        server = _make_server_with_session_id(None)
        key = current_mcp_session_key(server, transport="sse", pool_enabled=True)
        assert key is None

    def test_sse_no_request_pool_enabled_returns_none(self):
        server = _make_server_no_request()
        key = current_mcp_session_key(server, transport="sse", pool_enabled=True)
        assert key is None

    def test_sse_lookup_error_pool_enabled_returns_none(self):
        server = _make_server_lookup_error()
        key = current_mcp_session_key(server, transport="sse", pool_enabled=True)
        assert key is None


class TestErrorHandling:
    def test_unexpected_error_in_context_falls_through(self):
        """If request_context raises something other than LookupError, it's caught."""
        server = MagicMock()
        server._request_context_raises = RuntimeError("boom")
        p = property(lambda self: (_ for _ in ()).throw(self._request_context_raises))
        type(server).request_context = p
        # Should fall through to transport check, not crash.
        key = current_mcp_session_key(server, transport="stdio", pool_enabled=False)
        assert key == "stdio-singleton"


class TestNeverRandom:
    def test_no_session_id_does_not_generate_random_key(self):
        """The function must NEVER generate a random per-request key."""
        server = _make_server_with_session_id(None)
        key1 = current_mcp_session_key(server, transport="sse", pool_enabled=False)
        key2 = current_mcp_session_key(server, transport="sse", pool_enabled=False)
        # Both calls return the same deterministic "singleton" — not a random key.
        assert key1 == key2 == "singleton"
