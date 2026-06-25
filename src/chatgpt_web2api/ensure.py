"""Point-in-time reconciliation of the REST + SSE stack.

``chatgpt-web2api ensure`` is a thin one-liner for ZCode hooks: it makes REST
and SSE healthy NOW, then exits. It is NOT a continuous supervisor — if SSE
dies later, the next hook/session re-runs ``ensure``.

Pinned design (ROADMAP Phase 3):
  - point-in-time, not a watchdog (no Python loop after exit)
  - REST owns Chrome; SSE attaches and never launches Chrome
  - degraded-REST is NOT restarted immediately (may be a transient CDP
    reconnect — give it 20s of polling before bouncing the browser)
  - lock-protected so concurrent ``ensure`` runs don't double-launch

Exit codes: 0 when both REST and SSE are ready, nonzero with a diagnostic
otherwise.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

from mcp import ClientSession
from mcp.client.sse import sse_client

from chatgpt_web2api.cross_process_lock import LockAcquisitionError

logger = logging.getLogger(__name__)

# Bounded wait for REST to resolve starting/degraded before we act.
_REST_HEALTHY_TIMEOUT = 30.0
# Degraded-REST grace window: poll every 2s for up to 20s before restarting.
_DEGRADED_POLL_INTERVAL = 2.0
_DEGRADED_POLL_BUDGET = 20.0
# Startup lock: bounded contention wait.
_LOCK_TIMEOUT = 10.0
_LOCK_CONTENTION_RECHECK_INTERVAL = 3.0
_LOCK_CONTENTION_RECHECK_TRIES = 3
# SSE readiness: TCP preflight then real MCP handshake.
_SSE_VERIFY_TIMEOUT = 15.0


class _StartupLock:
    """A bounded, SSE-port-keyed startup lock.

    Distinct from the CDP-keyed CrossProcessLock (which serializes request-
    level DOM mutations). This one prevents two concurrent ``ensure`` runs
    from double-launching REST/SSE. Uses portalocker under the hood so it is
    cross-process safe.
    """

    import portalocker

    def __init__(self, sse_port: int, timeout: float = _LOCK_TIMEOUT) -> None:
        self._path = str(Path.home() / ".chatgpt_web2api" / f"sse-startup-{sse_port}.lock")
        self._timeout = timeout
        self._fh = None

    async def __aenter__(self):
        Path(self._path).parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + self._timeout
        while True:
            try:
                # Non-blocking attempt in a thread (portalocker is blocking)
                self._fh = await asyncio.to_thread(self._try_acquire)
                return self
            except _LockBusy:
                if time.monotonic() >= deadline:
                    raise LockAcquisitionError(
                        f"startup lock held after {self._timeout}s — another ensure is running"
                    )
                await asyncio.sleep(0.3)

    def _try_acquire(self):
        """Open the lockfile and acquire an exclusive non-blocking lock on it.

        Returns the file handle (which MUST be held open until release —
        closing it drops the OS lock). Raises ``_LockBusy`` if another process
        holds the lock.
        """
        fh = open(self._path, "w")
        try:
            self.portalocker.lock(fh, self.portalocker.LOCK_EX | self.portalocker.LOCK_NB)
        except self.portalocker.LockException:
            fh.close()
            raise _LockBusy()
        return fh

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self._fh:
            try:
                await asyncio.to_thread(self.portalocker.unlock, self._fh)
                self._fh.close()
            except Exception:
                pass


class _LockBusy(Exception):
    """Internal: lock is currently held by another process."""


def _rest_ready_for_ensure(h: dict | None) -> bool:
    """Is REST ready for ``ensure`` to proceed to SSE?

    ``healthy`` is always ready. ``starting`` is ALSO acceptable for a cold
    bootstrap: REST/Chrome/CDP are connected but no chat has succeeded yet —
    that's enough for SSE to attach. Requires all three connection flags set
    so a half-started REST (Chrome up but driver not connected) doesn't pass.
    """
    if not h:
        return False
    if h.get("status") == "healthy":
        return True
    if h.get("status") == "starting":
        return bool(
            h.get("chrome_running") and h.get("cdp_connected") and h.get("driver_connected")
        )
    return False


def _rest_health(rest_port: int, timeout: float = 3.0) -> dict | None:
    """GET /health. Returns the parsed JSON dict, or None if unreachable
    (connection refused = ``missing``)."""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{rest_port}/health", timeout=timeout) as r:
            return json.loads(r.read())
    except Exception:
        return None


def _sse_tcp_up(sse_port: int, timeout: float = 1.0) -> bool:
    """TCP preflight: is anything listening on the SSE port? Not a readiness
    guarantee — the real check is the MCP handshake below."""
    try:
        with socket.socket() as s:
            s.settimeout(timeout)
            s.connect(("127.0.0.1", sse_port))
            return True
    except OSError:
        return False


async def _sse_verify(sse_port: int) -> bool:
    """Real SSE readiness: connect a client, initialize, list tools. TCP-up
    but handshake failure = NOT ready."""
    url = f"http://127.0.0.1:{sse_port}/sse"
    try:
        async with asyncio.timeout(_SSE_VERIFY_TIMEOUT):
            async with sse_client(url) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    await session.list_tools()
        return True
    except Exception as e:
        logger.debug("SSE verify failed: %s", e)
        return False


def _build_rest_cmd(
    rest_port: int, cdp_port: int, config_path: str | None, log_level: str | None
) -> list[str]:
    """Construct the REST launch command. --port and --cdp-port are always
    passed (explicit ensure params). --config/--log-level only when the caller
    provided them (sentinel None = not provided)."""
    cmd = [
        sys.executable,
        "-m",
        "chatgpt_web2api",
        "start",
        "--port",
        str(rest_port),
        "--cdp-port",
        str(cdp_port),
    ]
    if config_path:
        cmd += ["--config", config_path]
    if log_level:
        cmd += ["--log-level", log_level]
    return cmd


def _build_sse_cmd(
    sse_port: int, cdp_port: int, config_path: str | None, log_level: str | None
) -> list[str]:
    """Construct the SSE/MCP launch command."""
    cmd = [
        sys.executable,
        "-m",
        "chatgpt_web2api.mcp_server",
        "--transport",
        "sse",
        "--port",
        str(sse_port),
        "--cdp-port",
        str(cdp_port),
    ]
    if config_path:
        cmd += ["--config", config_path]
    if log_level:
        cmd += ["--log-level", log_level]
    return cmd


def _launch_detached(cmd: list[str]) -> subprocess.Popen:
    """Launch a detached subprocess that survives this process's exit."""
    kwargs: dict = {
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "stdin": subprocess.DEVNULL,
    }
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS
    else:
        kwargs["start_new_session"] = True
    return subprocess.Popen(cmd, **kwargs)


