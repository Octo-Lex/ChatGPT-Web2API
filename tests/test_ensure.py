"""Tests for the ``ensure`` subcommand (ROADMAP Phase 3).

Point-in-time reconciliation of REST + SSE. Tests mock health checks, TCP
probes, subprocess launches, and the SSE handshake so the full policy is
exercised without real servers.
"""

from unittest.mock import MagicMock

import pytest

import chatgpt_web2api.ensure as ensure_mod
from chatgpt_web2api.ensure import (
    _build_rest_cmd,
    _build_sse_cmd,
    run_ensure,
)


def _install_virtual_clock(monkeypatch):
    t = [0.0]
    monkeypatch.setattr(ensure_mod.time, "monotonic", lambda: t[0])

    async def fast_sleep(s):
        t[0] += s

    monkeypatch.setattr(ensure_mod.asyncio, "sleep", fast_sleep)
    return t


def _patch_health(monkeypatch, health_sequence):
    """health_sequence: list of dicts/None returned by successive /health calls.
    A None means 'unreachable' (missing)."""
    calls = {"n": 0}
    health_sequence = list(health_sequence)

    def fake_health(rest_port, timeout=3.0):
        idx = min(calls["n"], len(health_sequence) - 1)
        calls["n"] += 1
        return health_sequence[idx]

    monkeypatch.setattr(ensure_mod, "_rest_health", fake_health)
    return calls


def _patch_sse_tcp(monkeypatch, up_sequence):
    """up_sequence: list of bools for successive TCP checks."""
    calls = {"n": 0}
    up_sequence = list(up_sequence)

    def fake_tcp(sse_port, timeout=1.0):
        idx = min(calls["n"], len(up_sequence) - 1)
        calls["n"] += 1
        return up_sequence[idx]

    monkeypatch.setattr(ensure_mod, "_sse_tcp_up", fake_tcp)
    return calls


def _patch_sse_verify(monkeypatch, result):
    async def fake_verify(sse_port):
        return result

    monkeypatch.setattr(ensure_mod, "_sse_verify", fake_verify)


def _patch_listener_stop(monkeypatch):
    """Patch _stop_listener + _find_listener_pid + _terminate_pid so restart
    tests don't call real netstat/taskkill. Returns a dict tracking calls."""
    calls = {"find": 0, "terminate": 0, "stopped_ports": []}

    def fake_find(port):
        calls["find"] += 1
        return 12345  # pretend a listener exists

    def fake_terminate(pid):
        calls["terminate"] += 1

    async def fake_stop(port, label="REST"):
        calls["stopped_ports"].append(port)
        return True  # success

    monkeypatch.setattr(ensure_mod, "_find_listener_pid", fake_find)
    monkeypatch.setattr(ensure_mod, "_terminate_pid", fake_terminate)
    monkeypatch.setattr(ensure_mod, "_stop_listener", fake_stop)
    return calls


def _patch_launch(monkeypatch):
    """Patch subprocess.Popen to capture commands without launching."""
    launches = []
    monkeypatch.setattr(
        ensure_mod.subprocess, "Popen", lambda cmd, **kw: launches.append(cmd) or MagicMock()
    )
    return launches


def _patch_lock(monkeypatch):
    """Patch _StartupLock to a no-op async context manager."""

    class FakeLock:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            pass

    monkeypatch.setattr(ensure_mod, "_StartupLock", lambda *a, **kw: FakeLock())


# ── 1. noop when both healthy ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_noop_when_rest_healthy_and_sse_up(monkeypatch):
    _install_virtual_clock(monkeypatch)
    _patch_health(monkeypatch, [{"status": "healthy"}])
    _patch_sse_tcp(monkeypatch, [True])
    _patch_sse_verify(monkeypatch, True)
    launches = _patch_launch(monkeypatch)
    _patch_lock(monkeypatch)

    code = await run_ensure(rest_port=8080, sse_port=8090)
    assert code == 0
    assert launches == [], "should not launch anything when both are up"


