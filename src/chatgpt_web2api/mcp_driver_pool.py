"""B1: MCP session-affine CDPDriver pool.

A lazy, bounded, session-affine pool of ``CDPDriver`` instances for MCP SSE.
In pool mode, MCP startup does NOT connect to Chrome. The first explicit
browser-affecting request for an MCP session materializes one owned
``CDPDriver``/tab. Later requests from the same session reuse that driver.
Different sessions receive different owned tabs, capped by pool size.

Design (peer-reviewed, implementation-ready per B1 spec):
  - Three slot states: PENDING → ACTIVE → CLOSING → DISOWNED.
  - Lock ordering: pool_lock → meta_lock (never reversed).
  - call_lock serializes ALL operations per session (reads + mutations).
  - Capacity is a hard cap on active + pending + closing slots.
  - A closing slot counts against capacity until driver.close() completes.
  - acquire() returns a sync _LeaseContext; the async logic is in _acquire_slot().
  - No driver.close() while pool_lock is held.
  - Idle sweeper marks closing but doesn't free capacity until close completes.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


# ── Errors ────────────────────────────────────────────────────────────────

class PoolExhaustedError(RuntimeError):
    """Pool is full after acquire_timeout."""


class PoolShuttingDownError(RuntimeError):
    """Pool is shutting down; no new slots can be created."""


class PoolSlotUnavailableError(RuntimeError):
    """A materialized slot's driver disappeared (race during shutdown)."""


# ── Account throttle breaker ──────────────────────────────────────────────

class AccountThrottleBreaker:
    """Pool-wide breaker that pauses mutations on account-throttle signals.

    Does NOT block reads. Does NOT handle CAPTCHA, hard account lock, or ban.
    Those require distinct failure modes.
    """

    def __init__(self, cooldown_seconds: float) -> None:
        self._cooldown = cooldown_seconds
        self._tripped_until: float | None = None
        self._lock = asyncio.Lock()

    def is_tripped(self) -> bool:
        return (
            self._tripped_until is not None
            and time.monotonic() < self._tripped_until
        )

    async def trip(self) -> None:
        async with self._lock:
            self._tripped_until = time.monotonic() + self._cooldown
        logger.warning(
            "Account-level throttle signal observed; pausing mutating MCP calls "
            "pool-wide for %.0fs (mcp_account_throttled)",
            self._cooldown,
        )

    def reset(self) -> None:
        self._tripped_until = None


# ── Slot ──────────────────────────────────────────────────────────────────

@dataclass
class DriverSlot:
    """One session's slot in the pool. Transitions: PENDING → ACTIVE → CLOSING → DISOWNED.

    State is determined by the combination of fields (see B1 §2 state table):
      PENDING:  driver=None, ready_event unset, closing=False
      ACTIVE:   driver set, ready_event set, closing=False
      CLOSING:  closing=True (driver may be set or None; still counts vs capacity)
      DISOWNED: removed from _slots and _active_keys (not reachable)
    """
    session_key: str
    driver: Any | None  # CDPDriver | None
    breakers: Any  # BreakerRegistry
    meta_lock: asyncio.Lock
    call_lock: asyncio.Lock
    ready_event: asyncio.Event
    materialize_error: Exception | None = None
    created_at: float = 0.0
    last_used_at: float = 0.0
    in_flight: int = 0
    closing: bool = False


# ── Lease ─────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class DriverLease:
    """Plain carrier for a leased driver. Does NOT implement __aenter__/__aexit__."""
    slot: DriverSlot
    driver: Any  # CDPDriver
    breakers: Any  # BreakerRegistry
    call_lock: asyncio.Lock


class _LeaseContext:
    """Async context manager wrapper. acquire() returns this (not a coroutine).

    Avoids the runtime bug where ``async with pool.acquire()`` would try to use
    a coroutine object as an async context manager.
    """

    def __init__(self, pool: McpSessionDriverPool, session_key: str) -> None:
        self._pool = pool
        self._session_key = session_key
        self._lease: DriverLease | None = None

    async def __aenter__(self) -> DriverLease:
        self._lease = await self._pool._acquire_slot(self._session_key)
        return self._lease

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        if self._lease is None:
            return
        await self._pool._release(self._lease.slot)
        self._lease = None


# ── Pool ──────────────────────────────────────────────────────────────────

