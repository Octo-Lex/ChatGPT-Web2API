"""Reliability tests — auth-expiry fix, stall detectors, cap removal.

Covers bugs #3 (silent auth expiry) and #1 (60s appear-cap + stuck
generation) per the implementation plan. All tests are unit-level with
mocked CDP — no live Chrome needed.
"""

import asyncio
import json
import time
import pytest
from unittest.mock import AsyncMock, MagicMock

from chatgpt_web2api.cdp_driver import (
    AuthExpiredError,
    CDPDriver,
    GenerationStuckError,
    PHASE_STALL_SECONDS,
    TOKEN_TTL_SECONDS,
)


# ── Helpers ────────────────────────────────────────────────────

def _make_driver():
    """A CDPDriver with a mocked websocket (no real connect)."""
    d = CDPDriver(cdp_port=9222)
    d._ws = MagicMock()  # truthy; is_connected will treat as open
    d._access_token = "fresh-token"
    d._token_fetched_at = time.time()
    return d


def _mock_js_with_payload(d, payload_map):
    """Make d._js_with_data return values based on a lookup of (conv_id/token)
    → response string. For _fetch_text the payload carries conv_id+token."""
    async def _fake(js_template, data, timeout=15):
        return payload_map.get(data.get("conv_id"), "")
    d._js_with_data_strict = _fake
    return d


# ── 1. Auth TTL ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_ensure_token_refreshes_when_empty():
    d = _make_driver()
    d._access_token = ""
    d._refresh_token = AsyncMock()
    await d.ensure_token()
    d._refresh_token.assert_awaited_once()


@pytest.mark.asyncio
async def test_ensure_token_refreshes_when_stale():
    d = _make_driver()
    d._token_fetched_at = time.time() - (TOKEN_TTL_SECONDS + 10)  # older than TTL
    d._refresh_token = AsyncMock()
    await d.ensure_token()
    d._refresh_token.assert_awaited_once()


@pytest.mark.asyncio
async def test_ensure_token_no_refresh_when_fresh():
    d = _make_driver()
    d._token_fetched_at = time.time()  # fresh
    d._refresh_token = AsyncMock()
    await d.ensure_token()
    d._refresh_token.assert_not_awaited()


# ── 2. 15-site guard ───────────────────────────────────────────

@pytest.mark.asyncio
async def test_read_methods_call_ensure_token_first():
    """A representative read method (get_models) calls ensure_token before its
    fetch. Verify via mock call-order."""
    d = _make_driver()
    d._refresh_token = AsyncMock()
    # _js_with_data returns a models JSON string
    async def _fake(js_template, data, timeout=15):
        return json.dumps({"title": "Models", "models": [{"slug": "auto", "title": "Auto"}]})
    d._js_with_data_strict = _fake
    # Patch ensure_token to record the call distinctly
    call_order = []
    async def _record_token():
        call_order.append("ensure_token")
        return d._access_token
    d.ensure_token = _record_token
    async def _rec_js(template, data, timeout=15):
        call_order.append("fetch")
        return json.dumps({"title": "M", "models": [{"slug": "auto", "title": "A"}]})
    d._js_with_data_strict = _rec_js
    await d.get_models()
    assert call_order == ["ensure_token", "fetch"], f"order: {call_order}"


# ── 3. _fetch_text 401 raises AuthExpiredError ─────────────────

@pytest.mark.asyncio
async def test_fetch_text_401_raises_auth_expired():
    d = _make_driver()
    async def _fake(js_template, data, timeout=15):
        return '{"__status": 401}'
    d._js_with_data_strict = _fake
    d.ensure_token = AsyncMock(return_value="tok")
    with pytest.raises(AuthExpiredError):
        await d._fetch_text("conv-1")


@pytest.mark.asyncio
async def test_fetch_text_404_raises_runtime_error():
    """Non-401 non-OK status surfaces as RuntimeError with the code, not silent ''."""
    d = _make_driver()
    async def _fake(js_template, data, timeout=15):
        return '{"__status": 404}'
    d._js_with_data_strict = _fake
    d.ensure_token = AsyncMock(return_value="tok")
    with pytest.raises(RuntimeError) as ei:
        await d._fetch_text("conv-1")
    assert "404" in str(ei.value)


