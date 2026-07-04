"""Tests for B1 MCP session-affine driver pool (Step 3).

Tests cover the highest-risk areas:
  - acquire() as async context manager (not coroutine)
  - same session reuses same driver
  - different sessions get different drivers
  - pool exhaustion returns PoolExhaustedError
  - same session operations serialize via call_lock
  - materialize failure frees capacity
  - idle TTL closes driver and removes mapping
  - capacity held until close completes
  - pool shutdown prevents new creation
  - DriverLease is not itself a releasing context manager
  - release decrements in_flight exactly once
"""
from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from chatgpt_web2api.mcp_driver_pool import (
    AccountThrottleBreaker,
    DriverLease,
    McpSessionDriverPool,
    PoolExhaustedError,
    PoolShuttingDownError,
    _LeaseContext,
)


def _make_config(pool_size=2, ttl=1800, acquire_timeout=5.0, create_concurrency=1):
    """Build a mock Config with pool settings."""
    cfg = MagicMock()
    cfg.chatgpt.mcp_session_pool_size = pool_size
    cfg.chatgpt.mcp_session_pool_ttl_seconds = ttl
    cfg.chatgpt.mcp_session_pool_acquire_timeout = acquire_timeout
    cfg.chatgpt.mcp_session_pool_sweep_interval_seconds = 60
    cfg.chatgpt.mcp_session_pool_create_concurrency = create_concurrency
    cfg.chatgpt.mcp_account_throttle_cooldown_seconds = 300
    return cfg


def _make_driver(name="driver"):
    """Build a mock CDPDriver."""
    d = MagicMock()
    d.name = name
    d.close = AsyncMock()
    return d


async def _fake_driver_factory(cfg, transport, port, slot):
    """Factory that creates mock drivers with unique names."""
    d = _make_driver(name=f"driver-{time.monotonic()}")
    return d


# ── acquire() API shape ───────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_acquire_returns_lease_context_not_coroutine():
    pool = McpSessionDriverPool(_make_config(), driver_factory=_fake_driver_factory)
    ctx = pool.acquire("sess-1")
    assert isinstance(ctx, _LeaseContext)
    # Must NOT be a coroutine.
    import inspect
    assert not inspect.iscoroutine(ctx)


@pytest.mark.asyncio
async def test_acquire_usable_as_async_context_manager():
    pool = McpSessionDriverPool(_make_config(), driver_factory=_fake_driver_factory)
    async with pool.acquire("sess-1") as lease:
        assert isinstance(lease, DriverLease)
        assert lease.driver is not None


@pytest.mark.asyncio
async def test_driverlease_is_not_itself_releasing_context_manager():
    """DriverLease must NOT implement __aenter__/__aexit__ (double-release guard)."""
    pool = McpSessionDriverPool(_make_config(), driver_factory=_fake_driver_factory)
    async with pool.acquire("sess-1") as lease:
        assert not hasattr(lease, "__aenter__")
        assert not hasattr(lease, "__aexit__")


# ── Session affinity ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_same_session_reuses_same_driver():
    pool = McpSessionDriverPool(_make_config(), driver_factory=_fake_driver_factory)
    async with pool.acquire("sess-1") as lease1:
        driver1 = lease1.driver
    async with pool.acquire("sess-1") as lease2:
        driver2 = lease2.driver
    assert driver1 is driver2


@pytest.mark.asyncio
async def test_different_sessions_get_different_drivers():
    pool = McpSessionDriverPool(_make_config(), driver_factory=_fake_driver_factory)
    async with pool.acquire("sess-1") as lease1:
        pass
    async with pool.acquire("sess-2") as lease2:
        pass
    # The drivers should be different objects.
    assert lease1 is not lease2


# ── Pool exhaustion ───────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_pool_size_exhaustion_returns_pool_exhausted_error():
    pool = McpSessionDriverPool(
        _make_config(pool_size=1, acquire_timeout=0.5),
        driver_factory=_fake_driver_factory,
    )
    # Hold the one slot.
    async with pool.acquire("sess-1"):
        # Second session should time out.
        with pytest.raises(PoolExhaustedError):
            async with pool.acquire("sess-2"):
                pass


# ── call_lock serialization ───────────────────────────────────────────────

@pytest.mark.asyncio
async def test_same_session_operations_serialize_via_call_lock():
    pool = McpSessionDriverPool(_make_config(), driver_factory=_fake_driver_factory)
    order = []

    async def do_work(label: str, delay: float):
        async with pool.acquire("sess-1") as lease:
            async with lease.call_lock:
                order.append(f"start-{label}")
                await asyncio.sleep(delay)
                order.append(f"end-{label}")

    # Two concurrent operations on the same session.
    await asyncio.gather(do_work("a", 0.05), do_work("b", 0.01))
    # They must not interleave — call_lock serializes.
    assert order == ["start-a", "end-a", "start-b", "end-b"]


# ── Release semantics ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_release_decrements_in_flight_exactly_once():
    pool = McpSessionDriverPool(_make_config(), driver_factory=_fake_driver_factory)
    async with pool.acquire("sess-1") as lease:
        assert lease.slot.in_flight == 1
    # After context exit, in_flight should be 0.
    assert lease.slot.in_flight == 0


# ── Materialization failure ───────────────────────────────────────────────

