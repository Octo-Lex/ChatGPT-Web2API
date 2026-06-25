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

    code = await run_ensure(rest_port=8080, sse_port=8090)
    assert code == 0
    # Must have launched REST (the restart after degraded timeout)
    assert len(launches) == 1
    assert any("start" in a for a in launches[0])
    # Must have waited (clock advanced past the degraded budget)
    assert t[0] >= 20.0


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

    code = await run_ensure(rest_port=8080, sse_port=8090)
    assert code == 0
    assert len(launches) == 1
    assert any("start" in a for a in launches[0])


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
