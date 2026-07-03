"""Tests for the PR1 CrossProcessLock generalization: ``lock_key`` suffix,
key validation, the non-blocking poll loop, and the helper builders.

These complement ``test_cross_process_lock.py`` (which exercises the
serialization/timeout/release semantics via ``__new__`` bypass). Here we
focus on what PR1 added.
"""

import asyncio
import os
import shutil
import tempfile

import portalocker
import pytest

from chatgpt_web2api.cross_process_lock import (
    CrossProcessLock,
    LockAcquisitionError,
    _validate_lock_key,
    chrome_launch_lock_key,
    target_lock_key,
)

# ── lock_key → filename ────────────────────────────────────────────────


def test_lock_key_none_is_legacy_filename():
    """``lock_key=None`` reproduces the exact legacy port-wide filename."""
    lock = CrossProcessLock(cdp_port=9222)
    assert lock._lockfile_path.endswith("cdp-9222.lock")
    assert "-target-" not in lock._lockfile_path
    assert "-chrome-launch" not in lock._lockfile_path


def test_lock_key_suffix_appears_in_filename():
    """A non-None key is appended as ``-{key}`` before ``.lock``."""
    lock = CrossProcessLock(cdp_port=9222, lock_key=target_lock_key("ABC123"))
    assert lock._lockfile_path.endswith("cdp-9222-target-ABC123.lock")


def test_distinct_keys_yield_distinct_files():
    """Two different keys on the same port map to different lockfiles —
    the basis for per-target parallelism and lifecycle/mutation isolation."""
    a = CrossProcessLock(cdp_port=9222, lock_key=target_lock_key("AAA"))
    b = CrossProcessLock(cdp_port=9222, lock_key=target_lock_key("BBB"))
    c = CrossProcessLock(cdp_port=9222, lock_key=chrome_launch_lock_key())
    d = CrossProcessLock(cdp_port=9222)  # legacy port-wide
    paths = {a._lockfile_path, b._lockfile_path, c._lockfile_path, d._lockfile_path}
    assert len(paths) == 4, f"expected 4 distinct lockfiles, got {paths}"


def test_distinct_ports_yield_distinct_files():
    """Different ports stay isolated regardless of key — preserves the
    pre-existing multi-Chrome scaling path."""
    p1 = CrossProcessLock(cdp_port=9222, lock_key=target_lock_key("X"))
    p2 = CrossProcessLock(cdp_port=9223, lock_key=target_lock_key("X"))
    assert p1._lockfile_path != p2._lockfile_path


# ── key validation ────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "key",
    [
        None,                   # no suffix = always valid
        "chrome-launch",
        "target-ABC123",
        "target-1a2b3c",
        "a_b.c-d=e",
    ],
)
def test_validate_lock_key_accepts_safe(key):
    _validate_lock_key(key)  # must not raise


@pytest.mark.parametrize(
    "key",
    [
        "target ABC",           # whitespace
        "../etc/passwd",        # traversal
        "target/x",             # path separator
        "target\\x",            # backslash separator
        "",                     # empty string is NOT None
        "target tab",           # internal space
    ],
)
def test_validate_lock_key_rejects_unsafe(key):
    with pytest.raises(ValueError):
        _validate_lock_key(key)


def test_constructor_rejects_unsafe_key():
    """Validation happens at construction, not at acquire time."""
    with pytest.raises(ValueError):
        CrossProcessLock(cdp_port=9222, lock_key="../evil")


# ── helper builders ───────────────────────────────────────────────────


def test_target_lock_key_format():
    assert target_lock_key("ABC") == "target-ABC"


def test_chrome_launch_lock_key_is_constant():
    assert chrome_launch_lock_key() == "chrome-launch"


def test_helpers_are_safe_keys():
    """The keys our own helpers produce must pass validation."""
    _validate_lock_key(target_lock_key("any-target-id"))
    _validate_lock_key(chrome_launch_lock_key())


# ── different keys do NOT exclude each other (the split-brain property) ─
# This is the core fact that forces ``parallel_tabs`` to bundle target-lock
# with fail-closed owned-tab enforcement: a port-wide lock and a target lock
# are different files and therefore do not serialize against each other.


