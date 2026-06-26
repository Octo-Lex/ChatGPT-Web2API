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

Exit codes:
  - 0: REST and SSE are ready
  - 1: generic reconcile failure (REST/SSE could not be made ready)
  - 2: auth/login needed — REST degraded because the ``auth_required`` breaker
       is open. REST is NOT restarted and SSE is NOT reconciled in this case;
       a human re-auth is required.
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
from dataclasses import dataclass
from pathlib import Path

from mcp import ClientSession
from mcp.client.sse import sse_client

from chatgpt_web2api.cross_process_lock import LockAcquisitionError

logger = logging.getLogger(__name__)

# Bounded wait for REST to resolve starting/degraded before we act.
_REST_HEALTHY_TIMEOUT = 30.0
# Startup lock: bounded contention wait.
_LOCK_TIMEOUT = 10.0
_LOCK_CONTENTION_RECHECK_INTERVAL = 3.0
_LOCK_CONTENTION_RECHECK_TRIES = 3
# SSE readiness: TCP preflight then real MCP handshake.
_SSE_VERIFY_TIMEOUT = 15.0


# Default ensure-policy tunables. Overridable via the ``ensure`` config section
# / ``W2A_ENSURE_*`` env (see EnsureConfig). Module constants stay as the
# built-in defaults used when no config is loaded.
_DEFAULT_DEGRADED_POLL_INTERVAL = 2.0
_DEFAULT_DEGRADED_POLL_BUDGET = 20.0
_DEFAULT_BREAKER_COOLDOWN_GRACE = 5.0


@dataclass(frozen=True)
class EnsurePolicy:
    """Ensure-side reconcile tunables, sourced from ``EnsureConfig``.

    Kept separate from ``Config`` so the reconcile logic depends on a tiny
    frozen value object rather than the whole config tree."""

    degraded_poll_interval_s: float = _DEFAULT_DEGRADED_POLL_INTERVAL
    degraded_poll_budget_s: float = _DEFAULT_DEGRADED_POLL_BUDGET
    breaker_cooldown_grace_s: float = _DEFAULT_BREAKER_COOLDOWN_GRACE


@dataclass(frozen=True)
class DegradedBreakerState:
    """Why REST is degraded, per the ``/health`` breaker snapshot.

    - ``auth_open``: the sticky ``auth_required`` breaker is open (needs login).
    - ``timed_open_kind``: a timed breaker (composer/cdp/chrome) is open.
    - ``cooldown_remaining_s``: seconds left on that timed trip; ``None`` means
      the value was missing/malformed (treat as legacy degraded).
    """

    auth_open: bool = False
    timed_open_kind: str | None = None
    cooldown_remaining_s: float | None = None


@dataclass(frozen=True)
class RestReconcileResult:
    """Outcome of ``_reconcile_rest``. ``auth_needed`` short-circuits SSE."""

    ok: bool
    auth_needed: bool = False


def _classify_degraded_breakers(breakers: dict) -> DegradedBreakerState:
    """Classify a degraded-REST cause from the ``/health`` breaker snapshot.

    Returns the first open breaker encountered. Auth is identified by
    ``kind == "auth_required"`` — NEVER by ``cooldown_seconds_remaining is None``
    (that field is overloaded: None also means a closed breaker). Any non-auth
    open breaker is reported as a timed trip with its remaining cooldown.

    Missing/malformed ``cooldown_seconds_remaining`` yields
    ``cooldown_remaining_s=None``; the caller treats that as legacy degraded
    behavior (no immediate restart). Health JSON is internal, but a partial
    shape (older REST, hand-edited fixture) should fall back gracefully rather
    than crash ``ensure``.
    """
    for kind, entry in breakers.items():
        if not entry.get("open"):
            continue
        if kind == "auth_required":
            return DegradedBreakerState(auth_open=True)
        remaining = entry.get("cooldown_seconds_remaining")
        try:
            cooldown = float(remaining) if remaining is not None else None
        except (TypeError, ValueError):
            cooldown = None
        return DegradedBreakerState(timed_open_kind=kind, cooldown_remaining_s=cooldown)
    return DegradedBreakerState()


