"""Service orchestrator — top-level lifecycle manager.

Owns the entire system:
  1. Load config
  2. Start/attach Chrome
  3. Connect CDP driver
  4. Start API server
  5. Signal handling + graceful shutdown
"""

from __future__ import annotations

import asyncio
import json
import logging
import signal
import sys
import time
from typing import Optional

from .chrome import ChromeProcess
from .cdp_driver import CDPDriver
from .api_server import APIServer
from .config import Config

logger = logging.getLogger(__name__)


class Service:
    """ChatGPT-Web2API service."""

    def __init__(self, config: Config) -> None:
        self._config = config
        self._chrome: Optional[ChromeProcess] = None
        self._driver: Optional[CDPDriver] = None
        self._server: Optional[APIServer] = None
        self._runner = None
        self._shutdown_event = asyncio.Event()

    async def start(self) -> None:
        """Start the full system."""
        cfg = self._config

        # Enable reactive diagnostics capture if the operator opted in.
        # Off by default; only writes artifacts when W2A_DIAGNOSE=1.
        from .diagnostics import apply_env_enablement
        apply_env_enablement()

        # 1. Chrome
        logger.info("Ensuring Chrome is running...")
        self._chrome = ChromeProcess(cfg)
        await self._chrome.ensure_running()
        await self._chrome.start_monitor()

        # 2. CDP driver (with login detection)
        logger.info("Connecting CDP driver...")
        self._driver = CDPDriver(
            cdp_port=cfg.chrome.cdp_port, tab_mode=cfg.chatgpt.tab_mode
        )

        try:
            await self._driver.connect()
        except Exception as e:
            logger.info("Auth failed: %s — waiting for login", e)
            # Not logged in — wait for user to complete login
            await self._wait_for_login()
            await self._driver.connect()

        # 3. API server
        self._server = APIServer(cfg, self._driver)
        self._runner = await self._start_server()

        self._print_banner()

        # 4. Wait for shutdown signal
        await self._shutdown_event.wait()

    async def _wait_for_login(self, timeout: int = 300) -> None:
        """Wait for the user to log into ChatGPT in the Chrome window."""
        print()
        print("=" * 52)
        print("  NOT LOGGED IN")
        print("=" * 52)
        print()
        print("  Chrome is open. Log into ChatGPT in the browser window.")
        print("  Waiting for login...")
        print()

        # Navigate to login page if not already there
        try:
            await self._driver._cdp("Page.navigate", {"url": "https://chatgpt.com/"})
        except Exception:
            pass

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                # Try to get an auth token
                raw = await self._driver._js(
                    "(async () => {"
                    "  try {"
                    "    const r = await fetch('/api/auth/session', {credentials:'include'});"
                    "    const d = await r.json();"
                    "    return d.accessToken || '';"
                    "  } catch(e) { return ''; }"
                    "})()"
                )
                if raw and len(raw) > 100:
                    print("  Login detected!")
                    print()
                    return
            except Exception:
                pass
            await asyncio.sleep(2)

        raise TimeoutError(f"Login not completed within {timeout}s")

    async def stop(self) -> None:
        """Graceful shutdown."""
        logger.info("Shutting down...")

        if self._runner:
            await self._runner.cleanup()

        if self._driver:
            await self._driver.close()

        if self._chrome:
            await self._chrome.stop()

        logger.info("Service stopped")

    async def _start_server(self):
        from aiohttp import web

        runner = web.AppRunner(self._server.app)
        await runner.setup()

        cfg = self._config
        site = web.TCPSite(runner, cfg.server.host, cfg.server.port)
        await site.start()

        return runner

    def _print_banner(self) -> None:
        cfg = self._config
        host = cfg.server.host
        port = cfg.server.port

        print()
        print("=" * 52)
        print("       ChatGPT-Web2API -- CDP Proxy")
        print("=" * 52)
        print()
        print(f"  Chrome:   PID running on CDP port {cfg.chrome.cdp_port}")
        print(f"  API:      http://{host}:{port}")
        print()
        print("  Endpoints:")
        print(f"    POST  {host}:{port}/v1/chat/completions")
        print(f"    GET   {host}:{port}/v1/models")
        print(f"    GET   {host}:{port}/v1/projects")
        print(f"    GET   {host}:{port}/health")
        print()
        print("  Ctrl+C to stop")
        print()

    def request_shutdown(self) -> None:
        self._shutdown_event.set()


async def run_service(config: Config) -> None:
    """Run the service with signal handling."""
    service = Service(config)

    loop = asyncio.get_running_loop()

    # Signal handlers (Unix)
    if sys.platform != "win32":
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, service.request_shutdown)
    else:
        # Windows: Ctrl+C raises KeyboardInterrupt in asyncio.run()
        pass

    try:
        await service.start()
    except KeyboardInterrupt:
        pass
    finally:
        await service.stop()
