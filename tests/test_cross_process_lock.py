"""Tests for the cross-process lock (#2/#9/#10/#21).

Verifies that the CrossProcessLock serializes concurrent access, times out
cleanly, and is transparent when uncontested (single-process operation).
"""

import asyncio
import os
import tempfile

import pytest

from chatgpt_web2api.cross_process_lock import CrossProcessLock, LockAcquisitionError


@pytest.mark.asyncio
async def test_uncontended_lock_acquires_immediately():
    """Single-process: lock acquires instantly when uncontested."""
    with tempfile.TemporaryDirectory() as tmp:
        lock = CrossProcessLock.__new__(CrossProcessLock)
        lock._lockfile_path = os.path.join(tmp, "test.lock")
        lock._timeout = 5
        lock._fh = None
        async with lock:
            assert lock._fh is not None
        assert lock._fh is None  # released


@pytest.mark.asyncio
async def test_contended_lock_serializes():
    """Two concurrent lock acquisitions on the same file serialize —
    the second waits for the first to release."""
    with tempfile.TemporaryDirectory() as tmp:
        lockfile = os.path.join(tmp, "test.lock")
        order = []

        def make_lock():
            lock = CrossProcessLock.__new__(CrossProcessLock)
            lock._lockfile_path = lockfile
            lock._timeout = 10
            lock._fh = None
            return lock

        async def worker(name: str, delay: float):
            async with make_lock():
                order.append(f"{name}-acquired")
                await asyncio.sleep(delay)
                order.append(f"{name}-released")

        # Run two workers concurrently; the second must wait for the first
        await asyncio.gather(worker("A", 0.3), worker("B", 0.1))

        # They must be serialized: A-acquired → A-released → B-acquired → B-released
        # (or B first, but no interleaving)
        assert order in (
            ["A-acquired", "A-released", "B-acquired", "B-released"],
            ["B-acquired", "B-released", "A-acquired", "A-released"],
        ), f"not serialized: {order}"


@pytest.mark.asyncio
async def test_lock_timeout_raises_acquisition_error():
    """When the lock can't be acquired within the timeout, raises
    LockAcquisitionError (which surfaces as a clean MCP/REST error)."""
    import shutil
    tmp = tempfile.mkdtemp()
    try:
        lockfile = os.path.join(tmp, "test.lock")

        def make_lock(timeout=1.0):
            lock = CrossProcessLock.__new__(CrossProcessLock)
            lock._lockfile_path = lockfile
            lock._timeout = timeout
            lock._fh = None
            return lock

        # Hold the lock open with a raw portalocker
        import portalocker
        fh = open(lockfile, "a")
        portalocker.lock(fh, portalocker.LOCK_EX)

        try:
            with pytest.raises(LockAcquisitionError):
                async with make_lock(timeout=1.0):
                    pass
        finally:
            portalocker.unlock(fh)
            fh.close()
            await asyncio.sleep(0.2)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)  # Windows: ignore locked-file errors


@pytest.mark.asyncio
async def test_lock_releases_on_exception():
    """If the critical section raises, the lock must still be released
    so subsequent callers aren't blocked."""
    with tempfile.TemporaryDirectory() as tmp:
        lockfile = os.path.join(tmp, "test.lock")

        def make_lock():
            lock = CrossProcessLock.__new__(CrossProcessLock)
            lock._lockfile_path = lockfile
            lock._timeout = 5
            lock._fh = None
            return lock

        # First acquisition raises
        with pytest.raises(ValueError):
            async with make_lock():
                raise ValueError("boom")

        # Second acquisition must succeed (lock was released)
        async with make_lock():
            pass  # no error = lock acquired successfully
