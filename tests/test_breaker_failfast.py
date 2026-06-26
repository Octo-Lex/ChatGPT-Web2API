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
    driver.recover_auth = AsyncMock(return_value=False)
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
    driver.recover_auth = AsyncMock(return_value=False)
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


# ── Blocker 1: post-lock re-check catches a race ──────────────────────


@pytest.mark.asyncio
async def test_rest_post_lock_check_catches_race(monkeypatch):
    """If a breaker trips while a request waits on the cross-process lock, the
    post-lock check must catch it and return 503 without driving Chrome.

    Simulates: breaker closed at pre-lock check → trip during lock wait →
    post-lock check catches it. select_model / navigate_new_chat /
    send_and_stream must NOT be called.
    """
    import chatgpt_web2api.api_server as srv

    server = srv.APIServer.__new__(srv.APIServer)
    server._last_conv_id = None
    server._last_project_id = None
    server._request_count = 0
    server._cdp_port = 9222
    server._config = srv.Config.load(None)
    server._last_error = None
    reg = BreakerRegistry()
    server._breakers = reg

    driver = MagicMock()
    driver._current_conv_id = None
    driver._current_model = None
    driver.recover_auth = AsyncMock(return_value=False)
    driver.select_model = AsyncMock(return_value=True)
    driver.navigate_new_chat = AsyncMock()
    driver.navigate_conversation = AsyncMock()
    driver.ensure_current_conversation = AsyncMock()

    async def _stream(text, timeout=120):
        yield MagicMock()

    driver.send_and_stream = _stream
    server._driver = driver

    # A lock wrapper that trips a breaker ONCE (simulating a concurrent request
    # tripping it during the wait). The pre-lock check sees closed; the post-
    # lock check sees open.
    tripped = {"done": False}

    class _RacingLock:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            if not tripped["done"]:
                reg.trip(
                    BreakerKind.COMPOSER_SEND_READINESS,
                    "race trip",
                    cooldown_s=300.0,
                )
                tripped["done"] = True
            return self

        async def __aexit__(self, *a):
            return False

    import chatgpt_web2api.api_server as mod

    monkeypatch.setattr(mod, "CrossProcessLock", _RacingLock)

    request = MagicMock()
    request.json = AsyncMock(
        return_value={
            "messages": [{"role": "user", "content": "hello"}],
            "model": "auto",
            "stream": False,
        }
    )

    resp = await server._handle_chat(request)

    assert resp.status == 503
    body = json.loads(resp.body)
    assert body["error"]["code"] == "circuit_open"
    # Chrome was NOT driven — the post-lock check fired first.
    driver.select_model.assert_not_called()
    driver.navigate_new_chat.assert_not_called()
    driver.navigate_conversation.assert_not_called()


# ── Blocker 2: AUTH_EXPIRED recovery probe ────────────────────────────


@pytest.mark.asyncio
async def test_rest_auth_recovery_probes_then_proceeds(monkeypatch):
    """When AUTH_EXPIRED is open, the preflight probes recover_auth() before
    failing fast. If recovery succeeds (user logged back in), the breaker is
    reset and the request proceeds normally — no 503."""
    import chatgpt_web2api.api_server as srv

    server = srv.APIServer.__new__(srv.APIServer)
    server._last_conv_id = None
    server._last_project_id = None
    server._request_count = 0
    server._cdp_port = 9222
    server._config = srv.Config.load(None)
    server._last_error = None
    server._last_successful_send_at = None
    reg = BreakerRegistry()
    reg.trip(BreakerKind.AUTH_EXPIRED, "401", cooldown_s=0)
    server._breakers = reg

    driver = MagicMock()
    driver._current_conv_id = None
    driver._current_model = None
    # Recovery succeeds — auth is valid again. The mock mimics the real
    # recover_auth() by resetting the breaker before returning True.
    recover_called = {"count": 0}

    async def _recover_ok():
        recover_called["count"] += 1
        reg.reset(BreakerKind.AUTH_EXPIRED)
        return True

    driver.recover_auth = _recover_ok
    driver.select_model = AsyncMock(return_value=True)
    driver.navigate_new_chat = AsyncMock()
    driver.navigate_conversation = AsyncMock()
    driver.ensure_current_conversation = AsyncMock()

    reached = {"sent": False}

    async def _full_response(*a, **kw):
        reached["sent"] = True
        resp = MagicMock()
        resp.status = 200
        return resp

    server._full_response = _full_response
    server._stream_response = _full_response
    server._driver = driver

    # Bypass the cross-process lock.
    class _NullLock:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    import chatgpt_web2api.api_server as mod

    monkeypatch.setattr(mod, "CrossProcessLock", _NullLock)

    request = MagicMock()
    request.json = AsyncMock(
        return_value={
            "messages": [{"role": "user", "content": "hello"}],
            "model": "auto",
            "stream": False,
        }
    )

    await server._handle_chat(request)

    # Recovery was probed and succeeded; the request proceeded.
    assert reached["sent"] is True
    assert recover_called["count"] >= 1
    # Breaker was reset.
    assert reg.is_open(BreakerKind.AUTH_EXPIRED) is False


