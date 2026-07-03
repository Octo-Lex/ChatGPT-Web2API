"""Chrome subprocess manager.

Owns the entire Chrome lifecycle:
  - Find existing Chrome by CDP port or user data dir
  - Launch new Chrome with --remote-debugging-port if needed
  - Monitor health via CDP ping
  - Auto-restart on crash
  - Clean shutdown
"""

from __future__ import annotations

import asyncio
import json
import logging
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

from .breakers import BreakerKind, BreakerRegistry
from .config import Config
from .cross_process_lock import CrossProcessLock, chrome_launch_lock_key

logger = logging.getLogger(__name__)

# Health-monitor tick interval (seconds). Module-level so tests/operators can
# shorten it; the monitor reads this attribute each iteration.
MONITOR_INTERVAL_S = 30


class ChromeProcess:
    """Manages a Chrome subprocess with CDP access."""

    def __init__(self, config: Config, breakers: BreakerRegistry | None = None) -> None:
        self._cfg = config.chrome
        self._process: subprocess.Popen | None = None
        self._monitor_task: asyncio.Task | None = None
        self._healthy = False
        self._started_at: float = 0
        self._restart_count = 0
        # Phase 4 PR2: optional breaker registry. When set, each restart records
        # a CHROME_CRASH_LOOP failure (3-in-300s trips it). None = back-compat.
        # No record_success here — clearing the rolling crash history on every
        # restart would defeat the 3-in-300s policy; recovery is cooldown/half-
        # open only.
        self._breakers = breakers
        # PR2/5: lifecycle ownership. True only for the process that launched
        # Chrome. Mirrors the driver's ``_owns_target`` pattern (cdp_driver.py):
        # a process that did not create the resource never restarts/kills it.
        self._owns_chrome: bool = False

    # ── Public API ────────────────────────────────────────────
    #
    # Locking invariant (PR2/5):
    #   - The chrome-launch election lock is acquired ONLY by ensure_running()
    #     and restart().
    #   - Mutation locks (target / port) are acquired ONLY by the REST/MCP
    #     send/mutation paths.
    #   - No code path may hold both. If that ever becomes necessary, the
    #     lifecycle/send lock ordering must be redesigned first — nesting them
    #     here is a deadlock flag, not a fix.

    async def ensure_running(self) -> None:
        """Make sure Chrome is running with CDP enabled.

        Determines lifecycle ownership (``_owns_chrome``):
          - A live owned process + CDP alive → stay owner (re-call guard).
          - A dead owned process → clear stale handle, fall through.
          - CDP alive with no live local process → attach as non-owner.
          - CDP down → win a cold-start election (hold the chrome-launch lock
            through launch + readiness) and become owner.

        On launch/readiness failure, ownership is dropped and the exception
        re-raised (a fresh process may later elect).
        """
        # Live local Chrome process: this object owns its lifecycle regardless
        # of CDP state. Must be handled BEFORE the dead-process clear and the
        # foreign-attach branches, otherwise a live process + CDP-down would
        # fall through to the cold-start election and launch a competing Chrome
        # (orphaning the live one) or demote ownership against a foreign Chrome.
        if self._process is not None and self._process.poll() is None:
            self._owns_chrome = True
            if await self._cdp_alive():
                self._healthy = True
                return
            # Process live but CDP wedged — restart it (takes the election
            # lock, kills, records the crash-loop failure, relaunches, waits).
            logger.warning(
                "Owned Chrome process running but CDP not responding; restarting"
            )
            await self.restart()
            return

        # Clear a stale owned-process handle before deciding attach vs launch.
        # Without this, a dead _process + foreign Chrome on the port would let
        # us falsely claim ownership of someone else's Chrome.
        if self._process is not None and self._process.poll() is not None:
            logger.warning(
                "Owned Chrome process exited with code %s", self._process.returncode
            )
            self._process = None
            self._owns_chrome = False
            self._healthy = False

        # Attacher: another process's Chrome is already up.
        if await self._cdp_alive():
            logger.info("Found existing Chrome on CDP port %d", self._cfg.cdp_port)
            self._owns_chrome = False
            self._healthy = True
            return

        # Cold-start election: serialize the check→launch→ready transition.
        # Holding the lock through _wait_for_cdp prevents a second starter from
        # seeing a not-yet-ready Chrome as dead and double-launching.
        async with CrossProcessLock(
            cdp_port=self._cfg.cdp_port, lock_key=chrome_launch_lock_key()
        ):
            if await self._cdp_alive():
                # Lost the election — another process launched while we waited.
                self._owns_chrome = False
                self._healthy = True
                return
            self._owns_chrome = True
            try:
                await self._launch()
                await self._wait_for_cdp(timeout=30)
            except Exception:
                # Drop ownership so a fresh process can elect; clean up the
                # half-started Chrome so the next starter isn't competing.
                await self._kill()
                self._owns_chrome = False
                self._healthy = False
                raise

    async def restart(self) -> None:
        """Kill and relaunch Chrome. Owner-only; no-op for attachers.

        Records a CHROME_CRASH_LOOP failure on every attempt (including ones
        that fail during _wait_for_cdp), so the crash-loop breaker sees every
        retry. On failure, cleans up the half-launched Chrome but KEEPS
        ownership — the owner retries on the next monitor tick, and no other
        running process can recover an orphaned Chrome at runtime.
        """
        if not self._owns_chrome:
            logger.warning("Refusing Chrome restart: this process does not own it")
            return
        async with CrossProcessLock(
            cdp_port=self._cfg.cdp_port, lock_key=chrome_launch_lock_key()
        ):
            logger.warning("Restarting Chrome (restart #%d)", self._restart_count + 1)
            await self._kill()
            self._restart_count += 1
            if self._breakers:
                self._breakers.record_failure(BreakerKind.CHROME_CRASH_LOOP)
            try:
                await self._launch()
                await self._wait_for_cdp(timeout=30)
            except Exception:
                # _launch may have partially succeeded and set _process; the
                # cleanup _kill reclaims it. Keep _owns_chrome=True so the
                # owner (not a fresh elector) retries on the next tick.
                await self._kill()
                self._healthy = False
                raise

    async def stop(self) -> None:
        """Stop Chrome cleanly. Non-owners cancel their monitor but do not kill
        Chrome (they never owned it). Owners kill and relinquish ownership."""
        if self._monitor_task:
            self._monitor_task.cancel()
            try:
                await self._monitor_task
            except asyncio.CancelledError:
                pass
            self._monitor_task = None
        if not self._owns_chrome:
            logger.debug("Chrome stop skipped: this process does not own it")
            return
        await self._kill()
        self._owns_chrome = False

    @property
    def healthy(self) -> bool:
        return self._healthy

    @property
    def owns_chrome(self) -> bool:
        """True if this process launched (and therefore owns the lifecycle of)
        the Chrome subprocess. Attachers are False."""
        return self._owns_chrome

    @property
    def restart_count(self) -> int:
        return self._restart_count

    def get_page_targets(self) -> list[dict]:
        """Get all page targets from CDP."""
        try:
            req = urllib.request.Request(f"http://127.0.0.1:{self._cfg.cdp_port}/json/list")
            with urllib.request.urlopen(req, timeout=5) as resp:
                return json.loads(resp.read())
        except Exception:
            return []

    # ── Launch / Kill ─────────────────────────────────────────

    async def _launch(self) -> None:
        chrome = self._cfg.chrome_path
        user_dir = self._cfg.user_data_dir
        port = self._cfg.cdp_port

        # Ensure user data dir exists
        Path(user_dir).mkdir(parents=True, exist_ok=True)

        args = [
            chrome,
            f"--remote-debugging-port={port}",
            f"--user-data-dir={user_dir}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-background-networking",
            "--disable-client-side-phishing-detection",
            "--disable-default-apps",
            "--disable-hang-monitor",
            "--disable-popup-blocking",
            "--disable-prompt-on-repost",
            "--disable-sync",
            "--metrics-recording-only",
            "--safebrowsing-disable-auto-update",
        ]

        if self._cfg.headless:
            args.append("--headless=new")

        args.extend(self._cfg.extra_args)

        # Add chatgpt.com as start URL
        args.append("https://chatgpt.com")

        logger.info("Launching Chrome: %s", " ".join(args[:4]) + " ...")

        self._process = subprocess.Popen(
            args,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0,
        )
        self._started_at = time.monotonic()
        logger.info("Chrome PID: %d", self._process.pid)

    async def _kill(self) -> None:
        if self._process is None:
            return

        pid = self._process.pid
        logger.info("Stopping Chrome PID %d", pid)

        try:
            if sys.platform == "win32":
                # Windows: taskkill to kill the entire process tree
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(pid)],
                    capture_output=True,
                    timeout=10,
                )
            else:
                self._process.terminate()
                try:
                    self._process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self._process.kill()
        except Exception as e:
            logger.warning("Error stopping Chrome: %s", e)

        self._process = None
        self._healthy = False

    # ── Health ────────────────────────────────────────────────

    async def _cdp_alive(self) -> bool:
        """Check if CDP is responding."""
        try:
            req = urllib.request.Request(f"http://127.0.0.1:{self._cfg.cdp_port}/json/version")
            with urllib.request.urlopen(req, timeout=3) as resp:
                return resp.status == 200
        except Exception:
            return False

    async def _wait_for_cdp(self, timeout: float = 30) -> None:
        """Wait for Chrome's CDP to start responding."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if await self._cdp_alive():
                self._healthy = True
                logger.info("CDP ready on port %d", self._cfg.cdp_port)
                return
            await asyncio.sleep(0.5)
        raise TimeoutError(f"Chrome CDP did not respond within {timeout}s")

    async def start_monitor(self) -> None:
        """Start the background health monitor. Runs in ALL processes —
        attachers observe health (so /health stays accurate); only the owner
        ever restarts. See _monitor_loop for the ownership/breaker gating."""
        if self._monitor_task and not self._monitor_task.done():
            return
        self._monitor_task = asyncio.create_task(self._monitor_loop())

    async def _monitor_loop(self) -> None:
        """Periodically observe Chrome health (all processes) and restart
        (owner-only). Health is updated first in every branch so /health stays
        truthful even when restart is intentionally suppressed."""
        while True:
            try:
                await asyncio.sleep(MONITOR_INTERVAL_S)
                alive = await self._cdp_alive()
                # Update _healthy FIRST — truthful health beats restart logic.
                was_healthy = self._healthy
                if alive != self._healthy:
                    self._healthy = alive
                if alive:
                    if not was_healthy:
                        logger.info("Chrome CDP recovered")
                    continue
                # CDP is down.
                if not self._owns_chrome:
                    logger.error("Chrome CDP stopped; not owner, not restarting")
                    continue
                if not self._cfg.restart_on_crash:
                    logger.error("Chrome CDP stopped; restart_on_crash=false")
                    continue
                if self._breakers and self._breakers.is_open(
                    BreakerKind.CHROME_CRASH_LOOP
                ):
                    logger.error(
                        "Chrome crash-loop breaker open; suppressing restart"
                    )
                    continue
                logger.warning(
                    "Chrome died — restarting (attempt #%d)...", self._restart_count + 1
                )
                try:
                    await self.restart()
                    logger.info("Chrome restarted successfully")
                except Exception as e:
                    logger.error("Chrome restart failed: %s", e)
            except asyncio.CancelledError:
                return
            except Exception as e:
                logger.error("Health monitor error: %s", e)

    # ── Context manager ───────────────────────────────────────

    async def __aenter__(self) -> ChromeProcess:
        await self.ensure_running()
        return self

    async def __aexit__(self, *exc) -> None:
        await self.stop()
