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
import os
import signal
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Optional

from .config import Config

logger = logging.getLogger(__name__)


class ChromeProcess:
    """Manages a Chrome subprocess with CDP access."""

    def __init__(self, config: Config) -> None:
        self._cfg = config.chrome
        self._process: Optional[subprocess.Popen] = None
        self._monitor_task: Optional[asyncio.Task] = None
        self._healthy = False
        self._started_at: float = 0
        self._restart_count = 0

    # ── Public API ────────────────────────────────────────────

    async def ensure_running(self) -> None:
        """Make sure Chrome is running with CDP enabled.

        1. Check if CDP port is already listening (existing Chrome)
        2. If not, launch Chrome with our config
        3. Wait for CDP to be ready
        """
        if await self._cdp_alive():
            logger.info("Found existing Chrome on CDP port %d", self._cfg.cdp_port)
            self._healthy = True
            return

        await self._launch()
        await self._wait_for_cdp(timeout=30)

    async def restart(self) -> None:
        """Kill and relaunch Chrome."""
        logger.warning("Restarting Chrome (restart #%d)", self._restart_count + 1)
        await self._kill()
        self._restart_count += 1
        await self._launch()
        await self._wait_for_cdp(timeout=30)

    async def stop(self) -> None:
        """Stop Chrome cleanly."""
        if self._monitor_task:
            self._monitor_task.cancel()
            try:
                await self._monitor_task
            except asyncio.CancelledError:
                pass
        await self._kill()

    @property
    def healthy(self) -> bool:
        return self._healthy

    @property
    def restart_count(self) -> int:
        return self._restart_count

    def get_page_targets(self) -> list[dict]:
        """Get all page targets from CDP."""
        try:
            req = urllib.request.Request(
                f"http://127.0.0.1:{self._cfg.cdp_port}/json/list"
            )
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
            req = urllib.request.Request(
                f"http://127.0.0.1:{self._cfg.cdp_port}/json/version"
            )
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
        """Start background health monitor."""
        self._monitor_task = asyncio.create_task(self._monitor_loop())

    async def _monitor_loop(self) -> None:
        """Periodically check Chrome health."""
        while True:
            try:
                await asyncio.sleep(30)
                alive = await self._cdp_alive()
                if alive != self._healthy:
                    self._healthy = alive
                    if not alive:
                        if self._cfg.restart_on_crash:
                            logger.warning("Chrome died — restarting (attempt #%d)...", self._restart_count + 1)
                            try:
                                await self.restart()
                                logger.info("Chrome restarted successfully")
                            except Exception as e:
                                logger.error("Chrome restart failed: %s", e)
                        else:
                            logger.error("Chrome CDP stopped responding")
                    else:
                        logger.info("Chrome CDP recovered")
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