def _load_ensure_policy(config_path: str | None) -> EnsurePolicy:
    """Load the ensure-policy tunables from config. Reads ONLY ``cfg.ensure`` —
    never lets ``cfg.server.port`` / ``cfg.chrome.cdp_port`` override the
    explicit ``run_ensure`` port arguments (those remain authoritative). Falls
    back to module defaults if config loading fails or the section is absent."""
    try:
        from chatgpt_web2api.config import Config

        cfg = Config.load(config_path)
        return EnsurePolicy(
            degraded_poll_interval_s=cfg.ensure.degraded_poll_interval_s,
            degraded_poll_budget_s=cfg.ensure.degraded_poll_budget_s,
            breaker_cooldown_grace_s=cfg.ensure.breaker_cooldown_grace_s,
        )
    except Exception as e:
        logger.debug("ensure config load failed (%s) — using defaults", e)
        return EnsurePolicy()


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
    rest_port: int,
    cdp_port: int,
    config_path: str | None,
    log_level: str | None,
    policy: EnsurePolicy,
) -> RestReconcileResult:
    """Apply the degraded-REST policy and wait for ready.

    Returns ``RestReconcileResult(ok=True)`` when REST is ready for ensure,
    ``ok=False`` otherwise. ``auth_needed=True`` signals the sticky
    ``auth_required`` breaker is open — REST must NOT be restarted and SSE must
    NOT be reconciled; a human re-auth is required (``run_ensure`` returns 2)."""
    h = _rest_health(rest_port)
    if h is None:
        logger.info("REST missing — starting")
        _launch_detached(_build_rest_cmd(rest_port, cdp_port, config_path, log_level))
        ok = await _wait_rest_ready(rest_port, _REST_HEALTHY_TIMEOUT)
        return RestReconcileResult(ok=ok)

    status = h.get("status", "broken")
    if status in ("healthy", "starting") and _rest_ready_for_ensure(h):
        logger.info("REST ready (status=%s)", status)
        return RestReconcileResult(ok=True)

    if status == "broken":
        logger.info("REST broken (Chrome down) — restarting")
        ok = await _restart_rest(rest_port, cdp_port, config_path, log_level)
        return RestReconcileResult(ok=ok)

    if status == "degraded":
        return await _reconcile_degraded(rest_port, cdp_port, config_path, log_level, policy, h)

    # starting without full connectivity — wait for it to resolve
    logger.info("REST starting (not fully connected) — waiting")
    ok = await _wait_rest_ready(rest_port, _REST_HEALTHY_TIMEOUT)
    return RestReconcileResult(ok=ok)