class McpSessionDriverPool:
    """Lazy, bounded, session-affine pool of CDPDriver instances.

    Owned by the MCP server (mcp_server.py). The pool creates drivers on demand
    (lazy materialization), one per session key, bounded by pool_size. Idle
    slots are swept after TTL. The account throttle breaker pauses mutations
    pool-wide on account-throttle signals.
    """

    def __init__(
        self,
        config: Any,  # Config
        *,
        transport: str = "sse",
        port: int = 8090,
        driver_factory: Callable[..., Awaitable[Any]] | None = None,
    ) -> None:
        cfg = config.chatgpt
        self._max_size = cfg.mcp_session_pool_size
        self._ttl = cfg.mcp_session_pool_ttl_seconds
        self._acquire_timeout = cfg.mcp_session_pool_acquire_timeout
        self._sweep_interval = cfg.mcp_session_pool_sweep_interval_seconds
        self._create_sem = asyncio.Semaphore(cfg.mcp_session_pool_create_concurrency)
        self._config = config
        self._transport = transport
        self._port = port
        # Injectable factory for testing; None = real CDPDriver creation.
        self._driver_factory = driver_factory

        self._slots: dict[str, DriverSlot] = {}
        self._active_keys: set[str] = set()
        self._pool_lock = asyncio.Lock()
        self._capacity_available = asyncio.Condition(self._pool_lock)
        self._shutting_down = False
        self._account_breaker = AccountThrottleBreaker(
            cfg.mcp_account_throttle_cooldown_seconds
        )
        self._sweep_task: asyncio.Task | None = None

    @property
    def account_breaker(self) -> AccountThrottleBreaker:
        return self._account_breaker

    def acquire(self, session_key: str) -> _LeaseContext:
        """Return a sync _LeaseContext (NOT async). Use as ``async with pool.acquire(key)``."""
        return _LeaseContext(self, session_key)

    async def start_sweeper(self) -> None:
        """Start the idle-slot sweeper background task."""
        if self._sweep_task is None or self._sweep_task.done():
            self._sweep_task = asyncio.create_task(self._sweep_idle())

    async def _create_driver(self, slot: DriverSlot) -> Any:
        """Create and connect a new CDPDriver for a slot. Injectable for testing.

        Passes slot.breakers into CDPDriver so driver failure sites record
        into the slot's breaker registry (PR #42 review fix #3).
        Derives instance_id from the session_key for per-session tab-registry
        identity (PR #42 review fix #4).
        """
        if self._driver_factory is not None:
            return await self._driver_factory(self._config, self._transport, self._port, slot)
        # Real path: construct + connect a CDPDriver.
        import hashlib
        import os

        from .cdp_driver import CDPDriver
        from .tab_registry import TabRegistry

        cfg = self._config
        # Per-session tab-registry identity, derived from session_key (fix #4).
        # W2A_INSTANCE_ID in pool mode: suffix with session hash so each slot
        # gets a distinct registry identity rather than collapsing to one.
        session_hash = hashlib.sha256(slot.session_key.encode()).hexdigest()[:12]
        if os.environ.get("W2A_INSTANCE_ID"):
            base = os.environ["W2A_INSTANCE_ID"]
            server_identity = f"{base}:session:{session_hash}"
        else:
            server_identity = f"mcp:{self._transport}:{self._port}:session:{session_hash}"
        instance_id = TabRegistry.derive_instance_id(
            cdp_port=cfg.chrome.cdp_port,
            server_identity=server_identity,
        )
        driver = CDPDriver(
            cdp_port=cfg.chrome.cdp_port,
            tab_mode="owned",
            parallel_tabs=True,
            instance_id=instance_id,
            breakers=slot.breakers,
        )
        await driver.connect()
        return driver

    async def _materialize_slot(self, slot: DriverSlot) -> None:
        """Materialize the CDPDriver for a PENDING slot.

        Contract (B1 §12):
          - If this raises, it must have already closed any partially opened
            CDP websocket, owned tab, or browser resource.
          - It must attach only to the one owned tab for this slot.
          - It must not enumerate or attach to unrelated profile tabs.
        """
        logger.info("_materialize_slot entered: session_key=%s", slot.session_key)
        try:
            driver = await self._create_driver(slot)
        except Exception:
            # _create_driver is responsible for cleaning partial resources
            # on failure (the CDPDriver.connect() path handles this).
            raise
        async with slot.meta_lock:
            slot.driver = driver

    async def _abandon_pending_slot(self, slot: DriverSlot) -> None:
        """Clean up a PENDING slot that failed or was abandoned."""
        async with self._pool_lock:
            async with slot.meta_lock:
                slot.in_flight = 0
                slot.closing = True
                slot.driver = None
            if self._slots.get(slot.session_key) is slot:
                del self._slots[slot.session_key]
            self._active_keys.discard(slot.session_key)
            slot.ready_event.set()
            self._capacity_available.notify_all()

    async def _acquire_slot(self, session_key: str) -> DriverLease:
        """Race-free, bounded slot acquisition. See B1 §5 for full design.

        Bounds:
          - acquire_timeout bounds waiting for pool capacity.
          - acquire_timeout bounds waiting for a pending slot to materialize.
          - acquire_timeout bounds waiting for the create semaphore.
          - acquire_timeout does NOT bound actual driver creation once started.
        """
        deadline = time.monotonic() + self._acquire_timeout

        while True:
            pending_slot: DriverSlot | None = None
            materialize_slot: DriverSlot | None = None

            async with self._pool_lock:
                if self._shutting_down:
                    raise PoolShuttingDownError()

                slot = self._slots.get(session_key)

                # Existing usable slot.
                if slot is not None and not slot.closing:
                    if not slot.ready_event.is_set():
                        pending_slot = slot
                    else:
                        async with slot.meta_lock:
                            if slot.closing or slot.driver is None:
                                pass  # Treat as unusable; loop to create/wait.
                            else:
                                slot.in_flight += 1
                                slot.last_used_at = time.monotonic()
                                return DriverLease(
                                    slot=slot,
                                    driver=slot.driver,
                                    breakers=slot.breakers,
                                    call_lock=slot.call_lock,
                                )

                # Need a new slot.
                if pending_slot is None and (
                    slot is None or slot.closing or slot.driver is None
                ):
                    if len(self._active_keys) < self._max_size:
                        now = time.monotonic()
                        new_slot = DriverSlot(
                            session_key=session_key,
                            driver=None,
                            breakers=self._make_breakers(),
                            meta_lock=asyncio.Lock(),
                            call_lock=asyncio.Lock(),
                            ready_event=asyncio.Event(),
                            created_at=now,
                            last_used_at=now,
                            in_flight=1,
                            closing=False,
                        )
                        self._slots[session_key] = new_slot
                        self._active_keys.add(session_key)
                        materialize_slot = new_slot
                        break

                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise PoolExhaustedError()

                    try:
                        await asyncio.wait_for(
                            self._capacity_available.wait_for(
                                lambda: (
                                    len(self._active_keys) < self._max_size
                                    or self._shutting_down
                                )
                            ),
                            timeout=remaining,
                        )
                    except TimeoutError:
                        raise PoolExhaustedError()

                    if self._shutting_down:
                        raise PoolShuttingDownError()
                    continue

            # Existing PENDING slot for this same session.
            if pending_slot is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise PoolExhaustedError()
                try:
                    await asyncio.wait_for(
                        pending_slot.ready_event.wait(), timeout=remaining
                    )
                except TimeoutError:
                    raise PoolExhaustedError()
                continue

        assert materialize_slot is not None

        # Bound waiting for the create semaphore.
        sem_acquired = False
        try:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                await self._abandon_pending_slot(materialize_slot)
                raise PoolExhaustedError()

            try:
                await asyncio.wait_for(
                    self._create_sem.acquire(), timeout=remaining
                )
                sem_acquired = True
            except TimeoutError:
                await self._abandon_pending_slot(materialize_slot)
                raise PoolExhaustedError()

            await self._materialize_slot(materialize_slot)

        except Exception as e:
            materialize_slot.materialize_error = e
            await self._abandon_pending_slot(materialize_slot)
            raise
        finally:
            if sem_acquired:
                self._create_sem.release()

        # Success: publish or disown if shutdown started during materialization.
        driver_to_close: Any | None = None

        async with self._pool_lock:
            if self._shutting_down:
                async with materialize_slot.meta_lock:
                    driver_to_close = materialize_slot.driver
                    materialize_slot.driver = None
                    materialize_slot.in_flight = 0
                    materialize_slot.closing = True
                if self._slots.get(materialize_slot.session_key) is materialize_slot:
                    del self._slots[materialize_slot.session_key]
                self._active_keys.discard(materialize_slot.session_key)
                materialize_slot.ready_event.set()
                self._capacity_available.notify_all()
            else:
                materialize_slot.ready_event.set()

        if driver_to_close is not None:
            try:
                await driver_to_close.close()
            except Exception:
                logger.exception("Error closing driver created during shutdown race")
            raise PoolShuttingDownError()

        async with materialize_slot.meta_lock:
            if materialize_slot.driver is None:
                raise PoolSlotUnavailableError()
            return DriverLease(
                slot=materialize_slot,
                driver=materialize_slot.driver,
                breakers=materialize_slot.breakers,
                call_lock=materialize_slot.call_lock,
            )

    def _make_breakers(self):
        """Create a fresh BreakerRegistry for a slot."""
        from .breakers import BreakerRegistry
        return BreakerRegistry()

    async def _release(self, slot: DriverSlot) -> None:
        """Release a lease: decrement in_flight, update last_used_at."""
        async with slot.meta_lock:
            slot.in_flight = max(0, slot.in_flight - 1)
            slot.last_used_at = time.monotonic()

    async def _sweep_idle(self) -> None:
        """Background loop: close idle slots past TTL.

        Marks victims as closing=True but does NOT remove from _active_keys
        until driver.close() completes. This prevents transient N+1 live tabs.
        """
        while True:
            await asyncio.sleep(self._sweep_interval)
            now = time.monotonic()
            victims: list[DriverSlot] = []

            async with self._pool_lock:
                if self._shutting_down:
                    return
                for key in list(self._active_keys):
                    slot = self._slots.get(key)
                    if slot is None:
                        self._active_keys.discard(key)
                        continue
                    async with slot.meta_lock:
                        if (
                            slot.driver is not None
                            and not slot.closing
                            and slot.in_flight == 0
                            and now - slot.last_used_at > self._ttl
                        ):
                            slot.closing = True
                            victims.append(slot)
                    # Do NOT discard active keys yet.
                    # Do NOT notify capacity yet.

            for slot in victims:
                driver_to_close: Any | None = None
                async with slot.meta_lock:
                    driver_to_close = slot.driver
                    slot.driver = None

                if driver_to_close is not None:
                    try:
                        await driver_to_close.close()
                    except Exception:
                        logger.exception("Error closing idle driver")

                async with self._pool_lock:
                    async with slot.meta_lock:
                        slot.in_flight = 0
                        slot.closing = True
                    if self._slots.get(slot.session_key) is slot:
                        del self._slots[slot.session_key]
                    self._active_keys.discard(slot.session_key)
                    self._capacity_available.notify_all()

    async def close_all(self) -> None:
        """Shutdown: stop new creation, wake waiters, drain in-flight, close all."""
        async with self._pool_lock:
            self._shutting_down = True
            slots = [
                self._slots[key]
                for key in list(self._active_keys)
                if key in self._slots
            ]
            self._capacity_available.notify_all()

        # Cancel the sweeper.
        if self._sweep_task is not None and not self._sweep_task.done():
            self._sweep_task.cancel()
            try:
                await self._sweep_task
            except asyncio.CancelledError:
                pass

        async def _close_one(slot: DriverSlot) -> None:
            # Wait for materialization to complete (or timeout).
            if not slot.ready_event.is_set():
                try:
                    await asyncio.wait_for(slot.ready_event.wait(), timeout=10.0)
                except TimeoutError:
                    logger.warning(
                        "shutdown proceeding without waiting for materialize: %s",
                        slot.session_key,
                    )
                    return

            # Drain in-flight (best-effort).
            drained = False
            for _ in range(100):
                async with slot.meta_lock:
                    if slot.in_flight == 0:
                        drained = True
                        break
                await asyncio.sleep(0.1)

            if not drained:
                async with slot.meta_lock:
                    current_in_flight = slot.in_flight
                logger.warning(
                    "MCP pool shutdown forcing close of driver for session %s "
                    "with in_flight=%d; request may see CDP error",
                    slot.session_key,
                    current_in_flight,
                )

            driver_to_close: Any | None = None
            async with slot.meta_lock:
                driver_to_close = slot.driver
                slot.driver = None
                slot.closing = True
                slot.in_flight = 0

            if driver_to_close is not None:
                try:
                    await driver_to_close.close()
                except Exception:
                    logger.exception("Error closing driver during shutdown")

            async with self._pool_lock:
                if self._slots.get(slot.session_key) is slot:
                    del self._slots[slot.session_key]
                self._active_keys.discard(slot.session_key)
                self._capacity_available.notify_all()

        await asyncio.gather(*(_close_one(slot) for slot in slots))

    def status(self) -> dict:
        """Return pool diagnostics (for /health or debugging)."""
        return {
            "enabled": True,
            "max_size": self._max_size,
            "active_keys": len(self._active_keys),
            "slots": {
                key: {
                    "session_key": s.session_key,
                    "has_driver": s.driver is not None,
                    "ready": s.ready_event.is_set(),
                    "closing": s.closing,
                    "in_flight": s.in_flight,
                    "created_at": s.created_at,
                    "last_used_at": s.last_used_at,
                }
                for key, s in self._slots.items()
            },
            "account_breaker_tripped": self._account_breaker.is_tripped(),
            "shutting_down": self._shutting_down,
        }
