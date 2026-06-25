"""ChatGPT-Web2API — OpenAI-compatible proxy via CDP.

Usage:
    chatgpt-web2api                          # all defaults
    chatgpt-web2api --config config.json     # from config file
    chatgpt-web2api --port 9090              # override port
    chatgpt-web2api --cdp-port 9333          # override CDP port
    chatgpt-web2api --headless               # headless Chrome
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys

from .config import Config
from .service import run_service


def inject_cookies(args) -> None:
    """Inject cookies from a JSON file into the Chrome profile."""

    config = Config.load(args.config)
    if args.cdp_port:
        config.chrome.cdp_port = args.cdp_port
    if args.user_data_dir:
        config.chrome.user_data_dir = args.user_data_dir

    cookie_file = args.cookies
    if not os.path.exists(cookie_file):
        print(f"Error: Cookie file not found: {cookie_file}")
        sys.exit(1)

    # Load cookies from file
    with open(cookie_file) as f:
        data = json.load(f)

    # Support both arrays and Netscape format
    if isinstance(data, list):
        cookies = data
    else:
        print("Error: Cookie file must be a JSON array of cookie objects")
        sys.exit(1)

    print(f"Loaded {len(cookies)} cookies from {cookie_file}")

    # Inject via CDP if Chrome is running
    import asyncio
    import urllib.request

    try:
        req = urllib.request.Request(f"http://127.0.0.1:{config.chrome.cdp_port}/json/list")
        with urllib.request.urlopen(req, timeout=3) as resp:
            targets = json.loads(resp.read())
    except Exception:
        print(f"Error: Chrome not running on CDP port {config.chrome.cdp_port}")
        print("Start Chrome first: chatgpt-web2api")
        sys.exit(1)

    async def _inject():
        import websockets

        pages = [t for t in targets if t.get("type") == "page"]
        if not pages:
            print("Error: No pages found")
            return
        ws_url = pages[0]["webSocketDebuggerUrl"]
        ws = await websockets.connect(ws_url, max_size=50 * 1024 * 1024)

        # Navigate to chatgpt.com first
        await ws.send(
            json.dumps(
                {"id": 1, "method": "Page.navigate", "params": {"url": "https://chatgpt.com/"}}
            )
        )
        import asyncio as aio

        await aio.sleep(3)

        # Set cookies via CDP
        for i, cookie in enumerate(cookies):
            cdp_cookie = {
                "name": cookie.get("name", ""),
                "value": cookie.get("value", ""),
                "domain": cookie.get("domain", ".chatgpt.com"),
                "path": cookie.get("path", "/"),
                "secure": cookie.get("secure", True),
                "httpOnly": cookie.get("httpOnly", False),
            }
            if cookie.get("sameSite"):
                cdp_cookie["sameSite"] = cookie["sameSite"]
            if cookie.get("expirationDate") or cookie.get("expiry"):
                cdp_cookie["expires"] = cookie.get("expirationDate") or cookie.get("expiry")

            await ws.send(
                json.dumps({"id": i + 10, "method": "Network.setCookie", "params": cdp_cookie})
            )
            await aio.sleep(0.01)

        print(f"Injected {len(cookies)} cookies")

        # Verify by trying to get auth
        await aio.sleep(2)
        await ws.send(
            json.dumps(
                {
                    "id": 999,
                    "method": "Runtime.evaluate",
                    "params": {
                        "expression": "(async () => { var r = await fetch('/api/auth/session', {credentials:'include'}); var d = await r.json(); return d.accessToken ? 'OK:' + d.user?.name : 'FAIL'; })()",
                        "awaitPromise": True,
                        "returnByValue": True,
                        "timeout": 10000,
                    },
                }
            )
        )
        while True:
            r = await aio.wait_for(ws.recv(), timeout=15)
            resp = json.loads(r)
            if resp.get("id") == 999:
                result = resp.get("result", {}).get("result", {}).get("value", "")
                if result.startswith("OK:"):
                    print(f"Auth verified: {result}")
                else:
                    print(f"Auth failed: {result}")
                    print("Cookies may be expired. Export fresh cookies from your browser.")
                break
        await ws.close()

    asyncio.run(_inject())


def main() -> None:
    # Check if first arg looks like a subcommand
    subcommands = {"start", "inject-cookies", "doctor", "ensure"}
    has_subcommand = len(sys.argv) > 1 and sys.argv[1] in subcommands

    parser = argparse.ArgumentParser(
        prog="chatgpt-web2api",
        description="OpenAI-compatible API proxy through ChatGPT web via CDP",
    )

    if has_subcommand:
        subparsers = parser.add_subparsers(dest="command")
        start_parser = subparsers.add_parser("start", help="Start the proxy (default)")
        _add_common_args(start_parser)
        cookie_parser = subparsers.add_parser(
            "inject-cookies", help="Inject browser cookies for auth"
        )
        cookie_parser.add_argument("cookies", help="Path to cookies JSON file")
        _add_common_args(cookie_parser)
        doctor_parser = subparsers.add_parser(
            "doctor", help="Diagnose a broken function from captured evidence"
        )
        doctor_parser.add_argument(
            "function", nargs="?", help="Function to diagnose (omit to auto-discover)"
        )
        doctor_parser.add_argument(
            "--verify", metavar="FUNCTION", help="Re-run a function live to verify a fix"
        )
        ensure_parser = subparsers.add_parser(
            "ensure",
            help="Point-in-time reconcile: make REST + SSE healthy, then exit (for ZCode hooks)",
        )
        ensure_parser.add_argument(
            "--rest-port", type=int, default=8080, help="REST API port to reconcile (default: 8080)"
        )
        ensure_parser.add_argument(
            "--mcp-sse-port",
            type=int,
            default=8090,
            help="MCP/SSE port to reconcile (default: 8090)",
        )
        ensure_parser.add_argument("--config", "-c", help="Config file path (JSON)")
        ensure_parser.add_argument(
            "--cdp-port", type=int, default=9222, help="Chrome CDP port (default: 9222)"
        )
        # default=None so we can distinguish "caller provided --log-level" from
        # the default — only propagated to child subprocesses when explicit.
        ensure_parser.add_argument(
            "--log-level",
            default=None,
            choices=["DEBUG", "INFO", "WARNING", "ERROR"],
            help="Log level for child processes (default: child's own default)",
        )
    else:
        # No subcommand — parse as start with all args
        _add_common_args(parser)

    args = parser.parse_args()
    command = getattr(args, "command", "start") if has_subcommand else "start"

    if command == "start":
        _run_start(args)
    elif command == "inject-cookies":
        inject_cookies(args)
    elif command == "doctor":
        from .doctor import run_doctor

        run_doctor(args)
    elif command == "ensure":
        from .ensure import run_ensure

        code = asyncio.run(
            run_ensure(
                rest_port=args.rest_port,
                sse_port=args.mcp_sse_port,
                cdp_port=args.cdp_port,
                config_path=args.config,
                log_level=args.log_level,
            )
        )
        sys.exit(code)


def _add_common_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--config", "-c", help="Config file path (JSON)")
    parser.add_argument("--port", "-p", type=int, help="API server port (default: 8080)")
    parser.add_argument("--host", help="API server host (default: 127.0.0.1)")
    parser.add_argument("--cdp-port", type=int, help="Chrome CDP port (default: 9222)")
    parser.add_argument("--chrome-path", help="Path to Chrome binary")
    parser.add_argument("--user-data-dir", help="Chrome user data directory")
    parser.add_argument("--headless", action="store_true", help="Run Chrome headless")
    parser.add_argument(
        "--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"]
    )


def _parse_as_start(parser: argparse.ArgumentParser) -> argparse.Namespace:
    """When no subcommand is given, parse args as 'start'."""
    # Use the same parser but with common args
    start_parser = argparse.ArgumentParser()
    _add_common_args(start_parser)
    args = start_parser.parse_args()
    args.command = "start"
    return args


def _run_start(args: argparse.Namespace) -> None:
    # Load config
    config = Config.load(getattr(args, "config", None))

    # CLI overrides
    if getattr(args, "port", None):
        config.server.port = args.port
    if getattr(args, "host", None):
        config.server.host = args.host
    if getattr(args, "cdp_port", None):
        config.chrome.cdp_port = args.cdp_port
    if getattr(args, "chrome_path", None):
        config.chrome.chrome_path = args.chrome_path
    if getattr(args, "user_data_dir", None):
        config.chrome.user_data_dir = args.user_data_dir
    if getattr(args, "headless", False):
        config.chrome.headless = True
    if getattr(args, "log_level", None):
        config.log.level = args.log_level

    # Logging
    logging.basicConfig(
        level=getattr(logging, config.log.level),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    # Suppress noisy loggers
    logging.getLogger("websockets").setLevel(logging.WARNING)
    logging.getLogger("aiohttp.access").setLevel(logging.WARNING)

    # Run
    try:
        asyncio.run(run_service(config))
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()