def _find_listener_pid(port: int) -> int | None:
    """Find the PID listening on the given TCP port (loopback). Returns None
    if nothing is listening or the lookup fails.

    Windows: ``netstat -ano`` (ubiquitous). Unix: a fallback chain of
    ``lsof`` → ``ss`` → ``fuser`` — no single tool is guaranteed on every
    distro/container, so we try each in turn. Returns the first PID found.
    """
    if sys.platform == "win32":
        return _find_listener_pid_netstat(port)
    for finder in (_find_listener_pid_lsof, _find_listener_pid_ss, _find_listener_pid_fuser):
        pid = finder(port)
        if pid is not None:
            return pid
    return None


def _find_listener_pid_netstat(port: int) -> int | None:
    try:
        out = subprocess.check_output(
            ["netstat", "-ano"], text=True, stderr=subprocess.DEVNULL, timeout=5
        )
        for line in out.splitlines():
            parts = line.split()
            # parts[1] is "HOST:PORT" (e.g. "127.0.0.1:8080"). endswith is
            # already exact here (":80" won't match ":8080" — the string ends in
            # "0", not "80"); kept explicit for clarity.
            if len(parts) >= 5 and "LISTENING" in line and parts[1].endswith(f":{port}"):
                return int(parts[-1])
    except Exception:
        pass
    return None


def _find_listener_pid_lsof(port: int) -> int | None:
    try:
        out = subprocess.check_output(
            ["lsof", "-ti", f":{port}"], text=True, stderr=subprocess.DEVNULL, timeout=5
        ).strip()
        if out:
            return int(out.splitlines()[0])
    except Exception:
        pass
    return None


def _find_listener_pid_ss(port: int) -> int | None:
    """``ss -tlnp`` — standard on modern Linux (iproute2). Parse the pid= from
    the users: column.

    The Local Address column is matched EXACTLY (host:port split on the last
    ':'), never by substring — the previous ``f":{port}" in line`` check treated
    port 80 as matching ':8080' and returned the wrong PID. See issue #16.
    """
    port_str = str(port)
    try:
        out = subprocess.check_output(
            ["ss", "-tlnp"], text=True, stderr=subprocess.DEVNULL, timeout=5
        )
        for line in out.splitlines():
            if "pid=" not in line:
                continue
            # Columns run together when empty; "pid=" only appears on bound
            # sockets, and the Local Address precedes Peer Address. Extract the
            # port from the "<addr>:<port>" token (whitespace-delimited) and
            # require an exact match.
            tokens = line.split()
            if not any(t.rsplit(":", 1)[-1] == port_str for t in tokens):
                continue
            m = re.search(r"pid=(\d+)", line)
            if m:
                return int(m.group(1))
    except Exception:
        pass
    return None