@pytest.mark.asyncio
async def test_fetch_text_valid_body_returns_text():
    d = _make_driver()
    async def _fake(js_template, data, timeout=15):
        return "the assistant reply text"
    d._js_with_data_strict = _fake
    d.ensure_token = AsyncMock(return_value="tok")
    result = await d._fetch_text("conv-1")
    assert result == "the assistant reply text"


# ── 4. Phase-1 stall (node count never changes) ────────────────

@pytest.mark.asyncio
async def test_phase1_stall_raises_generation_stuck(monkeypatch):
    """If the assistant node count never changes for >PHASE_STALL_SECONDS,
    raise GenerationStuckError('phase_1_appear', ...) rather than waiting
    the full timeout."""
    d = _make_driver()
    # Mock time to accelerate the test: advance monotonic fast.
    t = [0.0]
    def fake_monotonic():
        return t[0]
    monkeypatch.setattr("chatgpt_web2api.cdp_driver.time.monotonic", fake_monotonic)
    async def fast_sleep(s):
        t[0] += s  # each sleep advances "time" by the sleep amount
    monkeypatch.setattr("chatgpt_web2api.cdp_driver.asyncio.sleep", fast_sleep)

    # _js returns: rate-limit scan = harmless text; assistant count = constant 1
    call = {"n": 0}
    async def _fake_js(expr, timeout=15):
        call["n"] += 1
        # Count poll is the bare .length expression (no JSON.stringify)
        if "JSON.stringify" not in expr and ".length" in expr:
            return "1"  # constant count, never > initial_count
        return '{"text":"normal page text"}'
    d._js_strict = _fake_js
    d.type_message = AsyncMock()
    d.click_send = AsyncMock()

    with pytest.raises(GenerationStuckError) as ei:
        async for _ in d.send_and_stream("hi", timeout=10000):
            pass
    assert ei.value.phase == "phase_1_appear"


# ── 5. Phase-2 stall (text never changes, Stop present) ────────

@pytest.mark.asyncio
async def test_phase2_stall_raises_generation_stuck(monkeypatch):
    """Phase 2: text unchanging + Stop button present for >stall window → raise."""
    d = _make_driver()
    t = [0.0]
    monkeypatch.setattr("chatgpt_web2api.cdp_driver.time.monotonic", lambda: t[0])
    async def fast_sleep(s):
        t[0] += s
    monkeypatch.setattr("chatgpt_web2api.cdp_driver.asyncio.sleep", fast_sleep)

    # Track call sequence rather than expression-string matching (more robust).
    # Distinguish polls by unique substrings: the count poll is a bare expression
    # ending in `.length` (no JSON.stringify); the Phase-2 poll contains
    # `JSON.stringify`; the rate-limit scan contains `body.innerText`.
    state = {"count_polls": 0, "in_phase2": False}
    async def _fake_js(expr, timeout=15):
        if "JSON.stringify" in expr and "Stop" in expr:
            # Phase-2 poll
            state["in_phase2"] = True
            return json.dumps({"text": "partial", "done": False})
        if "body.innerText" in expr:
            # Rate-limit scan (Phase 1)
            return json.dumps({"text": "normal page"})
        # Count poll (bare .length expression)
        state["count_polls"] += 1
        return "1" if state["count_polls"] > 1 else "0"
    d._js_strict = _fake_js
    d.type_message = AsyncMock()
    d.click_send = AsyncMock()
    d._fetch_text = AsyncMock(return_value="")

    with pytest.raises(GenerationStuckError) as ei:
        async for _ in d.send_and_stream("hi", timeout=10000):
            pass
    assert ei.value.phase == "phase_2_stream"


# ── 6. Cap removal: slow appear succeeds ───────────────────────

