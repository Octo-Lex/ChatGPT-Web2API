"""Tests for the REST API's rate-limit response contract.

When the driver raises RateLimitError and the transparent-retry wrapper has
exhausted its attempts, the API must surface a STANDARD OpenAI 429 so that
any OpenAI-aware agent framework (SDK, LangChain, LlamaIndex) automatically
backs off — with zero client-side integration.

This tests the error-mapping seam directly (APIServer._error_response),
which both the streaming and non-streaming paths use.
"""

from aiohttp import web

from chatgpt_web2api.api_server import APIServer
from chatgpt_web2api.cdp_driver import RateLimitError


def _server():
    """An APIServer with a throwaway config + driver (only _error_response used)."""
    from unittest.mock import MagicMock

    from chatgpt_web2api.config import Config
    return APIServer(Config.load(None), MagicMock())


# ── RateLimitError -> standard OpenAI 429 ──────────────────────

def test_rate_limit_error_maps_to_429():
    """A RateLimitError produces HTTP 429 with the OpenAI rate-limit body."""
    server = _server()
    resp = server._error_response(RateLimitError(retry_after=120))

    assert resp.status == 429
    body = _body(resp)
    err = body["error"]
    # OpenAI's canonical contract that frameworks key on:
    assert err["type"] == "rate_limit_exceeded"
    assert err["code"] == "rate_limit_exceeded"
    # Retry-After header is set so SDKs honor it
    assert resp.headers.get("Retry-After") == "120"
    # Message is preserved and human-readable
    assert "rate limit" in err["message"].lower()


def test_rate_limit_error_uses_default_retry_after_when_absent():
    """A RateLimitError without an explicit retry_after still reports a header."""
    server = _server()
    resp = server._error_response(RateLimitError())
    assert resp.status == 429
    assert "Retry-After" in resp.headers
    # default is 60
    assert int(resp.headers["Retry-After"]) >= 30


# ── other errors stay 500 (not falsely retriable) ─────────────

def test_generic_runtime_error_maps_to_500():
    """A plain RuntimeError stays a 500 server_error (NOT a retriable 429)."""
    server = _server()
    resp = server._error_response(RuntimeError("chrome crashed"))
    assert resp.status == 500
    body = _body(resp)
    assert body["error"]["type"] == "server_error"
    assert "Retry-After" not in resp.headers


def test_timeout_error_maps_to_500():
    """TimeoutError is a server-side failure, not a rate limit."""
    server = _server()
    resp = server._error_response(TimeoutError("timed out"))
    assert resp.status == 500


def _body(resp: web.Response) -> dict:
    """Extract the JSON body from a prepared aiohttp Response."""
    import json
    # aiohttp Response.text is a coroutine in a running loop; for tests we
    # read the raw body that json_response serializes eagerly.
    return json.loads(resp.body)