def _find_listener_pid_fuser(port: int) -> int | None:
    """``fuser <port>/tcp`` — older Linux fallback (psmisc)."""
    try:
        out = subprocess.check_output(
            ["fuser", f"{port}/tcp"], text=True, stderr=subprocess.DEVNULL, timeout=5
        ).strip()
        if out:
            return int(out.split()[0])
    except Exception:
        pass
    return None


def _terminate_pid(pid: int) -> None:
    """Terminate a process by PID. Force-kill if it doesn't exit gracefully."""
    try:
        if sys.platform == "win32":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)], capture_output=True, timeout=10
            )
        else:
            import signal as _sig

            os.kill(pid, _sig.SIGTERM)
            time.sleep(2)
            # Check if still alive; SIGKILL if so
            try:
                os.kill(pid, 0)
                os.kill(pid, _sig.SIGKILL)
            except ProcessLookupError:
                pass
    except Exception as e:
        logger.debug("terminate pid %s failed: %s", pid, e)


async def _stop_listener(port: int, label: str) -> bool:
    """Stop whatever process is listening on the given port, then wait for the
    port to free. A bare ``_launch_detached`` on an occupied port fails to bind
    (stderr is discarded), so a restart must stop the existing listener first.

    Returns True if the port is free (either nothing was listening, or the
    listener was stopped). Returns False if the port is still occupied but no
    PID could be found to terminate — caller should NOT relaunch in that case
    (the new process would fail to bind with no diagnostic)."""
    pid = _find_listener_pid(port)
    if pid is None:
        # Check if the port is actually free (nothing listening) vs occupied
        # but no PID discoverable (tools missing) — the dangerous case.
        if _port_accepts(port):
            logger.error(
                "Port :%d (%s) is occupied but no listener PID could be found "
                "(lsof/ss/fuser/netstat all failed or absent). Cannot safely "
                "restart — the new process would fail to bind. Aborting restart.",
                port,
                label,
            )
            return False
        return True  # port is genuinely free

    logger.info("Stopping existing %s listener (pid %s) on :%d", label, pid, port)
    await asyncio.to_thread(_terminate_pid, pid)
    # Wait for the port to free (bind would fail if still occupied)
    for _ in range(20):
        if not _port_accepts(port):
            return True  # port closed — ready
        await asyncio.sleep(0.5)
    logger.error("%s listener (pid %s) did not release :%d after 10s", label, pid, port)
    return False


def _port_accepts(port: int) -> bool:
    """Does anything accept a TCP connection on this loopback port?"""
    try:
        with socket.socket() as s:
            s.settimeout(0.5)
            s.connect(("127.0.0.1", port))
        return True
    except OSError:
        return False


async def _restart_rest(
    rest_port: int, cdp_port: int, config_path: str | None, log_level: str | None
) -> bool:
    """Stop the existing REST listener, then launch a fresh one. Used for
    ``broken`` and degraded-after-timeout — not for ``missing`` (no listener
    to stop). Returns False if the listener couldn't be stopped (port still
    occupied) — in that case do NOT relaunch (bind would fail silently)."""
    if not await _stop_listener(rest_port, "REST"):
        return False
    _launch_detached(_build_rest_cmd(rest_port, cdp_port, config_path, log_level))
    return await _wait_rest_ready(rest_port, _REST_HEALTHY_TIMEOUT)


