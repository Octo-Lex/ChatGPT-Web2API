"""Tests: send acknowledgment + diagnostic preservation.

ChatGPT code review (conv 6a52f0f3) found two defects:

1. click_send dispatches events but doesn't verify React accepted the
   submission. If the page is overloaded, the click fires but no message
   is sent. The bridge silently enters completion detection which finds
   nothing → reconciliation failure with no diagnostic.

2. TurnReconciliationError discards the underlying fetch_failed diagnostic.
   The actual error (CDP timeout, destroyed context, HTTP error) is lost.

Fix 1: after click_send + UUID wait, verify at least one acknowledgment:
  - UUID captured, OR
  - user-message DOM count increased AND composer cleared
  If none → raise SendNotAcknowledgedError before entering completion detection.

Fix 2: include last_result.diagnostic in TurnReconciliationError.
"""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from chatgpt_web2api.cdp_driver import CDPDriver, SendReadinessError
from chatgpt_web2api.turn_anchor import TurnReconciliationError, TurnTextResult


def _make_driver():
    """A CDPDriver with mocked transport for testing."""
    driver = CDPDriver(cdp_port=9222)
    driver._cdp = AsyncMock()
    driver._js_strict = AsyncMock(return_value="1")
    driver._js = AsyncMock(return_value="1")
    driver._breakers = None
    driver._current_conv_id = None
    driver._target_id = "test-target"
    driver._owns_target = True
    return driver


# ── 1. Send acknowledgment ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_send_not_acknowledged_raises_when_no_signals(monkeypatch):
    """When click_send fires but no acknowledgment appears (no UUID, no DOM
    count increase, composer not cleared), the bridge must raise a typed error
    instead of silently entering completion detection."""
    from chatgpt_web2api.cdp_driver import CDPDriver

    driver = _make_driver()
    # Mock the send path
    driver.type_message = AsyncMock()
    driver.click_send = AsyncMock()
    driver._read_assistant_count_baseline = AsyncMock(return_value=0)
    driver._identity_listener = None  # no UUID capture possible
    driver._capture_pre_send_fallback_anchor = AsyncMock(return_value=MagicMock())
    driver._assert_owned_tab_required = MagicMock()
    # Set pre-send user count baseline so the delta check works
    driver._pre_send_user_count = 2  # existing conversation with 2 user msgs

    # After send: user count stays 2 (message didn't land), composer NOT cleared
    call_count = {"n": 0}

    async def fake_js_strict(expr, timeout=15):
        call_count["n"] += 1
        # Send acknowledgment probe: count stays at 2 (no increase), composer present but NOT empty
        if "userCount" in expr and "composerEmpty" in expr:
            return json.dumps({"userCount": 2, "composerPresent": True, "composerEmpty": False})
        if "assistant" in expr and "length" in expr:
            return "0"  # no new assistant messages
        if "prompt-textarea" in expr or "ProseMirror" in expr:
            return json.dumps({"text": "the message that should have been sent"})
        return "0"

    driver._js_strict = fake_js_strict

    # The send_and_stream should raise before entering completion detection
    with pytest.raises(Exception) as exc_info:
        async for _ in driver.send_and_stream("test message", timeout=10):
            pass

    # Should be a SendNotAcknowledged-style error, not a timeout
    assert "acknowledge" in str(exc_info.value).lower() or "not acknowledged" in str(exc_info.value).lower(), (
        f"Expected send-acknowledgment error, got: {exc_info.value}"
    )


