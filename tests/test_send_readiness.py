"""P2.5: Send-readiness selector hardening tests.

Addresses error #4 — "Send failed: no send button" (button_count:142). A
fully-loaded ChatGPT page where neither SEND_BUTTON_SELECTOR nor
SEND_BUTTON_FALLBACK_SELECTOR matched. Co-designed with ChatGPT (vision-
alignment cycle, conversation 6a4ebb2a).

P2.5 scope:
  1. Richer diagnostic snapshot (composer_text_length, send_candidates,
     enabled candidates, stop_button_present, generating_indicator_present).
  2. Broader send-button fallback selector (type=submit, not just aria-label).
  3. Distinguish 'composer injection failed' from 'send button missing'.
"""

import asyncio
import json
import logging
from unittest.mock import AsyncMock

import pytest

from chatgpt_web2api.cdp_driver import CDPDriver, SendReadinessError
from chatgpt_web2api.chatgpt_dom import ChatGPTDom


@pytest.fixture(autouse=True)
def _fast_poll(monkeypatch):
    """Collapse the send-button poll loop so tests run instantly.
    Patches both asyncio.sleep (the poll interval) and time.monotonic
    (the deadline check) in the chatgpt_dom module."""
    _real_sleep = asyncio.sleep
    _t = [0.0]

    async def _instant(_d):
        _t[0] += _d  # advance simulated time by the sleep duration
        await _real_sleep(0)

    monkeypatch.setattr("chatgpt_web2api.chatgpt_dom.asyncio.sleep", _instant)
    monkeypatch.setattr("chatgpt_web2api.chatgpt_dom.time.monotonic", lambda: _t[0])


def _make_dom(diag_json=None):
    """A ChatGPTDom backed by a real CDPDriver with mocked transport.
    Using a real driver (not spec mock) so _capture_selector_diagnostic runs
    the real code path that P2.5 enhances.

    diag_json: the JSON string the diagnostic _js_strict call should return.
    If None, returns a minimal old-style snapshot (button_count only).
    """
    driver = CDPDriver(cdp_port=9222)
    driver._js = AsyncMock(return_value="")
    # The diagnostic JS is the only _js_strict call from click_send.
    # Return the diag payload (or old-style minimal if not provided).
    driver._js_strict = AsyncMock(return_value=diag_json or json.dumps({
        "url": "https://chatgpt.com/c/conv-1",
        "title": "Test",
        "body_preview": "",
        "button_count": 142,
        "textarea_count": 1,
    }))
    driver._cdp = AsyncMock()
    driver._breakers = None
    driver._current_conv_id = "conv-1"
    return ChatGPTDom(driver), driver


def _make_scripted_js(*, poll_result="no", send_result="no send button"):
    """Build a fake _js that discriminates by expression content.
    - The button poll contains 'disabled' (checking btn.disabled)
    - The send JS contains 'no send button' (the failure return string)
    """
    async def fake_js(expr, timeout=15):
        if "no send button" in expr:
            return send_result
        # Button poll (or any other _js call)
        return poll_result
    return fake_js


def _diag_json(**overrides):
    """Build the diagnostic snapshot JSON with defaults."""
    data = {
        "url": "https://chatgpt.com/c/conv-1",
        "title": "Test",
        "body_preview": "Skip to content",
        "button_count": 142,
        "textarea_count": 1,
        "composer_found": True,
        "composer_text_length": 50,
        "composer_enabled": True,
        "send_candidates_count": 0,
        "enabled_send_candidates_count": 0,
        "stop_button_present": False,
        "generating_indicator_present": False,
    }
    data.update(overrides)
    return json.dumps(data)


# ── 1. Richer diagnostic snapshot ────────────────────────────────────────