# ── 2. starts REST when missing ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_starts_rest_when_missing(monkeypatch):
    _install_virtual_clock(monkeypatch)
    # missing, then becomes healthy after launch
    _patch_health(monkeypatch, [None, {"status": "healthy"}])
    _patch_sse_tcp(monkeypatch, [True])
    _patch_sse_verify(monkeypatch, True)
    launches = _patch_launch(monkeypatch)
    _patch_lock(monkeypatch)

    code = await run_ensure(rest_port=8080, sse_port=8090)
    assert code == 0
    assert len(launches) == 1
    assert any("start" in a for a in launches[0]) or "start" in launches[0]


# ── 3. starts SSE when missing ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_starts_sse_when_missing(monkeypatch):
    _install_virtual_clock(monkeypatch)
    _patch_health(monkeypatch, [{"status": "healthy"}])
    _patch_sse_tcp(monkeypatch, [False, True])  # not up, then up after launch
    _patch_sse_verify(monkeypatch, True)
    launches = _patch_launch(monkeypatch)
    _patch_lock(monkeypatch)

    code = await run_ensure(rest_port=8080, sse_port=8090)
    assert code == 0
    assert len(launches) == 1
    # launches[0] is the arg list; check for the mcp_server module + sse transport
    assert any("mcp_server" in a for a in launches[0])
    assert "sse" in launches[0]


# ── 4. degraded waits then restarts ─────────────────────────────────────


@pytest.mark.asyncio
async def test_degraded_waits_then_restarts(monkeypatch):
    t = _install_virtual_clock(monkeypatch)
    # degraded for the whole 20s window, then healthy after restart
    health_seq = [{"status": "degraded"}] * 20 + [{"status": "healthy"}]
    _patch_health(monkeypatch, health_seq)
    _patch_sse_tcp(monkeypatch, [True])
    _patch_sse_verify(monkeypatch, True)
    launches = _patch_launch(monkeypatch)
    _patch_lock(monkeypatch)
    stop_calls = _patch_listener_stop(monkeypatch)

    code = await run_ensure(rest_port=8080, sse_port=8090)
    assert code == 0
    # Must have launched REST (the restart after degraded timeout)
    assert len(launches) == 1
    assert any("start" in a for a in launches[0])
    # Must have waited (clock advanced past the degraded budget)
    assert t[0] >= 20.0
    # Restart MUST stop the existing listener before launching
    assert stop_calls["stopped_ports"] == [8080]


# ── 5. degraded recovers, no restart ────────────────────────────────────


@pytest.mark.asyncio
async def test_degraded_recovers_no_restart(monkeypatch):
    _install_virtual_clock(monkeypatch)
    # degraded, then healthy within the window
    _patch_health(
        monkeypatch, [{"status": "degraded"}, {"status": "degraded"}, {"status": "healthy"}]
    )
    _patch_sse_tcp(monkeypatch, [True])
    _patch_sse_verify(monkeypatch, True)
    launches = _patch_launch(monkeypatch)
    _patch_lock(monkeypatch)

    code = await run_ensure(rest_port=8080, sse_port=8090)
    assert code == 0
    assert launches == [], "should NOT restart REST when it recovers from degraded"


# ── 6. broken restarts REST immediately ─────────────────────────────────


@pytest.mark.asyncio
async def test_broken_restarts_rest(monkeypatch):
    _install_virtual_clock(monkeypatch)
    _patch_health(monkeypatch, [{"status": "broken"}, {"status": "healthy"}])
    _patch_sse_tcp(monkeypatch, [True])
    _patch_sse_verify(monkeypatch, True)
    launches = _patch_launch(monkeypatch)
    _patch_lock(monkeypatch)
    stop_calls = _patch_listener_stop(monkeypatch)

    code = await run_ensure(rest_port=8080, sse_port=8090)
    assert code == 0
    assert len(launches) == 1
    assert any("start" in a for a in launches[0])
    # Restart MUST stop the existing listener before launching — not a bare launch
    assert stop_calls["stopped_ports"] == [8080], (
        "restart must call _stop_rest_listener before relaunch"
    )