@pytest.mark.asyncio
async def test_send_acknowledged_when_user_count_increases(monkeypatch):
    """When the user-message count increases after send, the message landed.
    The bridge should proceed to completion detection normally."""
    driver = _make_driver()
    driver.type_message = AsyncMock()
    driver.click_send = AsyncMock()
    driver._read_assistant_count_baseline = AsyncMock(return_value=0)
    driver._identity_listener = None
    driver._assert_owned_tab_required = MagicMock()
    driver._pre_send_user_count = 0  # fresh chat, no prior user messages

    # Mock the anchor + fallback
    from chatgpt_web2api.turn_anchor import TurnAnchor
    anchor = TurnAnchor(sent_text="test", mode="fresh_chat")
    driver._capture_pre_send_fallback_anchor = AsyncMock(return_value=anchor)

    # After send: user count goes from 0 to 1 (message landed)
    poll_count = {"n": 0}

    async def fake_js_strict(expr, timeout=15):
        # Send acknowledgment check: user count + composer present + empty
        if "userCount" in expr and "composerEmpty" in expr:
            return json.dumps({"userCount": 1, "composerPresent": True, "composerEmpty": True})
        if "assistant" in expr and "length" in expr:
            return "1"  # assistant appeared too (completion)
        if "prompt-textarea" in expr or "ProseMirror" in expr:
            return json.dumps({"text": ""})  # composer cleared
        # Phase-2 poll: completed
        if "getBoundingClientRect" in expr:
            return json.dumps({"text": "ok", "md_text": "ok", "html_len": 60,
                              "child_count": 1, "has_action": False, "is_thinking": False})
        return "1"

    driver._js_strict = fake_js_strict

    # Mock the detector to return immediately
    from chatgpt_web2api.completion_detector import CompletionDetector
    driver._completion = MagicMock()
    driver._completion.stream_until_complete = MagicMock()

    async def fake_stream(**kwargs):
        from chatgpt_web2api.cdp_driver import StreamChunk
        yield StreamChunk(delta="ok")
    driver._completion.stream_until_complete = fake_stream
    driver._completion.last_dom_text = "ok"
    driver._completion.had_non_text_content = False

    # Should NOT raise — message was acknowledged
    chunks = []
    async for chunk in driver.send_and_stream("test message", timeout=10):
        chunks.append(chunk)
    assert len(chunks) > 0


# ── 2. Diagnostic preservation in TurnReconciliationError ────────────────


def test_turn_reconciliation_error_preserves_diagnostic():
    """TurnReconciliationError must include the last fetch result's diagnostic
    so operators can distinguish CDP timeout from HTTP error from projection failure.

    The diagnostic must be in BOTH the .diagnostic dict AND the error string
    (ChatGPT review: agents only see str(exc) via the API)."""
    err = TurnReconciliationError(
        conversation_id="conv-123",
        anchor_mode="fresh_chat",
        last_status="fetch_failed",
        diagnostic={
            "captured_id": "uuid-abc",
            "had_non_text_content": False,
            "last_fetch_diagnostic": {"error": "execution context destroyed"},
        },
    )
    msg = str(err)
    diag = err.diagnostic
    # Diagnostic dict must include the underlying fetch error
    assert "last_fetch_diagnostic" in diag, "Diagnostic dict must include the fetch diagnostic"
    # Error string must also include it (agents see str(exc), not .diagnostic)
    assert "execution context destroyed" in msg, (
        f"Error string must include the fetch error, got: {msg}"
    )


# ── 3. Missing-composer regression (ChatGPT review finding C) ────────────


@pytest.mark.asyncio
async def test_missing_composer_returns_none_not_false():
    """When the composer is missing on every poll (navigation, selector drift),
    the acknowledgment probe should return None (inconclusive) not False (blocking).

    ChatGPT found that valid_probe_seen was set BEFORE the composerPresent
    check, so an all-missing-composer run incorrectly returned False."""
    driver = _make_driver()
    driver._pre_send_user_count = 0

    async def fake_js_strict(expr, timeout=15):
        # Every probe returns valid JSON but composer is missing
        if "userCount" in expr and "composerEmpty" in expr:
            return json.dumps({"userCount": 1, "composerPresent": False, "composerEmpty": False})
        return "0"

    driver._js_strict = fake_js_strict

    # The 3s polling window uses real asyncio.sleep, so this test takes ~3s.
    # Acceptable for a regression test that verifies a critical safety path.
    result = await driver._verify_send_acknowledged()

    # Should be None (inconclusive), not False (blocking)
    assert result is None, (
        f"Missing composer should return None (inconclusive), got {result!r}"
    )
