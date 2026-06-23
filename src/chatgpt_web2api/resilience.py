"""Resilience helpers for transparent rate-limit handling.

``retry_on_rate_limit`` wraps an async operation so that ChatGPT's transient
"Too many requests" throttling is invisible to callers: on a RateLimitError it
dismisses the pop-up, backs off, and retries — up to ``max_attempts``. Only a
*persistent* limit (every attempt throttled) propagates the RateLimitError, so
the consumer layer (REST API → HTTP 429, MCP → structured result) can convert
it into a standard, machine-readable pause signal.

Design notes:
  - Dismiss is best-effort: if ``dismiss_rate_limit`` fails (returns False),
    we still back off and retry. The pop-up may have already cleared, or the
    selector may have drifted; either way retrying is harmless and correct.
  - Backoff per attempt = ``min(retry_after, cap)`` with small jitter, so a
    reported "retry in 5s" is respected but a huge value is capped, and
    thundering-herd on many concurrent callers is avoided.
  - Non-RateLimitError exceptions propagate immediately (no retry) — we only
    paper over the one failure mode we know is transient and dismissable.
"""

from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import Awaitable, Callable
from typing import TypeVar

from .cdp_driver import RATE_LIMIT_DEFAULT_RETRY_AFTER, RateLimitError

logger = logging.getLogger(__name__)

T = TypeVar("T")

# Cap on a single backoff wait (seconds), even if retry_after reports more.
# Prevents a pathological reported value from stalling a request for minutes.
_DEFAULT_CAP = 120


async def retry_on_rate_limit(
    driver,
    factory: Callable[[], Awaitable[T]],
    max_attempts: int = 3,
    backoff: float = RATE_LIMIT_DEFAULT_RETRY_AFTER,
    cap: float = _DEFAULT_CAP,
    on_progress: Callable[[str], Awaitable[None]] | None = None,
) -> T:
    """Run ``factory()``, transparently retrying on RateLimitError.

    Args:
        driver: a CDPDriver (used only to call ``dismiss_rate_limit``).
        factory: zero-arg async callable producing the operation to run. Called
            fresh on each attempt so generators/iterators restart cleanly.
        max_attempts: total attempts including the first. ``1`` = no retry.
        backoff: fallback wait (seconds) when the error carries no retry_after.
        cap: maximum seconds to wait in a single backoff, regardless of
            retry_after. Avoids very long reported waits stalling callers.
            Small jitter is added to avoid herd effects.
        on_progress: optional notifier for the backoff pause. When supplied,
            a single "Rate limited, retrying in Ns…" signal is emitted BEFORE
            the sleep so an MCP client's idle timer is reset during what is
            the longest silence in the system (up to ~120s). This is the same
            callback the factory's captured closure uses inside the business
            function — same object, two injection points. Best-effort: a
            failed notification is swallowed (we're already on an error path).

    Returns:
        The result of ``factory()`` on the first non-throttled attempt.

    Raises:
        RateLimitError: if every attempt is throttled (carries the last
            ``retry_after``, so the caller can surface it as a 429).
        Any other exception from ``factory()`` propagates immediately.

    Note on progress across retries: the on_progress counter is bound to the
    outer call_tool invocation and persists across attempts, so the numeric
    progress keeps climbing. But the business function re-streams the response
    from scratch on each retry, so the message text may visually "reset"
    (e.g. "Streaming… 847 chars" → "Assistant is responding…"). Expected —
    see _make_progress_callback in mcp_server.py.
    """
    last_error: RateLimitError | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            return await factory()
        except RateLimitError as e:
            last_error = e
            if attempt >= max_attempts:
                logger.warning(
                    "Rate limit persisted after %d attempt(s); giving up.", attempt
                )
                raise
            # Transient: try to clear the pop-up, then back off and retry.
            try:
                dismissed = await driver.dismiss_rate_limit()
            except Exception:  # best-effort
                dismissed = None  # unknown — see dismiss_rate_limit's tri-state contract
            wait = min(e.retry_after or backoff, cap)
            wait = wait + random.uniform(0, min(wait, 1.0))  # jitter
            logger.info(
                "Rate limit on attempt %d/%d (dismissed=%s); backing off %.1fs",
                attempt, max_attempts, dismissed, wait,
            )
            # Signal BEFORE sleeping — the backoff is the longest silence in
            # the system and the most likely thing to trip a client timeout.
            # Ordering is asserted in tests (test_resilience progress ordering).
            if on_progress is not None:
                try:
                    await on_progress(f"Rate limited, retrying in {wait:.0f}s…")
                except Exception:
                    pass  # don't compound an error-recovery path
            await asyncio.sleep(wait)
    # Unreachable: the loop either returns or re-raises on the last attempt.
    assert last_error is not None
    raise last_error
