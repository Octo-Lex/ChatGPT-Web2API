"""Tests for PR3/5 ``lock_resolver``: MutationLock + resolver + driver properties.

PR3 is purely additive machinery — nothing is wired into REST/MCP yet (PR4
wires it). These tests cover:

- ``CDPDriver`` read-only properties (``target_id``/``owns_target``/
  ``has_owned_target``) backed by the private fields.
- ``resolve_mutation_lock``: the three branches (port lock when parallel_tabs
  is off; target lock when on + owned; RAISE when on + no owned target).
- ``MutationLock``: process-local serialization (same target blocks), parallel
  across targets, correct acquire/release ordering, and cleanup on file-lock
  failure.

The ``CrossProcessLock`` component uses the ``__new__`` bypass + tmpdir idiom
from ``test_cross_process_lock.py`` so no real home-dir lockfile is touched.
The suite runs on one session-scoped event loop (conftest.py), so
``asyncio.Lock()`` works across tests with no special handling.
"""

import asyncio

import pytest

from chatgpt_web2api import lock_resolver as lr_mod
from chatgpt_web2api.cdp_driver import CDPDriver
from chatgpt_web2api.cross_process_lock import CrossProcessLock
from chatgpt_web2api.lock_resolver import (
    MutationLock,
    OwnedTabRequiredError,
    _proc_lock_for,
    resolve_mutation_lock,
)

# ── Driver properties ────────────────────────────────────────────────────


def test_driver_properties_default_false():
    """A fresh driver (no connect) reports no owned target."""
    d = CDPDriver(cdp_port=9222)
    assert d.target_id is None
    assert d.owns_target is False
    assert d.has_owned_target is False


def test_driver_properties_reflect_owned_state():
    """Mutating the backing fields flips the properties (owned case)."""
    d = CDPDriver(cdp_port=9222)
    d._target_id = "ABC123"
    d._owns_target = True
    assert d.target_id == "ABC123"
    assert d.owns_target is True
    assert d.has_owned_target is True


def test_has_owned_target_requires_all_three():
    """has_owned_target is False unless tab_mode=owned AND owns AND target_id."""
    d = CDPDriver(cdp_port=9222)
    # owns_target + target_id but tab_mode != owned → False
    d._target_id = "ABC"
    d._owns_target = True
    d.tab_mode = "adopt"
    assert d.has_owned_target is False
    # tab_mode=owned + owns_target but no target_id → False
    d.tab_mode = "owned"
    d._target_id = None
    assert d.has_owned_target is False
    # tab_mode=owned + target_id but not owns_target → False
    d._target_id = "ABC"
    d._owns_target = False
    assert d.has_owned_target is False


def test_properties_are_read_only():
    """The properties must not be settable (no setter defined)."""
    d = CDPDriver(cdp_port=9222)
    for prop in ("target_id", "owns_target", "has_owned_target"):
        with pytest.raises(AttributeError):
            setattr(d, prop, "x")


# ── resolve_mutation_lock ────────────────────────────────────────────────


def test_resolver_off_returns_port_lock():
    """parallel_tabs=False → (port, None) = legacy port-wide lock."""
    d = CDPDriver(cdp_port=9222)
    port, key = resolve_mutation_lock(d, parallel_tabs=False)
    assert port == 9222
    assert key is None


def test_resolver_on_owned_returns_target_lock():
    """parallel_tabs=True + owned target → (port, 'target-{id}')."""
    d = CDPDriver(cdp_port=9222)
    d._target_id = "ABC123"
    d._owns_target = True
    port, key = resolve_mutation_lock(d, parallel_tabs=True)
    assert port == 9222
    assert key == "target-ABC123"


def test_resolver_on_no_owned_raises():
    """parallel_tabs=True + no owned target → RAISE, never fall back to port.

    This is the split-brain guard: a port lock and a target lock are different
    files and do not exclude each other, so falling back would reintroduce the
    mixed-lock regime the bundle eliminates.
    """
    d = CDPDriver(cdp_port=9222)  # no owned target
    with pytest.raises(OwnedTabRequiredError):
        resolve_mutation_lock(d, parallel_tabs=True)


def test_resolver_on_adopt_mode_raises():
    """Even with a target_id, adopt mode (owns_target=False) raises in parallel."""
    d = CDPDriver(cdp_port=9222)
    d._target_id = "ADOPTED"
    d._owns_target = False
    d.tab_mode = "adopt"
    with pytest.raises(OwnedTabRequiredError):
        resolve_mutation_lock(d, parallel_tabs=True)


# ── _proc_lock_for registry ──────────────────────────────────────────────


def test_proc_lock_registry_caches_by_identity():
    """Same (port, key) → same asyncio.Lock; different identity → different."""
    a = _proc_lock_for(9222, "target-A")
    b = _proc_lock_for(9222, "target-A")
    c = _proc_lock_for(9222, "target-B")
    d = _proc_lock_for(9222, None)
    assert a is b, "same identity must return the cached lock"
    assert a is not c, "different key → different lock"
    assert a is not d, "key=None (port lock) is its own identity"


# ── MutationLock: process-local serialization ────────────────────────────


