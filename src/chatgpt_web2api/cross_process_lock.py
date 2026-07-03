"""Cross-process async lock for serializing mutating operations.

The driver and its browser tab are shared resources — multiple MCP server
processes (one per ZCode agent session) and/or the REST server can all be
attached to the same Chrome tab on the same CDP port. The existing
``asyncio.Lock`` in each server is process-local and provides zero mutual
exclusion across processes.

This module provides an async context manager wrapping ``portalocker`` (a
cross-platform file-lock library) so all processes that share a Chrome
instance serialize their mutating operations (sends, creates, deletes).

The lock is keyed on the CDP port plus an optional ``lock_key`` suffix
(``~/.chatgpt-web2api/cdp-{port}[-{key}].lock``) so multiple Chrome instances
on different ports get independent locks, and so different lock *purposes*
within one Chrome get independent locks too. The two purposes today:

  - mutation locks: ``lock_key=None`` (legacy port-wide) or
    ``lock_key="target-{targetId}"`` (per-tab, parallel mode)
  - lifecycle election: ``lock_key="chrome-launch"`` (cold-start launch race)

Usage::

    async with CrossProcessLock(cdp_port=9222):
        # exclusive across all processes on this port (legacy behavior)
        await driver.send_and_stream(...)

    async with CrossProcessLock(cdp_port=9222, lock_key=target_lock_key(tid)):
        # exclusive per-tab — other tabs on the same Chrome run in parallel

Design notes:
  - Acquire uses a **non-blocking poll loop** (``LOCK_EX | LOCK_NB`` +
    ``await asyncio.sleep``), never a blocking ``to_thread`` call. The
    previous implementation wrapped a blocking ``portalocker.lock(LOCK_EX)``
    in ``asyncio.to_thread`` + ``asyncio.wait_for``; on timeout the worker
    thread was not killed and could acquire the OS lock *after* the coroutine
    had given up. The poll loop has deterministic cancellation semantics —
    no leaked background thread can grab the lock post-abandonment.
  - A timeout on acquire (default 120s, matching the typical send timeout)
    prevents indefinite hangs when a prior holder crashed. On timeout, raises
    ``LockAcquisitionError`` which surfaces as a clean MCP/REST error.
  - ``portalocker.LOCK_SHARED`` is NOT used — this is an exclusive lock.
    Read-only operations run lock-free by design (concurrent reads are safe;
    only DOM-mutating operations need serialization).
  - ``lock_key`` is validated against a safe charset to prevent path
    traversal / separator injection in the lock filename.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from pathlib import Path

import portalocker

logger = logging.getLogger(__name__)

# How long to wait for the lock before giving up. Matches the typical send
# timeout so a serialized caller doesn't time out on the lock faster than
# the operation itself would take.
_DEFAULT_TIMEOUT = 120

# Poll interval for the non-blocking acquire loop. Small enough that an
# uncontended-after-contention lock is picked up quickly, large enough that
# we don't busy-spin. Pure ``asyncio.sleep`` — yields the event loop, no
# thread created.
_POLL_INTERVAL = 0.1

# Safe charset for a lock_key suffix. Chrome target IDs are hex-ish strings
# and our own keys ("chrome-launch", "target-{id}") are ASCII; this rejects
# path separators, whitespace, traversal sequences, and anything else that
# could corrupt the lock filename.
_SAFE_LOCK_KEY = re.compile(r"^[A-Za-z0-9_.=-]+$")


class LockAcquisitionError(RuntimeError):
    """Raised when the cross-process lock can't be acquired within the timeout.

    Surfaces to MCP as a CallToolResult(isError=True) and to REST as a 503 —
    the caller should retry, not treat it as a permanent failure.
    """


def _validate_lock_key(lock_key: str | None) -> None:
    """Reject a lock_key that could corrupt the lock filename.

    ``None`` means "no suffix" (legacy port-wide lock) and is always valid.
    Any non-None value must match the safe charset — no path separators,
    whitespace, or ``..`` traversal.
    """
    if lock_key is None:
        return
    if not _SAFE_LOCK_KEY.fullmatch(lock_key):
        raise ValueError(
            f"Unsafe lock_key {lock_key!r}: must match {_SAFE_LOCK_KEY.pattern}"
        )


def target_lock_key(target_id: str) -> str:
    """Build the ``lock_key`` suffix for a per-tab mutation lock.

    Combined with the port in ``CrossProcessLock``, this yields a distinct
    lockfile per owned tab (``cdp-{port}-target-{targetId}.lock``) so two
    processes on different tabs of the same Chrome run in parallel while two
    processes on the *same* tab still serialize.
    """
    return f"target-{target_id}"


def chrome_launch_lock_key() -> str:
    """Build the ``lock_key`` suffix for the cold-start launch-election lock.

    A fixed key (``cdp-{port}-chrome-launch.lock``) distinct from every
    mutation lock, so lifecycle transitions never contend with sends. Held
    only around the ``check → launch → wait-for-cdp`` transition in
    ``ensure_running``/``restart``.
    """
    return "chrome-launch"


class CrossProcessLock:
    """Async context manager providing cross-process mutual exclusion.

    Wraps ``portalocker`` with a non-blocking poll loop so the event loop is
    never blocked by, and never leaks a thread for, the OS-level file lock.

    ``lock_key`` optionally appends a suffix to the lock filename. ``None``
    reproduces the legacy port-wide lock (``cdp-{port}.lock``) exactly; a
    non-None value produces ``cdp-{port}-{key}.lock``.
    """

    def __init__(
        self,
        cdp_port: int = 9222,
        timeout: float = _DEFAULT_TIMEOUT,
        lock_key: str | None = None,
    ) -> None:
        _validate_lock_key(lock_key)
        suffix = f"-{lock_key}" if lock_key else ""
        self._lockfile_path = str(
            Path.home() / ".chatgpt-web2api" / f"cdp-{cdp_port}{suffix}.lock"
        )
        self._timeout = timeout
        self._fh = None  # file handle, held while locked

    async def __aenter__(self) -> CrossProcessLock:
        # Ensure the directory exists
        os.makedirs(os.path.dirname(self._lockfile_path), exist_ok=True)

        loop = asyncio.get_event_loop()
        deadline = loop.time() + self._timeout

        # Non-blocking poll loop. Each attempt is a LOCK_EX | LOCK_NB try —
        # instant return, no thread, no cancellation leak. On contention we
        # sleep on the event loop and retry until the deadline.
        while True:
            fh = open(self._lockfile_path, "a")
            try:
                portalocker.lock(fh, portalocker.LOCK_EX | portalocker.LOCK_NB)
                self._fh = fh  # acquired
                return self
            except portalocker.LockException:
                fh.close()

            remaining = deadline - loop.time()
            if remaining <= 0:
                raise LockAcquisitionError(
                    f"Could not acquire cross-process lock at "
                    f"{self._lockfile_path} within {self._timeout}s — "
                    f"another process is holding it."
                )
            await asyncio.sleep(min(_POLL_INTERVAL, remaining))

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        if self._fh is not None:
            fh, self._fh = self._fh, None
            # Release off-thread only if it could block; portalocker.unlock +
            # close are fast on all platforms, but keep the off-thread shape
            # to guarantee the event loop is never wedged by a slow release.
            def _release():
                try:
                    portalocker.unlock(fh)
                except Exception:
                    pass
                finally:
                    fh.close()

            await asyncio.to_thread(_release)
