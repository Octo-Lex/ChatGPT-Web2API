"""P1.5: Pool lease accounting instrumentation.

Purely additive diagnostics — no behavioral change to pool logic. Tracks
each lease's lifecycle (acquire → release) so the next field session and the
pool-exhaustion RCA have attributable data.

Verifies:
  1. Lease acquire creates a record with lease_id, session_key, timestamp.
  2. Lease release records duration + release_reason (normal/exception/cancel).
  3. PoolExhaustedError includes an active-lease dump.
  4. Double-release is detected and logged (not silently swallowed).
  5. status() includes lease accounting summary.
"""

import logging
from unittest.mock import AsyncMock, MagicMock

import pytest

from chatgpt_web2api.config import Config
from chatgpt_web2api.mcp_driver_pool import (
    McpSessionDriverPool,
    PoolExhaustedError,
)


def _make_pool(max_size=2, **overrides):
    """Build a pool with a fake driver factory for fast tests."""
    cfg = Config()
    cfg.chatgpt.mcp_session_pool_size = max_size
    cfg.chatgpt.mcp_session_pool_acquire_timeout = 2.0
    for k, v in overrides.items():
        setattr(cfg.chatgpt, k, v)

    async def fake_factory(config, transport, port, slot):
        driver = MagicMock()
        driver.close = AsyncMock()
        driver._current_conv_id = None
        return driver

    pool = McpSessionDriverPool(cfg, driver_factory=fake_factory)
    return pool


# ── 1. Lease acquire creates a record ────────────────────────────────────


@pytest.mark.asyncio
async def test_acquire_creates_lease_record():
    """Acquiring a lease creates a record with lease_id, session_key, timestamp."""
    pool = _make_pool(max_size=2)
    try:
        async with pool.acquire("session-1"):
            # The pool should have exactly one active lease record.
            assert len(pool.active_leases) == 1, (
                f"expected 1 active lease, got {len(pool.active_leases)}"
            )
            record = list(pool.active_leases.values())[0]
            assert record.session_key == "session-1"
            assert record.lease_id  # non-empty ID
            assert record.acquired_at > 0
    finally:
        await pool.close_all()


# ── 2. Lease release records duration + reason ──────────────────────────


@pytest.mark.asyncio
async def test_release_records_duration_and_normal_reason():
    """On normal release, the record has released_at set and reason='normal'."""
    pool = _make_pool()
    try:
        async with pool.acquire("session-1"):
            pass  # normal exit
        # Lease was released; active_leases should be empty.
        assert len(pool.active_leases) == 0
        # History should have the released record.
        assert len(pool.lease_history) >= 1
        rec = pool.lease_history[-1]
        assert rec.released_at is not None
        assert rec.release_reason == "normal"
        assert rec.hold_duration_s is not None
        assert rec.hold_duration_s >= 0
    finally:
        await pool.close_all()


@pytest.mark.asyncio
async def test_release_records_exception_reason():
    """On exception exit, the release reason is 'exception', not 'normal'."""
    pool = _make_pool()
    try:
        with pytest.raises(ValueError):
            async with pool.acquire("session-1"):
                raise ValueError("test error")
        rec = pool.lease_history[-1]
        assert rec.release_reason == "exception"
    finally:
        await pool.close_all()


# ── 3. PoolExhaustedError includes active-lease dump ────────────────────


@pytest.mark.asyncio
async def test_exhaustion_includes_active_lease_dump():
    """When the pool is exhausted, the error message includes info about
    what leases are currently active (session keys, hold durations, in_flight)."""
    pool = _make_pool(max_size=1)
    try:
        # Hold one lease to fill the pool.
        async with pool.acquire("session-1"):
            # Try to acquire a second — should exhaust.
            with pytest.raises(PoolExhaustedError) as exc_info:
                async with pool.acquire("session-2"):
                    pass
            msg = str(exc_info.value)
            # The error should mention the active session holding the slot.
            assert "session-1" in msg, (
                f"exhaustion error should include active lease dump, got: {msg}"
            )
    finally:
        await pool.close_all()


# ── 4. Double-release detection ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_double_release_detected(caplog):
    """Releasing a lease that's already released logs a warning (double-release bug)."""
    pool = _make_pool()
    try:
        lease_ctx = pool.acquire("session-1")
        async with lease_ctx as lease:
            pass  # normal release
        # Manually call _release again on the same slot with the same lease_id
        # (simulates a double-release bug where the lease is released twice).
        with caplog.at_level(logging.WARNING):
            await pool._release(lease.slot, lease_id=lease.lease_id, release_reason="normal")
        assert any("double" in r.message.lower() or "already released" in r.message.lower()
                      for r in caplog.records), (
            "double-release should log a warning"
        )
    finally:
        await pool.close_all()


# ── 5. status() includes lease summary ───────────────────────────────────


@pytest.mark.asyncio
async def test_status_includes_lease_accounting():
    """status() should include lease counts (active + history)."""
    pool = _make_pool()
    try:
        async with pool.acquire("session-1"):
            pass
        s = pool.status()
        assert "leases" in s, "status() should include lease accounting"
        lease_info = s["leases"]
        assert "active_count" in lease_info
        assert "history_count" in lease_info
        assert lease_info["history_count"] >= 1
    finally:
        await pool.close_all()
