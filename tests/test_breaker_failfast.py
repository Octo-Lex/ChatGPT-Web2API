"""Tests for the REST + MCP circuit-open fail-fast surface (Phase 4 PR2).

When a breaker is open, both transports must refuse FAST — before touching
Chrome — with a structured signal:

  - REST: HTTP 503 with ``code: circuit_open`` (mirrors the lock_timeout 503).
  - MCP: ``CallToolResult(isError=True)`` with a ``(circuit_open, kind=...)``
    machine token (mirrors the RateLimitError result shape).

These tests exercise the error-mapping seams directly and via the MCP
in-memory transport. No live Chrome — all via fakes/fixtures, matching the
patterns in ``test_api_rate_limit.py`` and ``test_mcp_rate_limit.py``.
"""

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

import chatgpt_web2api.mcp_server as mod
from chatgpt_web2api.breakers import BreakerKind, BreakerRegistry, CircuitOpenError
from chatgpt_web2api.config import Config

# ── REST: _error_response maps CircuitOpenError → 503 ─────────────────


def _server():
    """An APIServer with a throwaway config + driver (only _error_response used)."""
    from chatgpt_web2api.api_server import APIServer

    return APIServer(Config.load(None), MagicMock())


def test_rest_circuit_open_maps_to_503():
    """CircuitOpenError → HTTP 503, code circuit_open, message names the kind
    via kind.value (not the enum repr)."""
    server = _server()
    exc = CircuitOpenError(BreakerKind.COMPOSER_SEND_READINESS)
    resp = server._error_response(exc)

    assert resp.status == 503
    body = json.loads(resp.body)
    err = body["error"]
    assert err["type"] == "server_error"
    assert err["code"] == "circuit_open"
    assert err["param"] is None
    # kind rendered as its stable .value string, not <BreakerKind...>
    assert "composer_send_readiness" in err["message"]


def test_rest_circuit_open_each_kind_renders_value():
    """Every BreakerKind renders its .value in the REST error message."""
    server = _server()
    for kind in BreakerKind:
        resp = server._error_response(CircuitOpenError(kind))
        body = json.loads(resp.body)
        assert kind.value in body["error"]["message"]
        assert resp.status == 503


# ── MCP: open breaker → isError CallToolResult ────────────────────────


def _make_mcp_server_with_open_breaker():
    """Build a real MCP server whose breaker is OPEN (auth tripped), so the
    preflight in _run() raises CircuitOpenError before the handler runs."""
    driver = MagicMock()
    driver.dismiss_rate_limit = AsyncMock(return_value=True)
    driver.select_model = AsyncMock(return_value=True)
    driver.navigate_new_chat = AsyncMock()
    driver.navigate_conversation = AsyncMock()
    driver.ensure_current_conversation = AsyncMock()
    driver._current_conv_id = ""
    driver._current_model = None

    # If the handler somehow runs, yield a benign chunk (it should NOT).
    async def _stream(text, timeout=120):
        from chatgpt_web2api.cdp_driver import StreamChunk

        yield StreamChunk(delta="should-not-reach")
        yield StreamChunk(delta="", finish_reason="stop")

    driver.send_and_stream = _stream

    mod._driver = driver
    mod._config = Config.load(None)
    mod._lock = None
    # Open the auth breaker → preflight must refuse.
    reg = BreakerRegistry()
    reg.trip(BreakerKind.AUTH_EXPIRED, "test trip", cooldown_s=0)
    mod._breakers = reg
    return mod.create_server(), driver


@pytest.mark.asyncio
async def test_mcp_open_breaker_returns_circuit_open_error():
    """When a breaker is open, call_tool returns isError with the
    (circuit_open, kind=...) machine token, and the driver handler never runs."""
    server, driver = _make_mcp_server_with_open_breaker()

    from mcp.shared.memory import create_connected_server_and_client_session

    async with create_connected_server_and_client_session(server) as session:
        await session.initialize()
        result = await session.call_tool(
            "chat_completion",
            {
                "message": "hello",
                "model": "auto",
            },
        )

    # isError=True, no structuredContent (error payloads don't match schemas)
    assert result.isError is True
    text = result.content[0].text
    assert "circuit_open" in text
    assert "auth_required" in text  # kind.value rendered, not enum repr
    # The driver handler must NOT have run — send_and_stream is untouched.
    # (If it ran it would have produced content; here there's only the error.)


@pytest.mark.asyncio
async def test_mcp_closed_breaker_does_not_fail_fast(monkeypatch):
    """When all breakers are closed, tools proceed normally — no circuit_open."""
    import chatgpt_web2api.resilience as res

    async def _noop(_s):
        return None

    monkeypatch.setattr(res.asyncio, "sleep", _noop)

    driver = MagicMock()
    driver.dismiss_rate_limit = AsyncMock(return_value=True)
    driver.select_model = AsyncMock(return_value=True)
    driver.navigate_new_chat = AsyncMock()
    driver.navigate_conversation = AsyncMock()
    driver.ensure_current_conversation = AsyncMock()
    driver._current_conv_id = ""
    driver._current_model = None

    async def _stream(text, timeout=120):
        from chatgpt_web2api.cdp_driver import StreamChunk

        yield StreamChunk(delta="ok")
        yield StreamChunk(delta="", finish_reason="stop")

    driver.send_and_stream = _stream

    mod._driver = driver
    mod._config = Config.load(None)
    mod._lock = None
    mod._breakers = BreakerRegistry()  # all closed

    server = mod.create_server()
    from mcp.shared.memory import create_connected_server_and_client_session

    async with create_connected_server_and_client_session(server) as session:
        await session.initialize()
        result = await session.call_tool(
            "chat_completion",
            {"message": "hello", "model": "auto"},
        )

    assert result.isError is False
