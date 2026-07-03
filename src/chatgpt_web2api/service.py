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
import logging
import signal
import sys
import time

from .api_server import APIServer
from .breakers import BreakerRegistry
from .cdp_driver import CDPDriver
from .chrome import ChromeProcess
from .config import Config
from .lock_resolver import OwnedTabRequiredError
from .tab_registry import TabRegistry

logger = logging.getLogger(__name__)


class Service:
    """ChatGPT-Web2API service."""

    def __init__(self, config: Config) -> None:
        self._config = config
        self._chrome: ChromeProcess | None = None
        self._driver: CDPDriver | None = None
        self._server: APIServer | None = None
        self._runner = None
        self._shutdown_event = asyncio.Event()

    async def start(self) -> None:
        """Start the full system."""
        cfg = self._config

        # Enable reactive diagnostics capture if the operator opted in.
        # Off by default; only writes artifacts when W2A_DIAGNOSE=1.
        from .diagnostics import apply_env_enablement

        apply_env_enablement()

        # 0. Circuit-breaker registry (Phase 4 PR2) — one per REST process,
        # shared by Chrome (crash-loop), the CDP driver (auth/composer/CDP),
        # and the API server (snapshot + fail-fast). Constructed first so every
        # component below can record into the same registry.
        self._breakers = BreakerRegistry()

        # 1. Chrome
        logger.info("Ensuring Chrome is running...")
        self._chrome = ChromeProcess(cfg, breakers=self._breakers)
        await self._chrome.ensure_running()
        await self._chrome.start_monitor()

        # 2. CDP driver (with login detection)
        logger.info("Connecting CDP driver...")
        self._driver = CDPDriver(
            cdp_port=cfg.chrome.cdp_port,
            tab_mode=cfg.chatgpt.tab_mode,
            instance_id=TabRegistry.derive_instance_id(
                cdp_port=cfg.chrome.cdp_port,
                server_identity=f"rest:{cfg.server.port}",
            ),
            breakers=self._breakers,
            parallel_tabs=cfg.chatgpt.parallel_tabs,
        )

        try:
            await self._driver.connect()
        except OwnedTabRequiredError:
            # Parallel-mode fail-closed must propagate, not become a login wait.
            raise
        except Exception as e:
            logger.info("Auth failed: %s — waiting for login", e)
            # Not logged in — wait for user to complete login
            await self._wait_for_login()
            await self._driver.connect()

        # 3. API server
        self._server = APIServer(cfg, self._driver, breakers=self._breakers)
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

        cfg = self._config
        self._check_bind_safety(cfg)  # fail-fast before binding

        runner = web.AppRunner(self._server.app)
        await runner.setup()

        site = web.TCPSite(runner, cfg.server.host, cfg.server.port)
        await site.start()

        return runner

    @staticmethod
    def _check_bind_safety(cfg: Config) -> None:
        """Fail-fast guard against exposing an unauthenticated API remotely.

        Behavior matrix (agreed in review):
          loopback + no keys              → allow, banner "local no-auth"
          non-loopback + keys             → allow, banner "remote with auth"
          non-loopback + no keys, no env  → raise (fail startup)
          non-loopback + no keys + env    → allow, LOUD warning

        The env override is ``W2A_ALLOW_UNAUTH_REMOTE=1``. The error message
        names it so a user who hits the failure knows the escape hatch.
        """
        import os

        # Normalize empty/None to explicit loopback — security defaults must
        # not rely on empty-string semantics (aiohttp's empty-host bind is
        # loopback today, but making it explicit avoids ambiguity).
        host = (cfg.server.host or "127.0.0.1").lower()
        loopback = host in ("127.0.0.1", "::1", "localhost")
        has_keys = bool(cfg.server.api_keys)
        allow_unauth_remote = os.environ.get("W2A_ALLOW_UNAUTH_REMOTE", "").strip() == "1"

        if loopback and not has_keys:
            logger.warning(
                "API bound to loopback (%s) with no api_keys — local no-auth "
                "mode. Safe for single-user local use; do NOT bind remotely "
                "without setting api_keys.",
                host or "127.0.0.1",
            )
        elif not loopback and has_keys:
            logger.warning(
                "API bound to non-loopback %s — network-reachable. Auth is "
                "ENABLED (api_keys set). Confirm this interface is intended.",
                host,
            )
        elif not loopback and not has_keys and not allow_unauth_remote:
            raise RuntimeError(
                f"Refusing to start: API bound to non-loopback {host} with no "
                f"api_keys configured — this would expose an unauthenticated "
                f"OpenAI-compatible API to the network. Set 'api_keys' in your "
                f"config, bind to 127.0.0.1, or set W2A_ALLOW_UNAUTH_REMOTE=1 "
                f"to override (NOT recommended)."
            )
        elif not loopback and not has_keys and allow_unauth_remote:
            logger.warning(
                "WARNING: API bound to non-loopback %s with no api_keys — "
                "UNAUTHENTICATED REMOTE ACCESS enabled via "
                "W2A_ALLOW_UNAUTH_REMOTE=1. Anyone who can reach this host "
                "can use your ChatGPT account. This is strongly discouraged.",
                host,
            )

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