async def _reconcile_degraded(
    rest_port: int,
    cdp_port: int,
    config_path: str | None,
    log_level: str | None,
    policy: EnsurePolicy,
    h: dict,
) -> RestReconcileResult:
    """Breaker-aware degraded-REST reconcile.

    Dispatch on the ``/health`` breaker snapshot (read fresh each poll):

      - ``auth_required`` open → login needed. Do NOT restart, do NOT poll.
        Return ``auth_needed`` immediately.
      - a timed breaker open with a known cooldown → wait up to
        ``cooldown + grace`` for it to half-open/recover before restarting.
      - a timed breaker open with an unknown/missing cooldown (legacy/malformed
        health) OR no open breaker → legacy degraded behavior: poll the existing
        ``degraded_poll_budget_s`` window, then restart.

    A timed breaker that is still open past ``cooldown + grace`` is stuck
    (half-open recovery isn't progressing) → restart. At the exact cooldown
    boundary (``cooldown_seconds_remaining <= 0`` but still open) we re-fetch
    health once before restarting, to avoid racing a recovery in flight.
    """
    cls = _classify_degraded_breakers(h.get("breakers", {}))

    # Auth: sticky breaker, needs human login. Never restart for this.
    if cls.auth_open:
        logger.warning(
            "REST degraded: auth_required breaker open — login needed "
            "(manual re-auth required); not restarting REST"
        )
        return RestReconcileResult(ok=False, auth_needed=True)

    # Timed breaker with a known cooldown: wait for it to recover.
    if cls.timed_open_kind is not None and cls.cooldown_remaining_s is not None:
        return await _wait_timed_breaker(
            rest_port, cdp_port, config_path, log_level, policy, cls
        )

    # Legacy degraded: no open breaker info (transient CDP reconnect, older
    # REST, or malformed snapshot). Poll the standard budget before bouncing.
    logger.info(
        "REST degraded — waiting up to %.0fs before restart", policy.degraded_poll_budget_s
    )
    deadline = time.monotonic() + policy.degraded_poll_budget_s
    while time.monotonic() < deadline:
        await asyncio.sleep(policy.degraded_poll_interval_s)
        h = _rest_health(rest_port)
        if _rest_ready_for_ensure(h):
            logger.info("REST recovered from degraded")
            return RestReconcileResult(ok=True)
        if h and h.get("status") == "broken":
            break  # fall through to restart
        # A breaker may open mid-poll (e.g. session lapsed or a timed trip
        # fires during the wait). Reclassify and dispatch accordingly.
        cls = _classify_degraded_breakers((h or {}).get("breakers", {}))
        if cls.auth_open:
            logger.warning(
                "REST degraded: auth_required breaker open — login needed; not restarting"
            )
            return RestReconcileResult(ok=False, auth_needed=True)
        # A timed breaker opened with a known cooldown: switch to the
        # cooldown+grace wait rather than letting the (possibly shorter)
        # legacy budget expire into a premature restart.
        if cls.timed_open_kind is not None and cls.cooldown_remaining_s is not None:
            logger.info(
                "Timed breaker '%s' opened during degraded poll — switching to "
                "cooldown+grace wait",
                cls.timed_open_kind,
            )
            return await _wait_timed_breaker(
                rest_port, cdp_port, config_path, log_level, policy, cls
            )
    logger.info("REST still degraded after %.0fs — restarting", policy.degraded_poll_budget_s)
    ok = await _restart_rest(rest_port, cdp_port, config_path, log_level)
    return RestReconcileResult(ok=ok)


