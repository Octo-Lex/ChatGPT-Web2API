"""Tests for CDPDriver.dismiss_rate_limit().

The method dismisses ChatGPT's "Too many requests" pop-up by clicking its
'Got it' button, then re-scans to confirm the pop-up cleared. It is
best-effort: it never raises, returning True on success and False otherwise.

The unit tests verify the DOM-interaction contract with a mocked _js; the
live behavior is covered by an E2E test.
"""

import json

import pytest

from chatgpt_web2api.cdp_driver import CDPDriver


def _driver_with_js_sequence(js_returns: list[str]):
    """Build a CDPDriver whose _js_strict returns the given sequence.

    Each call to _js_strict pops the next canned return; once exhausted it
    returns 'OK'. This lets us script the click + the post-click re-scan.
    """
    driver = CDPDriver(cdp_port=9222)
    seq = list(js_returns)

    async def fake_js(expr, timeout=15):
        if seq:
            return seq.pop(0)
        return "OK"

    driver._js_strict = fake_js
    return driver


# ── dismiss_rate_limit contract ───────────────────────────────

@pytest.mark.asyncio
async def test_dismiss_returns_true_when_popup_clears():
    """A successful dismiss: click registers, then re-scan shows no rate limit.

    Sequence of _js calls: [click result, post-click scan].
    Post-click scan body text has NO rate-limit phrase → cleared → True.
    """
    driver = _driver_with_js_sequence([
        json.dumps({"clicked": True}),                       # the click
        json.dumps({"text": "Normal page content, no popup"}),  # post-click scan
    ])

    result = await driver.dismiss_rate_limit()

    assert result is True


@pytest.mark.asyncio
async def test_dismiss_returns_false_when_popup_persists():
    """If the pop-up is still present after the click, return False."""
    driver = _driver_with_js_sequence([
        json.dumps({"clicked": True}),
        json.dumps({"text": "Too many requests. Please wait a few minutes."}),
    ])

    result = await driver.dismiss_rate_limit()

    assert result is False


@pytest.mark.asyncio
async def test_dismiss_returns_false_when_no_got_it_button():
    """If the 'Got it' button isn't found, no click happens; return False."""
    driver = _driver_with_js_sequence([
        json.dumps({"clicked": False}),                      # button not found
        json.dumps({"text": "Too many requests..."}),         # still limited
    ])

    result = await driver.dismiss_rate_limit()

    assert result is False


@pytest.mark.asyncio
async def test_dismiss_never_raises_on_js_error():
    """A JS error during dismiss must not propagate — best-effort contract.
    Returns None (unknown status) per #19 tri-state: not False, to avoid
    triggering a retry storm against an already-dismissed pop-up."""
    driver = CDPDriver(cdp_port=9222)

    async def failing_js(expr, timeout=15):
        raise RuntimeError("websocket closed")

    driver._js_strict = failing_js

    # Should swallow and return None (not raise, not False).
    result = await driver.dismiss_rate_limit()
    assert result is None
