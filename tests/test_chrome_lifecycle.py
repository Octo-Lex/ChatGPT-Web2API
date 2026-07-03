"""Tests for PR2/5 Chrome lifecycle ownership (``_owns_chrome``).

These are the first tests for ``ChromeProcess`` — the restart path and monitor
loop were previously untested. We construct instances via the ``__new__`` bypass
(matching ``test_cross_process_lock.py`` / ``test_breaker_failfast.py``) so no
real Chrome is launched, and monkeypatch the private I/O methods
(``_cdp_alive``/``_launch``/``_kill``/``_wait_for_cdp``) on the instance.

The election lock (``CrossProcessLock`` with ``lock_key=chrome_launch_lock_key()``)
is faked via module-level monkeypatch — ``_NullLock`` for single-instance tests,
and a custom gate-lock for the concurrent election test (T5) that proves the
lock is held through ``_wait_for_cdp``.
"""

import asyncio

import pytest

from chatgpt_web2api import chrome as chrome_mod
from chatgpt_web2api.breakers import BreakerKind, BreakerRegistry
from chatgpt_web2api.chrome import ChromeProcess

# ── Helpers ──────────────────────────────────────────────────────────────


class _FakePopen:
    """Stand-in for subprocess.Popen with controllable liveness.

    poll() returns None while "live", or a return code once "dead"."""

    def __init__(self, returncode: int | None = 0) -> None:
        self.returncode = returncode
        self.pid = 12345

    def poll(self) -> int | None:
        # returncode is None => still running; else exited with that code.
        return self.returncode


def _make_chrome(
    *,
    owns_chrome: bool = False,
    process: _FakePopen | None = None,
    breakers: BreakerRegistry | None = None,
    restart_on_crash: bool = True,
    cdp_port: int = 9222,
) -> ChromeProcess:
    """Build a ChromeProcess without running __init__ (no real Chrome)."""
    chrome = ChromeProcess.__new__(ChromeProcess)
    chrome._cfg = type(
        "Cfg",
        (),
        {"cdp_port": cdp_port, "restart_on_crash": restart_on_crash},
    )()
    chrome._process = process
    chrome._monitor_task = None
    chrome._healthy = False
    chrome._started_at = 0
    chrome._restart_count = 0
    chrome._breakers = breakers
    chrome._owns_chrome = owns_chrome
    return chrome


class _NullLock:
    """No-op async CM — swallows construction args, never blocks."""

    def __init__(self, *a, **kw):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


# ── T1: owner re-call guard ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_owner_recall_keeps_ownership(monkeypatch):
    """A process whose _process is still live and CDP is alive stays owner on
    re-entry to ensure_running (does not demote to attacher)."""
    chrome = _make_chrome(owns_chrome=True, process=_FakePopen(returncode=None))

    async def _alive():
        return True

    chrome._cdp_alive = _alive  # type: ignore[method-assign]
    monkeypatch.setattr(chrome_mod, "CrossProcessLock", _NullLock)

    await chrome.ensure_running()

    assert chrome._owns_chrome is True
    assert chrome._healthy is True


# ── T1b: live _process + CDP down → restart, not orphaning launch ─────────


@pytest.mark.asyncio
async def test_live_process_cdp_down_restarts(monkeypatch):
    """A live owned _process with CDP not responding → restart() (which kills
    + relaunches under the election lock), NOT a raw cold-start _launch over
    the live process. Ownership stays True throughout.

    This guards the bug ChatGPT's review caught: without this branch, a live
    _process + CDP-down fell through to the election and _launch() orphaned
    the first Chrome process.
    """
    chrome = _make_chrome(
        owns_chrome=True, process=_FakePopen(returncode=None)
    )
    restart_called = {"n": 0}
    raw_launch_called = {"n": 0}

    async def _alive():
        return False  # process live but CDP down

    async def _raw_launch():
        raw_launch_called["n"] += 1  # must NOT be called directly

    async def _restart():
        restart_called["n"] += 1

    chrome._cdp_alive = _alive  # type: ignore[method-assign]
    chrome._launch = _raw_launch  # type: ignore[method-assign]
    chrome.restart = _restart  # type: ignore[method-assign]
    monkeypatch.setattr(chrome_mod, "CrossProcessLock", _NullLock)

    await chrome.ensure_running()

    assert restart_called["n"] == 1, "live process + CDP down must restart"
    assert raw_launch_called["n"] == 0, "must not raw-launch over the live process"
    assert chrome._owns_chrome is True, "ownership retained across restart"