@pytest.mark.asyncio
async def test_different_keys_do_not_exclude():
    """A holder of ``cdp-{port}.lock`` does NOT block a holder of
    ``cdp-{port}-target-X.lock`` — they are independent files.

    This test documents the property (and is the regression guard for it):
    it passes today and must keep passing. Split-brain prevention lives in
    the config-discipline layer (``parallel_tabs`` bundle), not here.
    """
    with tempfile.TemporaryDirectory() as tmp:
        port_lock = CrossProcessLock.__new__(CrossProcessLock)
        port_lock._lockfile_path = os.path.join(tmp, "cdp-9222.lock")
        port_lock._timeout = 5
        port_lock._fh = None

        target_lock = CrossProcessLock.__new__(CrossProcessLock)
        target_lock._lockfile_path = os.path.join(tmp, "cdp-9222-target-X.lock")
        target_lock._timeout = 5
        target_lock._fh = None

        async with port_lock:
            # While the port-wide lock is held, the per-target lock must
            # acquire immediately (no exclusion between different keys).
            async with target_lock:
                pass  # acquired → no exclusion between keys


# ── same key DOES exclude (serialization within a key is unchanged) ────


@pytest.mark.asyncio
async def test_same_key_still_serializes():
    """Two acquisitions with the same ``lock_key`` serialize — the per-key
    mutual exclusion that parallel-mode target-locking relies on."""
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "cdp-9222-target-X.lock")
        order = []

        def make_lock():
            lock = CrossProcessLock.__new__(CrossProcessLock)
            lock._lockfile_path = path
            lock._timeout = 10
            lock._fh = None
            return lock

        async def worker(name: str, delay: float):
            async with make_lock():
                order.append(f"{name}-in")
                await asyncio.sleep(delay)
                order.append(f"{name}-out")

        await asyncio.gather(worker("A", 0.2), worker("B", 0.1))
        assert order in (
            ["A-in", "A-out", "B-in", "B-out"],
            ["B-in", "B-out", "A-in", "A-out"],
        ), f"not serialized within key: {order}"


# ── non-blocking poll loop: no leaked acquisition after timeout ────────
# The bug PR1 fixes: the old ``to_thread(LOCK_EX)`` + ``wait_for`` pattern
# could leave a blocking thread that acquired the OS lock AFTER the coroutine
# timed out. The LOCK_NB poll loop has no such thread.


@pytest.mark.asyncio
async def test_timeout_does_not_leak_acquisition():
    """A waiter that times out must not later acquire the lock in a
    background thread. After the timeout + holder release, a *fresh* lock
    on the same file must be acquirable, and the timed-out instance must
    not be holding it.

    This is the regression guard for the thread-leak fix.
    """
    tmp = tempfile.mkdtemp()
    try:
        path = os.path.join(tmp, "cdp-9222.lock")

        # Hold the lock externally so the waiter times out.
        holder = open(path, "a")
        portalocker.lock(holder, portalocker.LOCK_EX)

        waiter = CrossProcessLock.__new__(CrossProcessLock)
        waiter._lockfile_path = path
        waiter._timeout = 0.3
        waiter._fh = None

        timed_out_at = asyncio.get_event_loop().time()
        with pytest.raises(LockAcquisitionError):
            async with waiter:
                pass
        elapsed = asyncio.get_event_loop().time() - timed_out_at
        # Must have given up promptly (not blocked for the full default 120s).
        assert elapsed < 2.0, f"timeout took too long: {elapsed}s"

        # The timed-out waiter must NOT hold the file handle.
        assert waiter._fh is None

        # Release the external holder; the timed-out waiter must not have
        # grabbed the lock from a leaked background thread.
        portalocker.unlock(holder)
        holder.close()
        await asyncio.sleep(0.3)

        # A fresh lock on the same file must acquire cleanly. If the timed-out
        # waiter had leaked an acquisition, this would deadlock (and time out).
        fresh = CrossProcessLock.__new__(CrossProcessLock)
        fresh._lockfile_path = path
        fresh._timeout = 2.0
        fresh._fh = None
        async with fresh:
            pass  # acquired cleanly → no leaked holder
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
