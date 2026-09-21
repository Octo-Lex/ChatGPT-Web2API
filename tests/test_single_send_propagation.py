"""#51 propagation tests — single_send reaches the retry seam on both servers.

The wrapper-level behavior (max_attempts=1 → one invocation) is covered in
test_no_replay_send.py. These tests pin the SEAMS:

  - resilience.chat_retry_attempts maps arguments → attempts
  - api_server._resolve_single_send maps the REST body (top level or
    metadata) → the single_send flag
  - the wrapper + helper combination keeps default callers at 3 attempts
    and single-send callers at exactly one business invocation
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from chatgpt_web2api.api_server import _resolve_single_send
from chatgpt_web2api.cdp_driver import RateLimitError
from chatgpt_web2api.resilience import chat_retry_attempts, retry_on_rate_limit

# ── resilience helper ────────────────────────────────────────────────

def test_chat_retry_attempts_default_is_three():
    assert chat_retry_attempts({}) == 3
    assert chat_retry_attempts({"single_send": False}) == 3
    assert chat_retry_attempts(None) == 3


def test_chat_retry_attempts_single_send_is_one():
    assert chat_retry_attempts({"single_send": True}) == 1


# ── REST seam ────────────────────────────────────────────────────────

def test_resolve_single_send_from_top_level():
    assert _resolve_single_send({"single_send": True, "messages": []}) is True


def test_resolve_single_send_from_metadata():
    assert _resolve_single_send(
        {"metadata": {"single_send": True, "project_id": "g-p-x"}}) is True


def test_resolve_single_send_absent_is_false():
    assert _resolve_single_send({"messages": []}) is False
    assert _resolve_single_send({"metadata": {}}) is False


# ── seam → wrapper combination (REST shape) ──────────────────────────

async def test_rest_single_send_shape_runs_once_on_post_mutation_429():
    calls = {"mutations": 0}
    driver = MagicMock()
    driver.dismiss_rate_limit = AsyncMock(return_value=True)

    async def send_and_collect():
        calls["mutations"] += 1  # the USER send was dispatched
        raise RateLimitError("rate limited", retry_after=0.01)

    body = {"messages": [{"role": "user", "content": "x"}],
            "metadata": {"single_send": True}}
    with pytest.raises(RateLimitError):
        await retry_on_rate_limit(
            driver, send_and_collect,
            max_attempts=chat_retry_attempts(
                {"single_send": _resolve_single_send(body)}),
        )

    assert calls["mutations"] == 1


# ── seam → wrapper combination (MCP shape) ───────────────────────────

async def test_mcp_single_send_argument_runs_once_on_post_mutation_429():
    calls = {"mutations": 0}
    driver = MagicMock()
    driver.dismiss_rate_limit = AsyncMock(return_value=True)

    async def chat_handler():
        calls["mutations"] += 1
        raise RateLimitError("rate limited", retry_after=0.01)

    arguments = {"message": "x", "single_send": True}
    with pytest.raises(RateLimitError):
        await retry_on_rate_limit(
            driver, chat_handler,
            max_attempts=chat_retry_attempts(arguments),
        )

    assert calls["mutations"] == 1


async def test_mcp_default_argument_still_retries():
    calls = {"attempts": 0}
    driver = MagicMock()
    driver.dismiss_rate_limit = AsyncMock(return_value=True)

    async def chat_handler():
        calls["attempts"] += 1
        if calls["attempts"] == 1:
            raise RateLimitError("rate limited", retry_after=0.01)
        return {"ok": True}

    arguments = {"message": "x"}  # no opt-in → default behavior
    result = await retry_on_rate_limit(
        driver, chat_handler, max_attempts=chat_retry_attempts(arguments))

    assert result == {"ok": True}
    assert calls["attempts"] == 2


# ── Streaming path: the mutating generator is consumed exactly once ──
# (GitWire asserted streaming might wrap the send in retry_on_rate_limit.
# It does not: _stream_response retries only the read-only rate-limit
# preflight, then consumes send_and_stream once. Pinned behaviorally.)

def _stream_test_server(driver):
    from chatgpt_web2api.api_server import APIServer
    from chatgpt_web2api.breakers import BreakerRegistry
    from chatgpt_web2api.config import Config

    server = APIServer.__new__(APIServer)
    server._config = Config.load(None)
    server._breakers = BreakerRegistry()
    server._last_conv_id = None
    server._last_project_id = None
    server._last_successful_send_at = None
    server._driver = driver
    driver._js_strict = AsyncMock(return_value='{"text":"normal text"}')
    return server


async def test_stream_response_consumes_send_and_stream_exactly_once():
    from aiohttp.test_utils import make_mocked_request

    from chatgpt_web2api.cdp_driver import StreamChunk

    driver = MagicMock()
    driver._current_conv_id = "conv-stream-1"
    sends = {"n": 0}

    async def send_and_stream(text, timeout=120, *, budgets=None, model=None):
        sends["n"] += 1
        yield StreamChunk(delta="hello")
        yield StreamChunk(delta="", finish_reason="stop")

    driver.send_and_stream = send_and_stream
    server = _stream_test_server(driver)

    request = make_mocked_request("POST", "/v1/chat/completions")
    resp = await server._stream_response(request, "auto", "hi", 30)

    assert sends["n"] == 1
    assert resp.status == 200


async def test_stream_response_rate_limit_midstream_does_not_restart_send():
    """The GitWire-disproving property: a RateLimitError surfacing AFTER
    the mutating generator started must not re-invoke it — streaming has
    no outer retry around the send (the inline SSE marker is the channel)."""
    from aiohttp.test_utils import make_mocked_request

    from chatgpt_web2api.cdp_driver import RateLimitError as RL
    from chatgpt_web2api.cdp_driver import StreamChunk

    driver = MagicMock()
    driver._current_conv_id = "conv-stream-2"
    sends = {"n": 0}

    async def send_and_stream(text, timeout=120, *, budgets=None, model=None):
        sends["n"] += 1
        yield StreamChunk(delta="partial")
        raise RL("rate limited", retry_after=30)  # mid-stream throttle

    driver.send_and_stream = send_and_stream
    server = _stream_test_server(driver)

    request = make_mocked_request("POST", "/v1/chat/completions")
    resp = await server._stream_response(request, "auto", "hi", 30)

    assert sends["n"] == 1  # NOT restarted — one business-send invocation
    assert resp.status == 200  # SSE committed; the marker chunk is the channel