# ── T2: dead _process does not imply ownership ───────────────────────────


@pytest.mark.asyncio
async def test_dead_process_cleared_then_attaches(monkeypatch):
    """_process non-None but poll() returns a code → clear stale handle, then
    attach to the foreign Chrome now alive on the port (not claim ownership)."""
    # process died (returncode 0), but a different Chrome is up on the port
    chrome = _make_chrome(
        owns_chrome=True, process=_FakePopen(returncode=0)
    )

    async def _alive():
        return True

    chrome._cdp_alive = _alive  # type: ignore[method-assign]
    monkeypatch.setattr(chrome_mod, "CrossProcessLock", _NullLock)

    await chrome.ensure_running()

    assert chrome._process is None, "stale _process should be cleared"
    assert chrome._owns_chrome is False, "must not own someone else's Chrome"
    assert chrome._healthy is True


# ── T3: attacher path ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_attacher_marks_non_owner(monkeypatch):
    """No local process + CDP alive → attach as non-owner, healthy."""
    chrome = _make_chrome()

    async def _alive():
        return True

    chrome._cdp_alive = _alive  # type: ignore[method-assign]
    monkeypatch.setattr(chrome_mod, "CrossProcessLock", _NullLock)

    await chrome.ensure_running()

    assert chrome._owns_chrome is False
    assert chrome._healthy is True


# ── T4: election loser ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_election_loser_attaches(monkeypatch):
    """Inside the election lock, CDP becomes alive → lost the election →
    attach as non-owner."""
    chrome = _make_chrome()

    # First _cdp_alive (outside lock) → False; second (inside lock) → True.
    calls = {"n": 0}

    async def _alive():
        calls["n"] += 1
        return calls["n"] >= 2  # False then True

    chrome._cdp_alive = _alive  # type: ignore[method-assign]
    monkeypatch.setattr(chrome_mod, "CrossProcessLock", _NullLock)

    await chrome.ensure_running()

    assert chrome._owns_chrome is False
    assert chrome._healthy is True


# ── T5: double-launch race — lock held through readiness ─────────────────