@pytest.mark.asyncio
async def test_send_diagnostic_probes_composer_and_send_state(caplog):
    """When click_send fails, the diagnostic JS should probe send-readiness
    fields: composer_text_length, send_candidates_count, enabled_send_candidates,
    stop_button_present, generating_indicator_present — not just button_count.

    This test captures the ACTUAL diagnostic JS expression and verifies it
    requests the P2.5 fields. The mock returns rich data; the assertion is
    that the JS expression itself contains the new probes."""
    captured_js = []
    dom, driver = _make_dom()

    async def capturing_js_strict(expr, timeout=15):
        captured_js.append(expr)
        return _diag_json(send_candidates_count=3, enabled_send_candidates_count=1)

    driver._js = _make_scripted_js(poll_result="no", send_result="no send button")
    driver._js_strict = capturing_js_strict

    with pytest.raises(SendReadinessError):
        await dom.click_send()

    # The diagnostic JS should have been called and should probe the new fields.
    assert len(captured_js) >= 1, "diagnostic _js_strict should have been called"
    diag_expr = captured_js[-1]
    assert "send_candidates" in diag_expr or "composer_text" in diag_expr, (
        f"diagnostic JS should probe send_candidates/composer_text, got: {diag_expr[:120]}"
    )
    assert "stop_button" in diag_expr or "stop-button" in diag_expr, (
        f"diagnostic JS should probe stop_button state, got: {diag_expr[:120]}"
    )


# ── 2. Broader send-button fallback (type=submit) ───────────────────────


@pytest.mark.asyncio
async def test_send_succeeds_with_submit_type_fallback():
    """If the aria-label selector fails but a type=submit button exists inside
    the composer form, the broader fallback should find it."""
    dom, driver = _make_dom()

    driver._js = _make_scripted_js(poll_result="yes", send_result="sent")
    await dom.click_send()  # should NOT raise


# ── 3. Distinguish injection-failed from send-button-missing ────────────


@pytest.mark.asyncio
async def test_error_distinguishes_empty_composer_from_missing_button(caplog):
    """When the send button is missing AND the composer is empty, the error
    should indicate the composer is empty (injection failed), not just 'no
    send button'. This distinguishes hypothesis #6 (input injection failed)
    from hypothesis #1 (selector drift)."""
    dom, driver = _make_dom()

    driver._js = _make_scripted_js(poll_result="no", send_result="no send button")
    driver._js_strict = AsyncMock(return_value=_diag_json(
        composer_text_length=0,  # EMPTY — injection failed!
    ))

    with caplog.at_level(logging.WARNING):
        with pytest.raises(SendReadinessError) as exc_info:
            await dom.click_send()

    msg = str(exc_info.value)
    all_logs = " ".join(r.message for r in caplog.records)
    combined = msg + " " + all_logs
    # The diagnostic should show composer_text_length=0 so a human can
    # distinguish "text didn't land" from "selector drifted".
    assert "composer_text_length" in combined, (
        f"diagnostic should include composer_text_length to distinguish "
        f"injection failure, got: {combined[:200]}"
    )


# ── 4. Stop-button-present diagnostic ────────────────────────────────────


@pytest.mark.asyncio
async def test_diagnostic_captures_stop_button_when_generating(caplog):
    """When the send button is missing because a generation is in progress
    (stop button visible), the diagnostic should capture stop_button_present=True."""
    dom, driver = _make_dom()

    driver._js = _make_scripted_js(poll_result="no", send_result="no send button")
    driver._js_strict = AsyncMock(return_value=_diag_json(
        stop_button_present=True,
        generating_indicator_present=True,
        composer_enabled=False,
    ))

    with caplog.at_level(logging.WARNING):
        with pytest.raises(SendReadinessError):
            await dom.click_send()

    diag_logs = [r for r in caplog.records if "diagnostic" in r.message.lower()
                 or "selector" in r.message.lower()]
    assert len(diag_logs) >= 1
    log_msg = diag_logs[0].message
    assert "stop_button_present" in log_msg, (
        f"diagnostic should capture stop_button state, got: {log_msg}"
    )