@pytest.mark.asyncio
async def test_slow_appear_succeeds_without_cap(monkeypatch):
    """A response whose assistant node appears at t=70s succeeds, PROVIDED there
    is progress beforehand (count changes) so the stall clock keeps resetting.
    Before the cap removal this raised RuntimeError at t=60s regardless. The
    stall detector correctly fires only on NO progress — a slow render that
    shows intermittent node changes (e.g. loading placeholders) is allowed."""
    d = _make_driver()
    t = [0.0]
    monkeypatch.setattr("chatgpt_web2api.cdp_driver.time.monotonic", lambda: t[0])
    async def fast_sleep(s):
        t[0] += s
    monkeypatch.setattr("chatgpt_web2api.cdp_driver.asyncio.sleep", fast_sleep)

    # Count wobbles 0→1→0→1... every ~10 polls so the stall clock (45s) keeps
    # resetting, then settles at 2 (>initial) at poll 150 (~75s) to break Phase 1.
    count_polls = {"n": 0}
    async def _fake_js(expr, timeout=15):
        if "JSON.stringify" in expr and "Stop" in expr:
            return json.dumps({"text": "done", "done": True})
        if "body.innerText" in expr:
            return json.dumps({"text": "normal"})
        count_polls["n"] += 1
        n = count_polls["n"]
        if n > 150:
            return "2"  # appear + break at ~75s
        return "1" if (n // 10) % 2 == 0 else "0"  # wobble 0/1 — progress signal
    d._js_strict = _fake_js
    d.type_message = AsyncMock()
    d.click_send = AsyncMock()
    d._fetch_text = AsyncMock(return_value="done")

    chunks = []
    async for chunk in d.send_and_stream("hi", timeout=10000):
        chunks.append(chunk)
    assert any(c.delta for c in chunks)
    assert chunks[-1].finish_reason == "stop"


# ── 7. Progressing generation does NOT raise ───────────────────

@pytest.mark.asyncio
async def test_progressing_generation_does_not_raise(monkeypatch):
    """A generation that keeps making progress (text changing) does NOT raise
    GenerationStuckError even after the stall window would have fired. Locks
    in progress-sensitivity, not wall-clock-sensitivity."""
    d = _make_driver()
    t = [0.0]
    monkeypatch.setattr("chatgpt_web2api.cdp_driver.time.monotonic", lambda: t[0])
    async def fast_sleep(s):
        t[0] += s
    monkeypatch.setattr("chatgpt_web2api.cdp_driver.asyncio.sleep", fast_sleep)

    state = {"phase1_polls": 0, "phase2_polls": 0, "text": ""}
    async def _fake_js(expr, timeout=15):
        if "JSON.stringify" in expr and "Stop" in expr:
            state["phase2_polls"] += 1
            state["text"] += "x"
            done = state["phase2_polls"] > 200  # ~100s of progress
            return json.dumps({"text": state["text"], "done": done})
        if "body.innerText" in expr:
            return json.dumps({"text": "normal"})
        state["phase1_polls"] += 1
        return "1" if state["phase1_polls"] > 1 else "0"
    d._js_strict = _fake_js
    d.type_message = AsyncMock()
    d.click_send = AsyncMock()
    d._fetch_text = AsyncMock(return_value=state["text"])

    chunks = []
    async for chunk in d.send_and_stream("hi", timeout=100000):
        chunks.append(chunk)
    assert chunks[-1].finish_reason == "stop"


# ── 8. MCP mapping ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_mcp_auth_expired_returns_error_result():
    """do_chat_completion raises AuthExpiredError when send_and_stream raises it.
    The call_tool handler catches it → CallToolResult(isError=True); we verify
    the do_ function propagates the exception (the catch lives in call_tool's
    closure, tested via the exception class contract)."""
    from chatgpt_web2api import mcp_server

    drv = MagicMock(spec=CDPDriver)
    drv._current_conv_id = None
    drv._current_model = None
    drv.is_connected = True
    drv.select_model = AsyncMock(return_value=True)
    drv.navigate_new_chat = AsyncMock()
    # send_and_stream must be an async GENERATOR that raises on iteration.
    async def _raising_stream(text, timeout=120):
        raise AuthExpiredError()
        yield  # unreachable, makes this a generator
    drv.send_and_stream = _raising_stream

    with pytest.raises(AuthExpiredError):
        await mcp_server.do_chat_completion(drv, {"message": "hi"}, None)


# ── 9. HTTP mapping ────────────────────────────────────────────

def test_http_error_response_auth_expired_is_401():
    from chatgpt_web2api.api_server import APIServer
    from unittest.mock import MagicMock
    srv = APIServer.__new__(APIServer)  # bypass __init__
    srv._driver = MagicMock()
    resp = srv._error_response(AuthExpiredError())
    assert resp.status == 401


def test_http_error_response_generation_stuck_is_504():
    from chatgpt_web2api.api_server import APIServer
    from unittest.mock import MagicMock
    srv = APIServer.__new__(APIServer)
    srv._driver = MagicMock()
    resp = srv._error_response(GenerationStuckError("phase_2_stream", 47.3))
    assert resp.status == 504