# ── 7. concurrent lock prevents double-launch (bounded wait) ───────────


@pytest.mark.asyncio
async def test_concurrent_lock_contention_exits_on_observed_state(monkeypatch):
    _install_virtual_clock(monkeypatch)

    class HeldLock:
        async def __aenter__(self):
            raise ensure_mod.LockAcquisitionError("held")

        async def __aexit__(self, *a):
            pass

    monkeypatch.setattr(ensure_mod, "_StartupLock", lambda *a, **kw: HeldLock())
    # During re-check, both are healthy → exit 0
    _patch_health(monkeypatch, [{"status": "healthy"}])
    _patch_sse_tcp(monkeypatch, [True])
    _patch_sse_verify(monkeypatch, True)
    launches = _patch_launch(monkeypatch)

    code = await run_ensure(rest_port=8080, sse_port=8090)
    assert code == 0
    assert launches == [], "should not launch while another ensure owns the lock"


# ── 8. exit nonzero on failure ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_exit_nonzero_when_rest_never_healthy(monkeypatch):
    _install_virtual_clock(monkeypatch)
    _patch_health(monkeypatch, [None])  # never becomes healthy
    _patch_sse_tcp(monkeypatch, [True])
    _patch_sse_verify(monkeypatch, True)
    _patch_launch(monkeypatch)
    _patch_lock(monkeypatch)

    code = await run_ensure(rest_port=8080, sse_port=8090)
    assert code != 0


# ── 9. custom rest_port passed to subprocess ────────────────────────────


@pytest.mark.asyncio
async def test_custom_rest_port_passed_to_subprocess(monkeypatch):
    _install_virtual_clock(monkeypatch)
    _patch_health(monkeypatch, [None, {"status": "healthy"}])
    _patch_sse_tcp(monkeypatch, [True])
    _patch_sse_verify(monkeypatch, True)
    launches = _patch_launch(monkeypatch)
    _patch_lock(monkeypatch)

    code = await run_ensure(rest_port=8081, sse_port=8090)
    assert code == 0
    assert len(launches) == 1
    # The custom port must appear in the REST launch command
    assert "8081" in launches[0]


# ── 10. cdp/config args propagated correctly ───────────────────────────


@pytest.mark.asyncio
async def test_cdp_and_config_args_propagated(monkeypatch):
    """--cdp-port is ALWAYS passed. --config/--log-level ONLY when explicit."""
    _install_virtual_clock(monkeypatch)
    _patch_health(monkeypatch, [None, {"status": "healthy"}])
    _patch_sse_tcp(monkeypatch, [False, True])
    _patch_sse_verify(monkeypatch, True)
    launches = _patch_launch(monkeypatch)
    _patch_lock(monkeypatch)

    # With explicit config + log_level
    code = await run_ensure(
        rest_port=8080,
        sse_port=8090,
        cdp_port=9333,
        config_path="/tmp/cfg.json",
        log_level="DEBUG",
    )
    assert code == 0
    assert len(launches) == 2  # REST + SSE
    rest_cmd, sse_cmd = launches
    # --cdp-port always present
    assert "--cdp-port" in rest_cmd and "9333" in rest_cmd
    assert "--cdp-port" in sse_cmd and "9333" in sse_cmd
    # --config present when explicit
    assert "--config" in rest_cmd and "/tmp/cfg.json" in rest_cmd
    assert "--config" in sse_cmd and "/tmp/cfg.json" in sse_cmd
    # --log-level present when explicit
    assert "--log-level" in rest_cmd and "DEBUG" in rest_cmd
    assert "--log-level" in sse_cmd and "DEBUG" in sse_cmd