@pytest.mark.asyncio
async def test_rest_auth_recovery_fails_still_fail_fasts():
    """When AUTH_EXPIRED is open and recovery fails, the preflight still
    fails fast with 503 circuit_open."""
    import chatgpt_web2api.api_server as srv

    server = srv.APIServer.__new__(srv.APIServer)
    server._last_conv_id = None
    server._last_project_id = None
    server._request_count = 0
    server._cdp_port = 9222
    server._config = srv.Config.load(None)
    server._last_error = None
    reg = BreakerRegistry()
    reg.trip(BreakerKind.AUTH_EXPIRED, "401", cooldown_s=0)
    server._breakers = reg

    driver = MagicMock()
    driver._current_conv_id = None
    driver._current_model = None
    # Recovery fails — auth still invalid.
    driver.recover_auth = AsyncMock(return_value=False)
    driver.select_model = AsyncMock(return_value=True)
    driver.navigate_new_chat = AsyncMock()
    server._driver = driver

    request = MagicMock()
    request.json = AsyncMock(
        return_value={
            "messages": [{"role": "user", "content": "hello"}],
            "model": "auto",
            "stream": False,
        }
    )

    resp = await server._handle_chat(request)

    assert resp.status == 503
    body = json.loads(resp.body)
    assert body["error"]["code"] == "circuit_open"
    driver.navigate_new_chat.assert_not_called()


@pytest.mark.asyncio
async def test_driver_recover_auth_resets_on_successful_refresh(monkeypatch):
    """driver.recover_auth() calls _refresh_token and resets AUTH_EXPIRED on
    success; on failure leaves the breaker open."""
    from chatgpt_web2api.backend_client import BackendClient
    from chatgpt_web2api.cdp_driver import CDPDriver

    driver = CDPDriver.__new__(CDPDriver)
    driver._breakers = BreakerRegistry()
    driver._access_token = ""
    # Phase 5 PR1: recover_auth is delegated to BackendClient. Wire it the
    # way CDPDriver.__init__ would (this test bypasses __init__ via __new__).
    driver._backend_client = BackendClient(driver)

    # Success path: _refresh_token returns (non-empty token) → reset.
    driver._breakers.trip(BreakerKind.AUTH_EXPIRED, "401", cooldown_s=0)
    assert driver._breakers.is_open(BreakerKind.AUTH_EXPIRED) is True

    async def _ok_refresh():
        driver._access_token = "newtoken"

    driver._refresh_token = _ok_refresh
    result = await driver.recover_auth()
    assert result is True
    assert driver._breakers.is_open(BreakerKind.AUTH_EXPIRED) is False

    # Failure path: _refresh_token raises → breaker stays open.
    driver._breakers.trip(BreakerKind.AUTH_EXPIRED, "401", cooldown_s=0)

    async def _bad_refresh():
        raise RuntimeError("No access token")

    driver._refresh_token = _bad_refresh
    result = await driver.recover_auth()
    assert result is False
    assert driver._breakers.is_open(BreakerKind.AUTH_EXPIRED) is True
