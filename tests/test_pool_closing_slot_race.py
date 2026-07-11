"""Regression test: closing-slot replacement race (ChatGPT review, conv 6a52a623).

When the idle sweeper is closing a slot and the same session makes a new request
before the close finishes, the pool creates a replacement slot for the same
session key. But when the old close finishes, the sweeper unconditionally
discards the session key from _active_keys — even though the replacement is live.

This test verifies the fix:
  1. The sweeper only discards _active_keys when the closing slot IS still the
     current mapping (identity check extends to _active_keys).
  2. A session that reacquires while its old slot is closing does not lose
     capacity accounting.
"""

import asyncio
from unittest.mock import MagicMock

import pytest

from chatgpt_web2api.config import Config
from chatgpt_web2api.mcp_driver_pool import McpSessionDriverPool


def _make_pool(max_size=2, ttl=0.01, sweep_interval=0.1):
    """Build a pool with a fake driver factory. TTL is very short so the
    sweeper closes idle slots almost immediately. Sweep interval is short
    so the sweeper runs quickly."""
    cfg = Config()
    cfg.chatgpt.mcp_session_pool_size = max_size
    cfg.chatgpt.mcp_session_pool_acquire_timeout = 5.0
    cfg.chatgpt.mcp_session_pool_ttl_seconds = ttl
    cfg.chatgpt.mcp_session_pool_sweep_interval_seconds = sweep_interval

    close_events = []

    async def fake_factory(config, transport, port, slot):
        driver = MagicMock()
        # Make close slow so we can interleave a reacquisition
        async def slow_close():
            close_events.append(("close_start", slot.session_key))
            await asyncio.sleep(0.5)
            close_events.append(("close_done", slot.session_key))

        driver.close = slow_close
        driver._current_conv_id = None
        return driver

    pool = McpSessionDriverPool(cfg, driver_factory=fake_factory)
    return pool, close_events


@pytest.mark.asyncio
async def test_reacquire_during_close_does_not_lose_capacity():
    """When a session reacquires while its old slot is closing, the replacement
    slot must remain in _active_keys after the old close completes.

    ChatGPT found that the sweeper unconditionally discards _active_keys even
    when the slot is no longer the current mapping. This test reproduces that
    race: with pool_size=2, session A acquires, goes idle (sweeper starts
    closing), then reacquires before close finishes. After the old close
    completes, session A's replacement must still be in _active_keys.
    """
    pool, close_events = _make_pool(max_size=2, ttl=0.01)

    try:
        # Session A acquires a slot.
        async with pool.acquire("session-A"):
            pass  # Release immediately → slot goes idle

        # Start the sweeper — it checks every 0.1s, TTL is 0.01s, so the
        # first sweep at ~0.1s will mark the slot closing and start slow_close.
        await pool.start_sweeper()

        # Wait long enough for the sweeper to mark the slot closing AND start
        # the slow driver.close() (which takes 0.5s).
        await asyncio.sleep(0.2)

        # At this point the old slot should be closing=True and driver.close()
        # should be in progress. Session A reacquires.
        async with pool.acquire("session-A") as lease:
            assert lease is not None
            # While holding the replacement, wait for the old close to finish.
            await asyncio.sleep(1.0)

        # After the old close is done, session-A's replacement must still be
        # tracked in _active_keys. If the sweeper discarded it unconditionally
        # (the bug), _active_keys would be empty and capacity accounting is wrong.
        assert "session-A" in pool._active_keys, (
            "Replacement slot lost from _active_keys after old close completed — "
            "the sweeper discarded it unconditionally (the race ChatGPT found)"
        )

        # Verify capacity is correct: the replacement should count against the cap.
        # A new session should still be able to acquire (pool_size=2, one slot used).
        async with pool.acquire("session-B"):
            assert "session-B" in pool._active_keys
    finally:
        await pool.close_all()