@pytest.mark.asyncio
async def test_no_config_no_log_level_when_not_explicit(monkeypatch):
    """When config/log_level are None (not provided), they must NOT appear."""
    _install_virtual_clock(monkeypatch)
    _patch_health(monkeypatch, [None, {"status": "healthy"}])
    _patch_sse_tcp(monkeypatch, [False, True])
    _patch_sse_verify(monkeypatch, True)
    launches = _patch_launch(monkeypatch)
    _patch_lock(monkeypatch)

    code = await run_ensure(rest_port=8080, sse_port=8090)  # no config/log_level
    assert code == 0
    for cmd in launches:
        assert "--config" not in cmd, f"--config should be absent: {cmd}"
        assert "--log-level" not in cmd, f"--log-level should be absent: {cmd}"


# ── unit tests for command builders (the 4 flag cases) ─────────────────


def test_build_rest_cmd_minimal():
    """No config/log_level → only --port and --cdp-port."""
    cmd = _build_rest_cmd(8080, 9222, None, None)
    assert "--port" in cmd and "8080" in cmd
    assert "--cdp-port" in cmd and "9222" in cmd
    assert "--config" not in cmd
    assert "--log-level" not in cmd


def test_build_rest_cmd_full():
    """Explicit config/log_level → both present."""
    cmd = _build_rest_cmd(8081, 9333, "/tmp/c.json", "DEBUG")
    assert "--port" in cmd and "8081" in cmd
    assert "--cdp-port" in cmd and "9333" in cmd
    assert "--config" in cmd and "/tmp/c.json" in cmd
    assert "--log-level" in cmd and "DEBUG" in cmd


def test_build_sse_cmd_minimal():
    cmd = _build_sse_cmd(8090, 9222, None, None)
    assert "--transport" in cmd and "sse" in cmd
    assert "--port" in cmd and "8090" in cmd
    assert "--cdp-port" in cmd and "9222" in cmd
    assert "--config" not in cmd
    assert "--log-level" not in cmd


def test_build_sse_cmd_full():
    cmd = _build_sse_cmd(8091, 9333, "/tmp/c.json", "WARNING")
    assert "--port" in cmd and "8091" in cmd
    assert "--cdp-port" in cmd and "9333" in cmd
    assert "--config" in cmd and "/tmp/c.json" in cmd
    assert "--log-level" in cmd and "WARNING" in cmd


# ── 12. Lock handle retained until release (review fix #1) ─────────────


@pytest.mark.asyncio
async def test_startup_lock_retains_handle_until_release(monkeypatch):
    """The portalocker file handle must be HELD open until __aexit__. The old
    code locked one handle then stored a different unlocked one — so concurrent
    ensures could double-launch. Verify the held handle is the locked one and
    survives until release."""
    import tempfile
    from pathlib import Path

    # Use a temp dir so we don't clobber the real lock
    tmpdir = tempfile.mkdtemp()
    monkeypatch.setattr(Path, "home", lambda: Path(tmpdir))

    lock = ensure_mod._StartupLock(sse_port=99999, timeout=5.0)
    await lock.__aenter__()
    # The handle must be set and open (not None, not closed)
    assert lock._fh is not None, "lock handle must be retained after acquire"
    assert not lock._fh.closed, "lock handle must still be open while held"
    await lock.__aexit__(None, None, None)
    # After release, the handle is closed
    assert lock._fh.closed, "lock handle must be closed after release"


# ── 13. starting + connected is ready for ensure (review fix #2) ───────


def test_rest_ready_for_ensure_accepts_starting_connected():
    """A cold-bootstrap REST may report 'starting' (no chat yet) but with
    Chrome/CDP/driver all connected. That's ready enough for SSE to attach."""
    assert (
        ensure_mod._rest_ready_for_ensure(
            {
                "status": "starting",
                "chrome_running": True,
                "cdp_connected": True,
                "driver_connected": True,
            }
        )
        is True
    )


def test_rest_ready_for_ensure_rejects_starting_not_connected():
    """starting WITHOUT full connectivity (Chrome up but driver not connected)
    must NOT pass — SSE can't attach to a half-started REST."""
    assert (
        ensure_mod._rest_ready_for_ensure(
            {
                "status": "starting",
                "chrome_running": True,
                "cdp_connected": False,
                "driver_connected": False,
            }
        )
        is False
    )


