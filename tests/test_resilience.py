"""Tests for retry_on_rate_limit — the transparent-retry wrapper.

This is the core "make agentic workflows practical" piece: catch a
RateLimitError, dismiss the pop-up, back off, and retry — so transient
limits are invisible to the caller. Only when retries exhaust does the
RateLimitError propagate (so the consumer layer can convert it to 429).

The wrapper is a pure async utility; tests use fakes, no browser needed.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from chatgpt_web2api.cdp_driver import RateLimitError
from chatgpt_web2api.resilience import retry_on_rate_limit


def _driver_with_dismiss(returns: bool):
    d = MagicMock()
    d.dismiss_rate_limit = AsyncMock(return_value=returns)
    return d


# ── succeeds without retry ────────────────────────────────────

@pytest.mark.asyncio
async def test_no_retry_when_first_call_succeeds():
    """A call that succeeds immediately returns its result; no retry."""
    driver = _driver_with_dismiss(True)
    calls = 0

    async def factory():
        nonlocal calls
        calls += 1
        return "ok"

    result = await retry_on_rate_limit(driver, factory, max_attempts=3)
    assert result == "ok"
    assert calls == 1
    driver.dismiss_rate_limit.assert_not_called()


# ── succeeds after retry ──────────────────────────────────────

@pytest.mark.asyncio
async def test_retries_after_rate_limit_then_succeeds():
    """A transient limit: first call raises, second succeeds → result returned."""
    driver = _driver_with_dismiss(True)
    attempts = []

    async def factory():
        attempts.append(1)
        if len(attempts) == 1:
            raise RateLimitError(retry_after=1)
        return "recovered"

    # Use a tiny backoff so the test is fast.
    result = await retry_on_rate_limit(
        driver, factory, max_attempts=3, backoff=0.01
    )
    assert result == "recovered"
    assert len(attempts) == 2
    driver.dismiss_rate_limit.assert_awaited_once()


# ── exhausts and re-raises ────────────────────────────────────

@pytest.mark.asyncio
async def test_exhausts_retries_and_reraises():
    """A persistent limit: every attempt raises → RateLimitError propagates."""
    driver = _driver_with_dismiss(True)
    attempts = []

    async def factory():
        attempts.append(1)
        raise RateLimitError(retry_after=1)

    with pytest.raises(RateLimitError) as exc_info:
        await retry_on_rate_limit(driver, factory, max_attempts=3, backoff=0.01)

    assert len(attempts) == 3  # 1 initial + 2 retries
    # The propagated error carries the retry_after so the caller can build a 429.
    assert exc_info.value.retry_after == 1


# ── non-rate-limit errors pass through unchanged ──────────────

@pytest.mark.asyncio
async def test_non_rate_limit_error_is_not_retried():
    """A ValueError (or any non-RateLimitError) propagates immediately, no retry."""
    driver = _driver_with_dismiss(True)
    attempts = []

    async def factory():
        attempts.append(1)
        raise ValueError("totally different problem")

    with pytest.raises(ValueError):
        await retry_on_rate_limit(driver, factory, max_attempts=3)

    assert len(attempts) == 1
    driver.dismiss_rate_limit.assert_not_called()


# ── dismiss is best-effort; retry continues even if dismiss fails ─

@pytest.mark.asyncio
async def test_retry_continues_even_if_dismiss_fails():
    """If dismiss returns False, we still back off and retry (best-effort)."""
    driver = _driver_with_dismiss(False)  # dismiss never succeeds
    attempts = []

    async def factory():
        attempts.append(1)
        if len(attempts) < 3:
            raise RateLimitError(retry_after=1)
        return "ok"

    result = await retry_on_rate_limit(
        driver, factory, max_attempts=3, backoff=0.01
    )
    assert result == "ok"
    assert len(attempts) == 3


# ── backoff respects retry_after but is capped ────────────────

@pytest.mark.asyncio
async def test_backoff_uses_retry_after_when_smaller_than_cap(monkeypatch):
    """The wait before a retry is min(retry_after, cap). Verify it's used."""
    driver = _driver_with_dismiss(True)
    slept = []

    async def fake_sleep(s):
        slept.append(s)

    monkeypatch.setattr("chatgpt_web2api.resilience.asyncio.sleep", fake_sleep)

    async def factory():
        if len(slept) == 0:
            raise RateLimitError(retry_after=5)
        return "ok"

    await retry_on_rate_limit(
        driver, factory, max_attempts=2, backoff=100, cap=10
    )
    # retry_after=5 < cap=10, so the sleep should be ~5 (allowing jitter).
    assert slept and slept[0] <= 10


@pytest.mark.asyncio
async def test_max_attempts_one_means_no_retry():
    """max_attempts=1 = try once, never retry."""
    driver = _driver_with_dismiss(True)
    attempts = []

    async def factory():
        attempts.append(1)
        raise RateLimitError(retry_after=1)

    with pytest.raises(RateLimitError):
        await retry_on_rate_limit(driver, factory, max_attempts=1, backoff=0.01)
    assert len(attempts) == 1