async def _wait_timed_breaker(
    rest_port: int,
    cdp_port: int,
    config_path: str | None,
    log_level: str | None,
    policy: EnsurePolicy,
    cls: DegradedBreakerState,
) -> RestReconcileResult:
    """Wait for a timed breaker to half-open/recover, then fall back to restart.

    Deadline is ``cooldown_remaining + grace``. On each tick: recovered → ok;
    status ``broken`` → restart immediately; still open at ``cooldown<=0`` →
    re-fetch health once (a recovery may be in flight at the boundary) and only
    restart if it is still open after that re-fetch.
    """
    cooldown = cls.cooldown_remaining_s or 0.0
    budget = cooldown + policy.breaker_cooldown_grace_s
    logger.info(
        "REST degraded: timed breaker '%s' open — waiting up to %.1fs "
        "(cooldown %.1fs + grace %.1fs) for recovery",
        cls.timed_open_kind,
        budget,
        cooldown,
        policy.breaker_cooldown_grace_s,
    )
    deadline = time.monotonic() + budget
    while time.monotonic() < deadline:
        await asyncio.sleep(policy.degraded_poll_interval_s)
        h = _rest_health(rest_port)
        if _rest_ready_for_ensure(h):
            logger.info("REST recovered from degraded (breaker closed)")
            return RestReconcileResult(ok=True)
        if h and h.get("status") == "broken":
            break  # fall through to restart
        cur = _classify_degraded_breakers((h or {}).get("breakers", {}))
        if cur.auth_open:
            logger.warning(
                "REST degraded: auth_required breaker open — login needed; not restarting"
            )
            return RestReconcileResult(ok=False, auth_needed=True)
        remaining = cur.cooldown_remaining_s
        if (
            cur.timed_open_kind is not None
            and remaining is not None
            and remaining <= 0
        ):
            # Boundary: cooldown just elapsed and a half-open probe may be in
            # flight. Re-fetch once before deciding to restart.
            logger.info("Timed breaker at cooldown boundary — re-fetching health once")
            h2 = _rest_health(rest_port)
            if _rest_ready_for_ensure(h2):
                return RestReconcileResult(ok=True)
            if h2 and h2.get("status") == "broken":
                break  # fall through to restart
            cur2 = _classify_degraded_breakers((h2 or {}).get("breakers", {}))
            if cur2.auth_open:
                logger.warning(
                    "REST degraded: auth_required breaker open — login needed; not restarting"
                )
                return RestReconcileResult(ok=False, auth_needed=True)
            if cur2.timed_open_kind is not None:
                break  # still timed-open after boundary re-fetch → restart
            # No breaker open on h2, but REST is still not ready (e.g.
            # driver_connected=false). Do NOT return ok=True — keep polling
            # the remaining deadline so we don't proceed to SSE on an
            # unready REST.
    logger.info("Timed breaker still open past cooldown+grace — restarting")
    logger.info("Timed breaker still open past cooldown+grace — restarting")
    ok = await _restart_rest(rest_port, cdp_port, config_path, log_level)
    return RestReconcileResult(ok=ok)


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
    """Point-in-time reconcile of REST + SSE.

    Exit codes:
      - 0: REST + SSE ready
      - 1: generic reconcile failure
      - 2: auth/login needed (auth_required breaker open; REST not restarted,
           SSE not reconciled)
    """
    # Ensure-policy tunables come from config (cfg.ensure ONLY); explicit
    # run_ensure port args remain authoritative and are never overridden.
    policy = _load_ensure_policy(config_path)
    lock = _StartupLock(sse_port)
    try:
        await lock.__aenter__()
    except LockAcquisitionError:
        # Bounded contention: another ensure owns the lock. Re-check health a
        # few times and exit on observed state — never block indefinitely.
        logger.info("Startup lock held — waiting for another ensure to finish")
        for _ in range(_LOCK_CONTENTION_RECHECK_TRIES):
            await asyncio.sleep(_LOCK_CONTENTION_RECHECK_INTERVAL)
            h = _rest_health(rest_port)
            rest_ok = _rest_ready_for_ensure(h)
            sse_ok = _sse_tcp_up(sse_port) and await _sse_verify(sse_port)
            if rest_ok and sse_ok:
                print("REST + SSE ready (another ensure succeeded)")
                return 0
            # Surface the auth case distinctly even under contention.
            if h and h.get("status") == "degraded":
                if _classify_degraded_breakers(h.get("breakers", {})).auth_open:
                    print(
                        "REST degraded: auth_required breaker open — login needed.",
                        file=sys.stderr,
                    )
                    return 2
        print(
            "ERROR: another ensure is running and services are still not ready.",
            file=sys.stderr,
        )
        return 1

    try:
        result = await _reconcile_rest(rest_port, cdp_port, config_path, log_level, policy)
        if not result.ok:
            if result.auth_needed:
                # Auth needed: do NOT reconcile SSE — a login is required first.
                print(
                    "REST degraded: auth_required breaker open — login needed.",
                    file=sys.stderr,
                )
                return 2
            print("ERROR: REST did not become healthy.", file=sys.stderr)
            return 1
        if not await _reconcile_sse(sse_port, cdp_port, config_path, log_level):
            print("ERROR: SSE did not become ready.", file=sys.stderr)
            return 1
        print("REST + SSE ready")
        return 0
    finally:
        await lock.__aexit__(None, None, None)