def test_rest_ready_for_ensure_rejects_degraded_broken():
    assert ensure_mod._rest_ready_for_ensure({"status": "degraded"}) is False
    assert ensure_mod._rest_ready_for_ensure({"status": "broken"}) is False
    assert ensure_mod._rest_ready_for_ensure(None) is False


@pytest.mark.asyncio
async def test_ensure_accepts_starting_connected_as_ready(monkeypatch):
    """End-to-end: REST reports starting+connected → ensure proceeds to SSE,
    no restart, exits 0."""
    _install_virtual_clock(monkeypatch)
    _patch_health(
        monkeypatch,
        [
            {
                "status": "starting",
                "chrome_running": True,
                "cdp_connected": True,
                "driver_connected": True,
            }
        ],
    )
    _patch_sse_tcp(monkeypatch, [True])
    _patch_sse_verify(monkeypatch, True)
    launches = _patch_launch(monkeypatch)
    _patch_lock(monkeypatch)

    code = await run_ensure(rest_port=8080, sse_port=8090)
    assert code == 0
    assert launches == [], "should not launch anything — starting+connected is ready"


# ── 14. restart stops listener before relaunch (review fix #3) ─────────


@pytest.mark.asyncio
async def test_restart_calls_stop_listener_before_launch(monkeypatch):
    """The broken/degraded-timeout restart path must call _stop_listener
    BEFORE _launch_detached — a bare launch on an occupied port fails to bind.
    This is covered in test_broken_restarts_rest and test_degraded_waits_then_restarts
    above; this is a focused unit test on _restart_rest itself."""
    _install_virtual_clock(monkeypatch)
    monkeypatch.setattr(ensure_mod, "_rest_health", lambda *a, **kw: {"status": "healthy"})

    stop_order = []
    monkeypatch.setattr(ensure_mod, "_stop_listener", _make_async_recorder(stop_order, "stop"))
    monkeypatch.setattr(ensure_mod, "_launch_detached", _make_recorder(stop_order, "launch"))
    monkeypatch.setattr(ensure_mod, "_wait_rest_ready", _make_async_recorder(stop_order, "wait"))

    await ensure_mod._restart_rest(8080, 9222, None, None)
    # stop MUST come before launch
    assert stop_order == ["stop", "launch", "wait"], f"order wrong: {stop_order}"


def _make_recorder(lst, tag):
    def _record(*a, **kw):
        lst.append(tag)
        return MagicMock()

    return _record


def _make_async_recorder(lst, tag):
    async def _record(*a, **kw):
        lst.append(tag)
        return True

    return _record


# ── 15. Unix listener discovery fallback (issue #16) ───────────────────


def test_find_listener_pid_uses_netstat_on_windows(monkeypatch):
    """On Windows, _find_listener_pid uses netstat (not the Unix chain)."""
    from unittest.mock import patch

    with (
        patch.object(ensure_mod.sys, "platform", "win32"),
        patch.object(ensure_mod, "_find_listener_pid_netstat", return_value=999) as netstat_mock,
        patch.object(ensure_mod, "_find_listener_pid_lsof", return_value=111) as lsof_mock,
    ):
        assert ensure_mod._find_listener_pid(8080) == 999
        netstat_mock.assert_called_once_with(8080)
        lsof_mock.assert_not_called()


def test_find_listener_pid_unix_lsof_found(monkeypatch):
    """On Unix, when lsof succeeds, its PID is returned (no fallback needed)."""
    from unittest.mock import patch

    with (
        patch.object(ensure_mod.sys, "platform", "linux"),
        patch.object(ensure_mod, "_find_listener_pid_lsof", return_value=111),
        patch.object(ensure_mod, "_find_listener_pid_ss", return_value=222) as ss_mock,
    ):
        assert ensure_mod._find_listener_pid(8080) == 111
        ss_mock.assert_not_called()