@pytest.mark.asyncio
async def test_mutation_lock_serializes_same_target(monkeypatch, tmp_path):
    """Two concurrent MutationLocks on the SAME (port, key) serialize via the
    process-local asyncio.Lock — even with the file lock nulled out."""
    # Null the file-lock component so we isolate the process-local behavior.
    monkeypatch.setattr(lr_mod, "CrossProcessLock", _NullFileLock)
    m1 = MutationLock(cdp_port=9222, lock_key="target-A")
    m1._file_lock = _NullFileLock()  # type: ignore[attr-defined]
    m2 = MutationLock(cdp_port=9222, lock_key="target-A")
    m2._file_lock = _NullFileLock()  # type: ignore[attr-defined]

    order: list[str] = []

    async def worker(name: str, delay: float):
        async with MutationLock(cdp_port=9222, lock_key="target-A"):
            order.append(f"{name}-in")
            await asyncio.sleep(delay)
            order.append(f"{name}-out")

    await asyncio.gather(worker("A", 0.1), worker("B", 0.1))

    # Serialized: no interleaving.
    assert order in (
        ["A-in", "A-out", "B-in", "B-out"],
        ["B-in", "B-out", "A-in", "A-out"],
    ), f"not serialized: {order}"


@pytest.mark.asyncio
async def test_mutation_lock_parallel_across_targets():
    """Two MutationLocks on DIFFERENT targets run in parallel (distinct locks)."""
    order: list[str] = []

    async def worker(name: str, key: str):
        async with MutationLock(cdp_port=9222, lock_key=key):
            order.append(f"{name}-in")
            await asyncio.sleep(0.1)
            order.append(f"{name}-out")

    # Different keys → different asyncio.Lock → interleaved (parallel).
    await asyncio.gather(worker("A", "target-X"), worker("B", "target-Y"))

    # Parallel: A enters before B exits (or vice versa).
    assert order[0].endswith("-in")
    assert order[1].endswith("-in"), f"not parallel: {order}"


# ── MutationLock: acquire/release ordering ───────────────────────────────


@pytest.mark.asyncio
async def test_mutation_lock_release_order_file_then_proc(tmp_path):
    """On exit, the file lock is released BEFORE the process-local lock.

    Verified by recording the order of release hooks. The strict reverse of
    acquire (proc first, file second) prevents a brief window where another
    coroutine could grab the proc lock while a foreign process still sees the
    file as held.
    """
    events: list[str] = []

    class _RecordingFileLock:
        async def __aenter__(self):
            events.append("file-acq")
            return self

        async def __aexit__(self, *a):
            events.append("file-rel")

    m = MutationLock(cdp_port=9223, lock_key="target-REC")
    m._file_lock = _RecordingFileLock()  # type: ignore[attr-defined]

    async with m:
        events.append("body")

    # Acquire: proc (implicit, no event) → file. Release: file → proc (implicit).
    assert events == ["file-acq", "body", "file-rel"], events


@pytest.mark.asyncio
async def test_mutation_lock_proc_released_on_file_failure():
    """If the file-lock acquire raises, the proc lock is released (no deadlock).

    Without the try/except in __aenter__, a file-lock timeout would leave the
    process-local asyncio.Lock held forever, blocking every other coroutine on
    that target.
    """
    call_count = {"n": 0}

    class _FailingFileLock:
        async def __aenter__(self):
            raise RuntimeError("file lock boom")

        async def __aexit__(self, *a):
            pass

    m = MutationLock(cdp_port=9224, lock_key="target-FAIL")
    m._file_lock = _FailingFileLock()  # type: ignore[attr-defined]

    with pytest.raises(RuntimeError, match="file lock boom"):
        async with m:
            call_count["n"] += 1  # should never run

    assert call_count["n"] == 0, "body must not run when file lock fails"

    # The proc lock must be releasable now — a second acquire must not block.
    proc = _proc_lock_for(9224, "target-FAIL")
    assert proc.locked() is False, "proc lock leaked after file-lock failure"


# ── MutationLock: end-to-end with a real file lock ───────────────────────


@pytest.mark.asyncio
async def test_mutation_lock_works_with_real_cross_process_lock(tmp_path):
    """MutationLock composing a REAL CrossProcessLock (tmpdir) acquires and
    releases cleanly. Confirms the wrapper delegates correctly."""
    lockfile = str(tmp_path / "cdp-9225-target-E2E.lock")

    # Build a CrossProcessLock via the __new__ bypass (no home-dir path).
    real = CrossProcessLock.__new__(CrossProcessLock)
    real._lockfile_path = lockfile
    real._timeout = 5
    real._fh = None

    m = MutationLock(cdp_port=9225, lock_key="target-E2E")
    m._file_lock = real  # type: ignore[attr-defined]

    async with m:
        assert real._fh is not None, "file lock should be held"
    assert real._fh is None, "file lock released on exit"


# ── Helpers ──────────────────────────────────────────────────────────────


class _NullFileLock:
    """No-op async CM stand-in for CrossProcessLock (isolates proc-lock tests)."""

    def __init__(self, *a, **kw):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False
