"""Per-target mutation locking + lock-scope resolution (PR3/5).

This module provides the machinery for parallel multi-tab traffic on one Chrome
instance. It is **inert until PR4 wires it**: nothing here is called from the
REST/MCP send paths yet. PR3 lands the pieces so PR4 can flip the switch in a
single, reviewable change.

Two concerns live here:

1. **`MutationLock`** — composes a *process-local* ``asyncio.Lock`` with the
   *cross-process* ``CrossProcessLock``. Acquire order is process-local first,
   file lock second; release is the reverse. The process-local lock is required
   (not YAGNI) because OS file locks do not serialize reentrant acquisition by
   two coroutines in the *same* process, and the REST server can have multiple
   concurrent coroutines on the same driver/target.

2. **`resolve_mutation_lock`** — picks ``(cdp_port, lock_key)`` for a mutating
   operation based on ``parallel_tabs`` mode and the driver's owned-target
   state. In parallel mode it RAISES rather than falls back to the port lock,
   because a port lock and a target lock are different files that do not
   exclude each other — falling back would reintroduce the split-brain regime
   the ``parallel_tabs`` bundle was designed to eliminate.

Lock-nesting invariant (asserted by the design, enforced by call-site
discipline in PR4): a ``MutationLock`` and a ``chrome-launch`` election lock
are never held simultaneously. Lifecycle never sends; sends never touch
lifecycle.
"""

from __future__ import annotations

import asyncio
import threading
from typing import TYPE_CHECKING

from .cross_process_lock import CrossProcessLock, target_lock_key

if TYPE_CHECKING:
    from .cdp_driver import CDPDriver


class OwnedTabRequiredError(RuntimeError):
    """Raised when parallel mode requires an owned tab and none is available.

    Parallel mode (``parallel_tabs=true``) requires each bridge process to
    drive a dedicated, owned tab target so per-target locking can serialize
    DOM mutation correctly. If the driver has no owned target (adopt/fallback
    path, or owned-tab creation failed), this is raised rather than silently
    degrading to the port-wide lock — a port lock and a target lock are
    independent files that do not exclude each other, so mixing them would
    reintroduce the split-brain the parallel bundle eliminates. Surfaces to
    MCP as a CallToolResult(isError=True) and to REST as a 503 — the caller
    should retry, not treat it as permanent.
    """


# ── Process-local lock registry ──────────────────────────────────────────
#
# Keyed by (cdp_port, lock_key) — the same identity the cross-process file
# lock uses. Two coroutines in the SAME process on the same target serialize
# here regardless of OS file-lock reentrance; two coroutines on DIFFERENT
# targets get distinct asyncio.Lock objects and run in parallel. The guard is
# a threading.Lock only to make registry initialization safe if multiple
# threads ever construct locks concurrently; under the single-event-loop
# runtime it is effectively uncontended.
_PROC_LOCKS: dict[tuple[int, str | None], asyncio.Lock] = {}
_PROC_LOCKS_GUARD = threading.Lock()


def _proc_lock_for(cdp_port: int, lock_key: str | None) -> asyncio.Lock:
    """Return the process-local asyncio.Lock for a given (port, key).

    Lazily creates and caches. Binds to the running event loop on first
    ``acquire()`` (Python 3.10+ ``asyncio.Lock`` binds lazily), which is safe
    under the project's single-loop runtime. If a future change runs locks
    across multiple event loops in one process, key by
    ``(id(asyncio.get_running_loop()), cdp_port, lock_key)`` instead.
    """
    ident = (cdp_port, lock_key)
    with _PROC_LOCKS_GUARD:
        lock = _PROC_LOCKS.get(ident)
        if lock is None:
            lock = asyncio.Lock()
            _PROC_LOCKS[ident] = lock
        return lock


class MutationLock:
    """Async CM: process-local asyncio.Lock (first) + CrossProcessLock (second).

    Contract::

        same process + same target  → serialized by the asyncio.Lock
        different process + same target → serialized by the file lock
        different target → parallel (distinct asyncio.Lock + distinct file)

    Acquire order is process-local first, file lock second. Release order is
    the reverse (file first, process-local second). Never acquire in the
    opposite order elsewhere — the chrome-launch election lock and a
    MutationLock must never be held at the same time.
    """

    def __init__(self, cdp_port: int, lock_key: str | None = None) -> None:
        self._proc_lock = _proc_lock_for(cdp_port, lock_key)
        self._file_lock = CrossProcessLock(cdp_port=cdp_port, lock_key=lock_key)

    async def __aenter__(self) -> MutationLock:
        await self._proc_lock.acquire()
        try:
            await self._file_lock.__aenter__()
        except BaseException:
            # File-lock acquire failed OR the coroutine was cancelled while
            # waiting (asyncio.CancelledError is a BaseException, not an
            # Exception, since 3.8 — `except Exception` would miss it). Release
            # the proc lock so another coroutine on this target isn't blocked.
            self._proc_lock.release()
            raise
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        # Release file first, then process-local — strict reverse of acquire.
        try:
            await self._file_lock.__aexit__(exc_type, exc, tb)
        finally:
            self._proc_lock.release()


def resolve_mutation_lock(
    driver: CDPDriver, parallel_tabs: bool
) -> tuple[int, str | None]:
    """Return ``(cdp_port, lock_key)`` for a DOM-mutating operation.

    - ``parallel_tabs=False`` (default): always the legacy port-wide lock
      (``lock_key=None`` → ``cdp-{port}.lock``). No behavior change.
    - ``parallel_tabs=True`` + owned target: per-target lock
      (``target-{targetId}`` → ``cdp-{port}-target-{id}.lock``).
    - ``parallel_tabs=True`` + no owned target: **raise**, do NOT fall back to
      the port lock. See ``OwnedTabRequiredError`` docstring for why.
    """
    port = driver.port
    if not parallel_tabs:
        return port, None

    target_id = driver.target_id
    if driver.has_owned_target and target_id is not None:
        # target_lock_key builds the validated "target-{id}" suffix.
        return port, target_lock_key(target_id)

    raise OwnedTabRequiredError(
        "parallel_tabs=true requires an owned tab target, but the driver has "
        f"none (tab_mode={driver.tab_mode!r}, owns_target={driver.owns_target}). "
        "Refusing to fall back to the port-wide lock — that would mix lock "
        "files and reintroduce split-brain. Ensure tab_mode=owned and the tab "
        "was created successfully."
    )