def test_find_listener_pid_unix_falls_back_to_ss():
    """When lsof returns None (absent/fails), ss is tried."""
    from unittest.mock import patch

    with (
        patch.object(ensure_mod.sys, "platform", "linux"),
        patch.object(ensure_mod, "_find_listener_pid_lsof", return_value=None),
        patch.object(ensure_mod, "_find_listener_pid_ss", return_value=222),
    ):
        assert ensure_mod._find_listener_pid(8080) == 222


def test_find_listener_pid_unix_falls_back_to_fuser():
    """When lsof and ss both fail, fuser is tried."""
    from unittest.mock import patch

    with (
        patch.object(ensure_mod.sys, "platform", "linux"),
        patch.object(ensure_mod, "_find_listener_pid_lsof", return_value=None),
        patch.object(ensure_mod, "_find_listener_pid_ss", return_value=None),
        patch.object(ensure_mod, "_find_listener_pid_fuser", return_value=333),
    ):
        assert ensure_mod._find_listener_pid(8080) == 333


def test_find_listener_pid_unix_all_fail_returns_none():
    """When all three Unix tools fail/are absent, returns None (not an error)."""
    from unittest.mock import patch

    with (
        patch.object(ensure_mod.sys, "platform", "linux"),
        patch.object(ensure_mod, "_find_listener_pid_lsof", return_value=None),
        patch.object(ensure_mod, "_find_listener_pid_ss", return_value=None),
        patch.object(ensure_mod, "_find_listener_pid_fuser", return_value=None),
    ):
        assert ensure_mod._find_listener_pid(8080) is None


# ── 16. Occupied port but no PID → _stop_listener returns False (issue #16)


@pytest.mark.asyncio
async def test_stop_listener_returns_false_when_port_occupied_but_no_pid(monkeypatch):
    """The dangerous case: port is occupied (accepts connections) but no PID can
    be found (tools missing). _stop_listener must return False so the caller
    does NOT relaunch into the occupied port."""
    monkeypatch.setattr(ensure_mod, "_find_listener_pid", lambda port: None)
    monkeypatch.setattr(ensure_mod, "_port_accepts", lambda port: True)  # occupied

    result = await ensure_mod._stop_listener(8080, "REST")
    assert result is False, "must return False when occupied but no PID found"


@pytest.mark.asyncio
async def test_stop_listener_returns_true_when_port_free(monkeypatch):
    """Nothing listening → _stop_listener returns True (safe to launch)."""
    monkeypatch.setattr(ensure_mod, "_find_listener_pid", lambda port: None)
    monkeypatch.setattr(ensure_mod, "_port_accepts", lambda port: False)  # free

    result = await ensure_mod._stop_listener(8080, "REST")
    assert result is True


@pytest.mark.asyncio
async def test_restart_rest_aborts_when_listener_cannot_be_stopped(monkeypatch):
    """If _stop_listener returns False (can't stop), _restart_rest must NOT
    launch — returns False (the launch would fail to bind)."""
    _install_virtual_clock(monkeypatch)
    monkeypatch.setattr(ensure_mod, "_rest_health", lambda *a, **kw: {"status": "healthy"})
    launches = _patch_launch(monkeypatch)

    async def stop_fails(port, label="REST"):
        return False  # can't stop

    monkeypatch.setattr(ensure_mod, "_stop_listener", stop_fails)

    result = await ensure_mod._restart_rest(8080, 9222, None, None)
    assert result is False
    assert launches == [], "must NOT launch when listener can't be stopped"


# ── 17. SSE broken-handshake stops listener before relaunch (issue #16) ─


