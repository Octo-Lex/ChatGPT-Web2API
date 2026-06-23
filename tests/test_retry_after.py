"""Tests for rate-limit retry-after parsing.

ChatGPT's pop-up says things like "Please wait a few minutes before trying
again." with no exact number. ``parse_retry_after`` extracts a wait in seconds
when it can, and falls back to a conservative default. ``RateLimitError``
carries the parsed/reported ``retry_after`` so consumers (REST 429 header,
MCP structured result) can report it.
"""

import pytest

from chatgpt_web2api.cdp_driver import RateLimitError, parse_retry_after

# ── parse_retry_after ─────────────────────────────────────────

DEFAULT = 60  # the conservative fallback used when no duration is parseable


@pytest.mark.parametrize("text,expected", [
    # The actual observed pop-up text (no explicit number → fallback)
    ("Too many requests\n\nYou're making requests too quickly. We've temporarily "
     "limited access to your conversations to protect your data.\n\nPlease wait a "
     "few minutes before trying again.\n\nGot it", DEFAULT),
    # "a few minutes" is vague but > 1 min → treat as the default (no exact number)
    ("Please wait a few minutes before trying again.", DEFAULT),
    # Explicit numbers
    ("Please try again in 30 seconds.", 30),
    ("Please try again in 2 minutes.", 120),
    ("try again in 1 minute", 60),
    ("Rate limited. Retry in 5 minutes.", 300),
    ("wait 45 secs and retry", 45),
    # Empty / garbage → fallback
    ("", DEFAULT),
    ("totally unrelated text", DEFAULT),
])
def test_parse_retry_after(text, expected):
    assert parse_retry_after(text) == expected


def test_parse_retry_after_default_is_configurable():
    """The fallback can be overridden via the default argument."""
    assert parse_retry_after("nothing here", default=120) == 120


# ── RateLimitError carries retry_after ─────────────────────────

def test_rate_limit_error_has_retry_after_default():
    """A RateLimitError with no arg reports the conservative default."""
    err = RateLimitError()
    assert err.retry_after == DEFAULT


def test_rate_limit_error_carries_explicit_retry_after():
    """The reported wait is stored on the exception."""
    err = RateLimitError(retry_after=120)
    assert err.retry_after == 120


def test_rate_limit_error_message_mentions_the_wait():
    """The human message should include the wait so it's visible in logs/errors."""
    err = RateLimitError(retry_after=30)
    assert "30" in str(err)


def test_rate_limit_error_from_text_parses_duration():
    """Constructing from the pop-up text extracts the retry_after."""
    err = RateLimitError.from_text("Please try again in 2 minutes.")
    assert err.retry_after == 120