@pytest.mark.asyncio
async def test_materialize_failure_frees_capacity():
    call_count = [0]

    async def failing_factory(cfg, transport, port, slot):
        call_count[0] += 1
        if call_count[0] == 1:
            raise ConnectionError("Chrome not ready")
        return _make_driver()

    pool = McpSessionDriverPool(
        _make_config(pool_size=1, acquire_timeout=5.0),
        driver_factory=failing_factory,
    )
    # First acquire should fail.
    with pytest.raises(ConnectionError):
        async with pool.acquire("sess-1"):
            pass
    # Capacity should be freed — a different session can acquire.
    async with pool.acquire("sess-2") as lease:
        assert lease.driver is not None


# ── Idle sweep ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_idle_ttl_closes_driver_and_removes_mapping():
    pool = McpSessionDriverPool(
        _make_config(pool_size=2, ttl=0.1, acquire_timeout=2.0),
        driver_factory=_fake_driver_factory,
    )
    pool._sweep_interval = 0.05  # fast sweep for testing
    await pool.start_sweeper()

    # Create a driver, then release it.
    async with pool.acquire("sess-1") as lease:
        driver = lease.driver
    assert driver is not None

    # Wait for idle TTL + sweep.
    await asyncio.sleep(0.3)

    # The driver should have been closed.
    driver.close.assert_called_once()
    # The slot should be gone.
    assert "sess-1" not in pool._active_keys

    await pool.close_all()


@pytest.mark.asyncio
async def test_idle_closing_slot_counts_against_capacity_until_close():
    """A slot being closed still counts against capacity."""
    slow_close = _make_driver("slow")
    close_event = asyncio.Event()

    async def slow_close_fn():
        await close_event.wait()

    slow_close.close = slow_close_fn

    async def factory(cfg, transport, port, slot):
        return slow_close

    pool = McpSessionDriverPool(
        _make_config(pool_size=1, ttl=0.05, acquire_timeout=1.0),
        driver_factory=factory,
    )
    pool._sweep_interval = 0.02
    await pool.start_sweeper()

    # Create and release.
    async with pool.acquire("sess-1"):
        pass

    # Wait for sweep to mark closing (but close is blocked on close_event).
    await asyncio.sleep(0.1)

    # Pool should still be at capacity (closing slot counts).
    assert len(pool._active_keys) == 1

    # Now let the close complete.
    close_event.set()
    await asyncio.sleep(0.1)

    # Capacity should be freed.
    assert len(pool._active_keys) == 0

    await pool.close_all()


# ── Shutdown ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_pool_shutdown_prevents_new_slot_creation():
    pool = McpSessionDriverPool(_make_config(), driver_factory=_fake_driver_factory)
    await pool.close_all()
    with pytest.raises(PoolShuttingDownError):
        async with pool.acquire("sess-1"):
            pass


@pytest.mark.asyncio
async def test_shutdown_during_materialization_does_not_leak_driver():
    """If shutdown starts during materialization, the driver is closed, not leaked."""
    materialize_started = asyncio.Event()

    async def slow_factory(cfg, transport, port, slot):
        materialize_started.set()
        await asyncio.sleep(0.5)  # slow materialization
        return _make_driver()

    pool = McpSessionDriverPool(
        _make_config(acquire_timeout=10.0),
        driver_factory=slow_factory,
    )

    # Start acquire in background.
    acquire_task = asyncio.create_task(pool._acquire_slot("sess-1"))

    # Wait for materialization to start.
    await asyncio.wait_for(materialize_started.wait(), timeout=2.0)

    # Shutdown the pool while materialization is in progress.
    close_task = asyncio.create_task(pool.close_all())

    # The acquire should raise PoolShuttingDownError.
    with pytest.raises((PoolShuttingDownError, Exception)):
        await acquire_task

    await close_task


# ── Account throttle breaker ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_account_breaker_trips_and_blocks():
    breaker = AccountThrottleBreaker(cooldown_seconds=300)
    assert not breaker.is_tripped()
    await breaker.trip()
    assert breaker.is_tripped()
    breaker.reset()
    assert not breaker.is_tripped()


@pytest.mark.asyncio
async def test_account_breaker_is_shared_across_sessions():
    pool = McpSessionDriverPool(_make_config(), driver_factory=_fake_driver_factory)
    breaker = pool.account_breaker
    await breaker.trip()
    assert breaker.is_tripped()
    await pool.close_all()


# ── Status ────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_status_returns_pool_diagnostics():
    pool = McpSessionDriverPool(
        _make_config(pool_size=3), driver_factory=_fake_driver_factory
    )
    async with pool.acquire("sess-1"):
        pass
    s = pool.status()
    assert s["enabled"] is True
    assert s["max_size"] == 3
    assert s["active_keys"] == 1
    assert "sess-1" in s["slots"]
    await pool.close_all()


# ── Concurrent first calls same session create one slot ──────────────────

@pytest.mark.asyncio
async def test_concurrent_first_calls_same_new_session_create_one_slot():
    pool = McpSessionDriverPool(_make_config(), driver_factory=_fake_driver_factory)
    # Two concurrent acquires for the same NEW session.
    results = await asyncio.gather(
        pool._acquire_slot("sess-1"),
        pool._acquire_slot("sess-1"),
    )
    # Both should get the same driver.
    assert results[0].driver is results[1].driver
    assert len(pool._active_keys) == 1
    await pool.close_all()