async def _wait_rest_ready(rest_port: int, timeout: float) -> bool:
    """Poll /health until REST is ready for ensure (healthy OR starting+connected).
    Returns True if ready within the timeout, False otherwise."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _rest_ready_for_ensure(_rest_health(rest_port)):
            return True
        await asyncio.sleep(1.0)
    return False


async def _reconcile_rest(
    rest_port: int, cdp_port: int, config_path: str | None, log_level: str | None
) -> bool:
    """Apply the degraded-REST policy and wait for ready. Returns True when
    REST is ready for ensure, False if it can't be made ready."""
    h = _rest_health(rest_port)
    if h is None:
        logger.info("REST missing — starting")
        _launch_detached(_build_rest_cmd(rest_port, cdp_port, config_path, log_level))
        return await _wait_rest_ready(rest_port, _REST_HEALTHY_TIMEOUT)

    status = h.get("status", "broken")
    if status in ("healthy", "starting") and _rest_ready_for_ensure(h):
        logger.info("REST ready (status=%s)", status)
        return True

    if status == "broken":
        logger.info("REST broken (Chrome down) — restarting")
        return await _restart_rest(rest_port, cdp_port, config_path, log_level)

    if status == "degraded":
        # PINNED: degraded may be a transient CDP reconnect. Wait before bouncing.
        logger.info("REST degraded — waiting up to %.0fs before restart", _DEGRADED_POLL_BUDGET)
        deadline = time.monotonic() + _DEGRADED_POLL_BUDGET
        while time.monotonic() < deadline:
            await asyncio.sleep(_DEGRADED_POLL_INTERVAL)
            h = _rest_health(rest_port)
            if _rest_ready_for_ensure(h):
                logger.info("REST recovered from degraded/starting")
                return True
            if h and h.get("status") == "broken":
                break  # fall through to restart
        logger.info("REST still degraded after %.0fs — restarting", _DEGRADED_POLL_BUDGET)
        return await _restart_rest(rest_port, cdp_port, config_path, log_level)

    # starting without full connectivity — wait for it to resolve
    logger.info("REST starting (not fully connected) — waiting")
    return await _wait_rest_ready(rest_port, _REST_HEALTHY_TIMEOUT)


async def _reconcile_sse(
    sse_port: int, cdp_port: int, config_path: str | None, log_level: str | None
) -> bool:
    """Start SSE if missing, then verify via real MCP handshake. If the port is
    up but the handshake fails (broken/hung SSE), stop the existing listener
    before relaunching — a bare launch on the occupied port would fail to bind."""
    if _sse_tcp_up(sse_port):
        ready = await _sse_verify(sse_port)
        if ready:
            logger.info("SSE ready on :%d", sse_port)
            return True
        # Port is up but handshake failed — stop the broken listener first.
        # If _stop_listener can't confirm termination (tools missing), abort:
        # a relaunch would fail to bind with no diagnostic.
        logger.info("SSE port up but handshake failed — stopping broken listener")
        if not await _stop_listener(sse_port, "SSE"):
            logger.error("Could not stop broken SSE listener on :%d — aborting", sse_port)
            return False

    logger.info("SSE starting")
    _launch_detached(_build_sse_cmd(sse_port, cdp_port, config_path, log_level))

    # Wait for TCP, then verify handshake.
    deadline = time.monotonic() + _REST_HEALTHY_TIMEOUT
    while time.monotonic() < deadline:
        if _sse_tcp_up(sse_port):
            if await _sse_verify(sse_port):
                logger.info("SSE ready on :%d", sse_port)
                return True
        await asyncio.sleep(1.0)
    return False


async def run_ensure(
    rest_port: int = 8080,
    sse_port: int = 8090,
    cdp_port: int = 9222,
    config_path: str | None = None,
    log_level: str | None = None,
) -> int:
    """Point-in-time reconcile of REST + SSE. Returns 0 on ready, nonzero on failure."""
    lock = _StartupLock(sse_port)
    try:
        await lock.__aenter__()
    except LockAcquisitionError:
        # Bounded contention: another ensure owns the lock. Re-check health a
        # few times and exit on observed state — never block indefinitely.
        logger.info("Startup lock held — waiting for another ensure to finish")
        for _ in range(_LOCK_CONTENTION_RECHECK_TRIES):
            await asyncio.sleep(_LOCK_CONTENTION_RECHECK_INTERVAL)
            rest_ok = _rest_ready_for_ensure(_rest_health(rest_port))
            sse_ok = _sse_tcp_up(sse_port) and await _sse_verify(sse_port)
            if rest_ok and sse_ok:
                print("REST + SSE ready (another ensure succeeded)")
                return 0
        print(
            "ERROR: another ensure is running and services are still not ready.",
            file=sys.stderr,
        )
        return 1

    try:
        if not await _reconcile_rest(rest_port, cdp_port, config_path, log_level):
            print("ERROR: REST did not become healthy.", file=sys.stderr)
            return 1
        if not await _reconcile_sse(sse_port, cdp_port, config_path, log_level):
            print("ERROR: SSE did not become ready.", file=sys.stderr)
            return 1
        print("REST + SSE ready")
        return 0
    finally:
        await lock.__aexit__(None, None, None)