@pytest.mark.asyncio
async def test_sse_broken_handshake_stops_listener_before_relaunch(monkeypatch):
    """TCP-up + handshake-failed → _stop_listener called on the SSE port
    before launching a new SSE. The old behavior launched without stopping,
    causing a bind failure."""
    _install_virtual_clock(monkeypatch)
    _patch_health(monkeypatch, [{"status": "healthy"}])
    # TCP up, handshake fails, then TCP up + handshake succeeds after relaunch
    _patch_sse_tcp(monkeypatch, [True, False, True])
    _patch_sse_verify(monkeypatch, False)  # first handshake fails
    # But we need the second verify to succeed — use a sequence
    verify_results = [False, True]

    async def seq_verify(sse_port):
        return verify_results.pop(0) if verify_results else True

    monkeypatch.setattr(ensure_mod, "_sse_verify", seq_verify)
    launches = _patch_launch(monkeypatch)
    _patch_lock(monkeypatch)

    stop_calls = []

    # First TCP check returns True (port up), _stop_listener must be called.
    # After stop, TCP goes False, then after launch TCP goes True.
    async def fake_stop(port, label="REST"):
        stop_calls.append((port, label))
        return True

    monkeypatch.setattr(ensure_mod, "_stop_listener", fake_stop)

    code = await run_ensure(rest_port=8080, sse_port=8090)
    assert code == 0
    assert len(launches) == 1  # SSE was launched
    # _stop_listener was called on the SSE port
    assert any(p == 8090 and lbl == "SSE" for p, lbl in stop_calls), (
        f"SSE broken-handshake must call _stop_listener: {stop_calls}"
    )


# ── 18. Exact-port matching — no prefix-port false positive (#16 review) ──


def test_find_listener_pid_ss_does_not_match_prefix_port(monkeypatch):
    """The reviewer's exact scenario: querying port 80 must NOT match an ss
    line for :8080. The old substring check ``f":{port}" in line`` matched
    ``:8080`` when port==80 and returned pid=123, so _stop_listener could
    terminate an unrelated process. Exact address parsing fixes it."""
    from unittest.mock import patch

    ss_output = (
        "LISTEN 0      4096   127.0.0.1:8080   0.0.0.0:*   "
        "users:((\"python\",pid=123,fd=5))\n"
    )
    with patch.object(ensure_mod.subprocess, "check_output", return_value=ss_output):
        assert ensure_mod._find_listener_pid_ss(80) is None


def test_find_listener_pid_ss_matches_exact_port(monkeypatch):
    """The exact-port fix still returns the PID when the queried port IS the
    listening port."""
    from unittest.mock import patch

    ss_output = (
        "LISTEN 0      4096   127.0.0.1:8080   0.0.0.0:*   "
        "users:((\"python\",pid=123,fd=5))\n"
    )
    with patch.object(ensure_mod.subprocess, "check_output", return_value=ss_output):
        assert ensure_mod._find_listener_pid_ss(8080) == 123


def test_find_listener_pid_ss_ignores_neighbor_port(monkeypatch):
    """A listener on 8080 must not satisfy a query for 8090 (and vice versa)."""
    from unittest.mock import patch

    ss_output = (
        "LISTEN 0      4096   127.0.0.1:8080   0.0.0.0:*   "
        "users:((\"python\",pid=123,fd=5))\n"
    )
    with patch.object(ensure_mod.subprocess, "check_output", return_value=ss_output):
        assert ensure_mod._find_listener_pid_ss(8090) is None


def test_find_listener_pid_ss_picks_correct_pid_among_many(monkeypatch):
    """When several listeners are present, the exact port returns its own PID,
    not a neighbor's."""
    from unittest.mock import patch

    ss_output = (
        "LISTEN 0      4096   127.0.0.1:80    0.0.0.0:*   "
        "users:((\"nginx\",pid=7,fd=6))\n"
        "LISTEN 0      4096   127.0.0.1:8080  0.0.0.0:*   "
        "users:((\"python\",pid=123,fd=5))\n"
    )
    with patch.object(ensure_mod.subprocess, "check_output", return_value=ss_output):
        assert ensure_mod._find_listener_pid_ss(80) == 7
        assert ensure_mod._find_listener_pid_ss(8080) == 123