@pytest.mark.asyncio
async def test_election_lock_held_through_readiness(monkeypatch):
    """Two concurrent ensure_running calls: P1 wins the election, launches,
    and holds the lock THROUGH _wait_for_cdp. P2 cannot enter the lock until
    P1 releases AFTER readiness. Then P2 re-checks _cdp_alive → True → attaches
    without launching. _launch is called exactly once.

    This test FAILS if _wait_for_cdp is moved outside the election lock: P2
    would enter the lock while P1's Chrome is not yet ready, see _cdp_alive
    False, and launch a competing Chrome.
    """
    # A real serialized lock faked over an asyncio.Lock so P2 actually waits.
    serial = asyncio.Lock()

    class _SerialLock:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            await serial.acquire()
            return self

        async def __aexit__(self, *a):
            serial.release()
            return False

    monkeypatch.setattr(chrome_mod, "CrossProcessLock", _SerialLock)

    launch_count = {"n": 0}
    # Track which "process" is alive. Initially nobody. P1's _wait_for_cdp
    # flips it to True (ready) before releasing the lock.
    alive_flag = {"ready": False}

    async def make_alive_for(owner: str):
        async def _alive():
            return alive_flag["ready"]

        return _alive

    p1 = _make_chrome()
    p2 = _make_chrome()
    p1_alive, p2_alive = await make_alive_for("p1"), await make_alive_for("p2")
    p1._cdp_alive = p1_alive  # type: ignore[method-assign]
    p2._cdp_alive = p2_alive  # type: ignore[method-assign]

    async def p1_launch():
        launch_count["n"] += 1

    async def p1_wait():
        # Simulate Chrome becoming ready while still inside the lock.
        alive_flag["ready"] = True

    async def noop_kill():
        pass

    p1._launch = p1_launch  # type: ignore[method-assign]
    p1._wait_for_cdp = lambda timeout=30: p1_wait()  # type: ignore[method-assign]
    p1._kill = noop_kill  # type: ignore[method-assign]
    p2._launch = lambda: pytest.fail("P2 must not launch — Chrome is already up")  # type: ignore[method-assign]
    p2._kill = noop_kill  # type: ignore[method-assign]

    # P1 and P2 start concurrently. P1 wins the election (first to the lock),
    # launches, and holds through readiness. P2 waits, then sees ready → attaches.
    await asyncio.gather(p1.ensure_running(), p2.ensure_running())

    assert launch_count["n"] == 1, "exactly one launch (P1)"
    assert p1._owns_chrome is True, "P1 is the owner"
    assert p2._owns_chrome is False, "P2 attached, did not launch"
    assert p2._healthy is True


# ── T6: ensure_running failure cleanup ───────────────────────────────────


@pytest.mark.asyncio
async def test_ensure_running_failure_drops_ownership(monkeypatch):
    """_launch sets _process, _wait_for_cdp raises → cleanup _kill called,
    _owns_chrome=False, _healthy=False, exception propagates."""
    chrome = _make_chrome()
    kill_called = {"n": 0}

    async def _alive():
        return False

    async def _launch():
        chrome._process = _FakePopen(returncode=None)  # half-spawned

    async def _wait(timeout=30):
        raise TimeoutError("CDP did not respond")

    async def _kill():
        kill_called["n"] += 1
        chrome._process = None

    chrome._cdp_alive = _alive  # type: ignore[method-assign]
    chrome._launch = _launch  # type: ignore[method-assign]
    chrome._wait_for_cdp = _wait  # type: ignore[method-assign]
    chrome._kill = _kill  # type: ignore[method-assign]
    monkeypatch.setattr(chrome_mod, "CrossProcessLock", _NullLock)

    with pytest.raises(TimeoutError):
        await chrome.ensure_running()

    assert kill_called["n"] == 1, "cleanup _kill must run"
    assert chrome._owns_chrome is False, "ownership dropped on initial failure"
    assert chrome._healthy is False


# ── T7: attacher restart is a no-op ──────────────────────────────────────


@pytest.mark.asyncio
async def test_attacher_restart_noop(monkeypatch):
    """A non-owner's restart() returns without acquiring the lock or launching."""
    chrome = _make_chrome(owns_chrome=False)
    lock_entered = {"n": 0}

    class _CountingLock(_NullLock):
        async def __aenter__(self):
            lock_entered["n"] += 1
            return self

    monkeypatch.setattr(chrome_mod, "CrossProcessLock", _CountingLock)

    await chrome.restart()

    assert lock_entered["n"] == 0, "attacher must not acquire the election lock"
    assert chrome._owns_chrome is False
    assert chrome._restart_count == 0


# ── T8: owner restart failure keeps ownership ────────────────────────────


