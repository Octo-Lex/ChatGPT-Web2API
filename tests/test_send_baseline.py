"""Tests for `_read_assistant_count_baseline` — the pre-send count fix (PR A1).

The old code fell back to `initial_count = 0` on any JS failure, which made the
completion detector treat pre-existing assistant messages as "new" → stale-return
(the bridge returned the previous turn's text). This fix retries the count with
bounded attempts, logs structured diagnostics, and raises `SendReadinessError`
if it can't establish a trusted baseline.

Tests:
- Baseline succeeds on first try → returns the count.
- Baseline fails once then succeeds on retry → returns the count.
- Baseline fails all retries → raises SendReadinessError (never returns 0).
- The returned count is never 0 when the DOM actually has assistants.
"""

from unittest.mock import AsyncMock

import pytest

from chatgpt_web2api.cdp_driver import CDPDriver, CDPJSError, SendReadinessError


def _make_driver() -> CDPDriver:
    """A minimal CDPDriver with just enough state for the baseline helper."""
    d = CDPDriver.__new__(CDPDriver)
    d._js_strict = AsyncMock()
    d._current_conv_id = None
    return d


@pytest.mark.asyncio
async def test_baseline_succeeds_first_try(monkeypatch):
    """Normal path: _js_strict returns a valid count on the first attempt."""
    d = _make_driver()
    d._js_strict = AsyncMock(side_effect=["3", "5"])  # assistant=3, user=5

    # Patch asyncio.sleep so retries don't actually wait
    import chatgpt_web2api.cdp_driver as drv_mod
    monkeypatch.setattr(drv_mod.asyncio, "sleep", AsyncMock())

    count = await d._read_assistant_count_baseline()
    assert count == 3


@pytest.mark.asyncio
async def test_baseline_retries_then_succeeds(monkeypatch):
    """First attempt fails (CDPJSError), second succeeds → returns count."""
    d = _make_driver()
    d._js_strict = AsyncMock(
        side_effect=[CDPJSError("timeout"), "2", "1"]  # fail, then assistant=2, user=1
    )

    import chatgpt_web2api.cdp_driver as drv_mod
    monkeypatch.setattr(drv_mod.asyncio, "sleep", AsyncMock())

    count = await d._read_assistant_count_baseline()
    assert count == 2


@pytest.mark.asyncio
async def test_baseline_fail_closed_after_retries(monkeypatch):
    """All 3 attempts fail → raises SendReadinessError, never returns 0."""
    d = _make_driver()
    d._js_strict = AsyncMock(
        side_effect=[
            CDPJSError("err1"),
            CDPJSError("err2"),
            CDPJSError("err3"),
        ]
    )

    import chatgpt_web2api.cdp_driver as drv_mod
    monkeypatch.setattr(drv_mod.asyncio, "sleep", AsyncMock())

    with pytest.raises(SendReadinessError, match="Cannot establish pre-send"):
        await d._read_assistant_count_baseline()

    # Confirm it tried 3 times (not 1, not unlimited)
    assert d._js_strict.await_count == 3


@pytest.mark.asyncio
async def test_baseline_never_returns_zero_on_failure(monkeypatch):
    """The CRITICAL invariant: a JS failure must NEVER produce count=0, because
    count=0 on an existing conversation causes stale-return (the detector treats
    pre-existing assistant nodes as 'new'). This test is the regression guard."""
    d = _make_driver()
    d._js_strict = AsyncMock(side_effect=CDPJSError("DOM not ready"))

    import chatgpt_web2api.cdp_driver as drv_mod
    monkeypatch.setattr(drv_mod.asyncio, "sleep", AsyncMock())

    # Must raise, not silently return 0
    with pytest.raises(SendReadinessError):
        await d._read_assistant_count_baseline()

    # If we somehow got here, the baseline must NOT be 0 (this line never
    # executes because of the raise, but documents the intent)
    assert True  # the raise above is the assertion


@pytest.mark.asyncio
async def test_baseline_logs_diagnostics(monkeypatch, caplog):
    """The structured log line includes attempt, counts, elapsed_ms, conv_id."""
    import logging

    d = _make_driver()
    d._js_strict = AsyncMock(side_effect=["7", "4"])  # assistant=7, user=4
    d._current_conv_id = "test-conv-123"

    import chatgpt_web2api.cdp_driver as drv_mod
    monkeypatch.setattr(drv_mod.asyncio, "sleep", AsyncMock())

    with caplog.at_level(logging.INFO, logger="chatgpt_web2api.cdp_driver"):
        count = await d._read_assistant_count_baseline()

    assert count == 7
    # The log should include the diagnostic fields
    log_text = " ".join(r.message for r in caplog.records)
    assert "send_baseline:" in log_text
    assert "assistant_count=7" in log_text
    assert "user_count=4" in log_text
    assert "conv_id=test-conv-123" in log_text


@pytest.mark.asyncio
async def test_baseline_accepts_numeric_zero_when_dom_is_empty(monkeypatch):
    """If the DOM genuinely has 0 assistant messages (fresh chat), count=0 is
    correct and must be accepted — it's not a failure, just a new conversation.
    CDP returns numeric 0, not string '0' — ChatGPT's review caught that the
    truthiness check rejected numeric 0."""
    d = _make_driver()
    d._js_strict = AsyncMock(side_effect=[0, 0])  # numeric 0, not string

    import chatgpt_web2api.cdp_driver as drv_mod
    monkeypatch.setattr(drv_mod.asyncio, "sleep", AsyncMock())

    count = await d._read_assistant_count_baseline()
    assert count == 0  # Correct: truly empty conversation


@pytest.mark.asyncio
async def test_baseline_malformed_string_retries_then_fails(monkeypatch):
    """If _js_strict returns a non-integer string, the parse fails and retries.
    After exhausting retries, raises SendReadinessError (not raw ValueError)."""
    d = _make_driver()
    d._js_strict = AsyncMock(side_effect=["not-an-int", "bad", "also-bad"])

    import chatgpt_web2api.cdp_driver as drv_mod
    monkeypatch.setattr(drv_mod.asyncio, "sleep", AsyncMock())

    with pytest.raises(SendReadinessError, match="Cannot establish pre-send"):
        await d._read_assistant_count_baseline()
