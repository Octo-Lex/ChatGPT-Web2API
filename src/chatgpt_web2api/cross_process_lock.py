"""Cross-process async lock for serializing mutating operations.

The driver and its browser tab are shared resources — multiple MCP server
processes (one per ZCode agent session) and/or the REST server can all be
attached to the same Chrome tab on the same CDP port. The existing
``asyncio.Lock`` in each server is process-local and provides zero mutual
exclusion across processes.

This module provides an async context manager wrapping ``portalocker`` (a
cross-platform file-lock library) so all processes that share a Chrome
instance serialize their mutating operations (sends, creates, deletes).

The lock is keyed on the CDP port (``~/.chatgpt-web2api/cdp-{port}.lock``)
so multiple Chrome instances on different ports get independent locks.

Usage::

    async with CrossProcessLock(cdp_port=9222):
        # exclusive across all processes on this port
        await driver.send_and_stream(...)

Design notes:
  - Acquire/release are offloaded to ``asyncio.to_thread`` so the event loop
    is never blocked by the OS-level file lock.
  - A timeout on acquire (default 120s, matching the typical send timeout)
    prevents indefinite hangs when a prior holder crashed. On timeout, raises
    ``LockAcquisitionError`` which surfaces as a clean MCP/REST error.
  - ``portalocker.LOCK_SHARED`` is NOT used — this is an exclusive lock.
    Read-only operations run lock-free by design (concurrent reads are safe;
    only DOM-mutating operations need serialization).
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

import portalocker

logger = logging.getLogger(__name__)

# How long to wait for the lock before giving up. Matches the typical send
# timeout so a serialized caller doesn't time out on the lock faster than
# the operation itself would take.
_DEFAULT_TIMEOUT = 120


class LockAcquisitionError(RuntimeError):
    """Raised when the cross-process lock can't be acquired within the timeout.

    Surfaces to MCP as a CallToolResult(isError=True) and to REST as a 503 —
    the caller should retry, not treat it as a permanent failure.
    """


class CrossProcessLock:
    """Async context manager providing cross-process mutual exclusion.

    Wraps ``portalocker`` with ``asyncio.to_thread`` so the event loop is
    never blocked by the file-lock acquire/release.
    """

    def __init__(
        self,
        cdp_port: int = 9222,
        timeout: float = _DEFAULT_TIMEOUT,
    ) -> None:
        self._lockfile_path = str(
            Path.home() / ".chatgpt-web2api" / f"cdp-{cdp_port}.lock"
        )
        self._timeout = timeout
        self._fh = None  # file handle, held while locked

    async def __aenter__(self) -> "CrossProcessLock":
        # Ensure the directory exists
        os.makedirs(os.path.dirname(self._lockfile_path), exist_ok=True)

        def _acquire():
            fh = open(self._lockfile_path, "a")
            try:
                portalocker.lock(fh, portalocker.LOCK_EX | portalocker.LOCK_NB)
                return fh
            except portalocker.LockException:
                fh.close()
                return None

        # Try non-blocking first (fast path — uncontended lock)
        self._fh = await asyncio.to_thread(_acquire)
        if self._fh is not None:
            return self

        # Contended — poll with blocking acquire in a thread
        deadline = asyncio.get_event_loop().time() + self._timeout

        def _acquire_blocking():
            fh = open(self._lockfile_path, "a")
            portalocker.lock(fh, portalocker.LOCK_EX)
            return fh

        while self._fh is None:
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                raise LockAcquisitionError(
                    f"Could not acquire cross-process lock at "
                    f"{self._lockfile_path} within {self._timeout}s — "
                    f"another process is holding it."
                )
            try:
                self._fh = await asyncio.wait_for(
                    asyncio.to_thread(_acquire_blocking),
                    timeout=remaining,
                )
            except (asyncio.TimeoutError, portalocker.LockException):
                continue

        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        if self._fh is not None:
            def _release():
                try:
                    portalocker.unlock(self._fh)
                except Exception:
                    pass
                finally:
                    self._fh.close()

            await asyncio.to_thread(_release)
            self._fh = None