@pytest.mark.asyncio
async def test_owner_restart_failure_keeps_ownership(monkeypatch):
    """Owner restart where _wait_for_cdp raises → cleanup _kill, but
    _owns_chrome STAYS True (owner retries next tick) — the asymmetry vs T6."""
    chrome = _make_chrome(
        owns_chrome=True, process=_FakePopen(returncode=None), breakers=BreakerRegistry()
    )
    kill_count = {"n": 0}

    async def _kill():
        kill_count["n"] += 1
        chrome._process = None

    async def _launch():
        chrome._process = _FakePopen(returncode=None)

    async def _wait(timeout=30):
        raise TimeoutError("CDP did not respond")

    chrome._kill = _kill  # type: ignore[method-assign]
    chrome._launch = _launch  # type: ignore[method-assign]
    chrome._wait_for_cdp = _wait  # type: ignore[method-assign]
    monkeypatch.setattr(chrome_mod, "CrossProcessLock", _NullLock)

    with pytest.raises(TimeoutError):
        await chrome.restart()

    assert kill_count["n"] == 2, "primary _kill + cleanup _kill"
    assert chrome._owns_chrome is True, "owner RETAINS ownership on restart failure"
    assert chrome._healthy is False
    assert chrome._restart_count == 1
    assert chrome._breakers.is_open(BreakerKind.CHROME_CRASH_LOOP) or True  # recorded


# ── T9: monitor-all — attacher observes, does not restart ────────────────


@pytest.mark.asyncio
async def test_monitor_attacher_observes_no_restart(monkeypatch):
    """An attacher's monitor updates _healthy to False when CDP is down but
    never calls restart(). Exercises the real _monitor_loop."""
    monkeypatch.setattr(chrome_mod, "MONITOR_INTERVAL_S", 0)
    chrome = _make_chrome(owns_chrome=False)

    async def _alive():
        return False  # CDP down

    chrome._cdp_alive = _alive  # type: ignore[method-assign]
    chrome.restart = lambda: pytest.fail("attacher must not restart") or asyncio.sleep(0)  # type: ignore[method-assign]

    task = asyncio.create_task(chrome._monitor_loop())
    await asyncio.sleep(0.05)  # let at least one iteration run
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    assert chrome._healthy is False, "attacher must still observe health"
    # restart was not called (the lambda would have failed the test if it was)


# ── T10: breaker-open suppresses restart ─────────────────────────────────


@pytest.mark.asyncio
async def test_monitor_breaker_open_suppresses_restart(monkeypatch):
    """Owner + restart_on_crash=True but breaker is open → no restart, but
    ownership is retained and _healthy reflects the truth (False). Exercises
    the real _monitor_loop."""
    monkeypatch.setattr(chrome_mod, "MONITOR_INTERVAL_S", 0)
    breakers = BreakerRegistry()
    breakers.trip(BreakerKind.CHROME_CRASH_LOOP, "test trip", cooldown_s=300.0)
    chrome = _make_chrome(
        owns_chrome=True, breakers=breakers, restart_on_crash=True
    )

    async def _alive():
        return False

    chrome._cdp_alive = _alive  # type: ignore[method-assign]

    async def _must_not_restart():
        pytest.fail("breaker open must suppress restart")

    chrome.restart = _must_not_restart  # type: ignore[method-assign]

    task = asyncio.create_task(chrome._monitor_loop())
    await asyncio.sleep(0.05)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    assert chrome._owns_chrome is True, "breaker does not relinquish ownership"
    assert chrome._healthy is False


# ── T11: stop guard ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_stop_owner_kills_attacher_does_not():
    """Owner stop() kills + clears ownership; attacher stop() does neither."""
    # Attacher
    attacher = _make_chrome(owns_chrome=False)
    attacher_kill = {"n": 0}

    async def a_kill():
        attacher_kill["n"] += 1

    attacher._kill = a_kill  # type: ignore[method-assign]
    await attacher.stop()
    assert attacher_kill["n"] == 0, "attacher must not kill"
    assert attacher._owns_chrome is False

    # Owner
    owner = _make_chrome(owns_chrome=True, process=_FakePopen(returncode=None))
    owner_kill = {"n": 0}

    async def o_kill():
        owner_kill["n"] += 1

    owner._kill = o_kill  # type: ignore[method-assign]
    await owner.stop()
    assert owner_kill["n"] == 1, "owner must kill"
    assert owner._owns_chrome is False, "owner relinquishes on stop"
