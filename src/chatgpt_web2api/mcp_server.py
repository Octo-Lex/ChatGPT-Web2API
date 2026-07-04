                     projects = await _driver.get_projects()
                        names = [p.get("name", "") for p in projects if p.get("name")]
                        # Filter by what user has typed
                        prefix = argument.value.lower()
                        matches = [n for n in names if prefix in n.lower()]
                        return mcp_types.Completion(
                            values=matches[:20],
                            total=len(matches),
                            hasMore=len(matches) > 20,
                        )
                    except Exception:
                        pass
        return None

    return server


# ═══════════════════════════════════════════════════════════════
# Transport Layer
# ═══════════════════════════════════════════════════════════════


def _mcp_server_identity(config: Config, transport: str, port: int) -> str:
    """Derive the tab-registry ``server_identity`` for MCP (PR4/5).

    Outside parallel mode this is the fixed ``"mcp"`` (legacy behavior). In
    parallel mode it must be unique per concurrent MCP process so two processes
    on the same CDP port don't collide on a tab-registry entry:

      - SSE: ``mcp:sse:{host}:{port}`` — unique AND stable across restart
        (host:port survives restart, so reclaim works). Mirrors REST's
        ``rest:{port}`` model.
      - stdio: ``mcp:stdio:{pid}`` — unique (one PID per process) but NOT stable
        across restart, so restart-reclaim is sacrificed. For the typical
        one-MCP-per-agent-session case, isolation beats reclaim; a leaked tab
        on restart is preferable to two sessions corrupting a shared lease.

    The result feeds ``TabRegistry.derive_instance_id``, which still honors
    ``W2A_INSTANCE_ID`` as the highest-priority override.
    """
    if not config.chatgpt.parallel_tabs:
        return "mcp"
    if transport == "sse":
        return f"mcp:sse:{config.server.host or '127.0.0.1'}:{port}"
    return f"mcp:stdio:{os.getpid()}"


async def run_mcp(config: Config, transport: str = "stdio", port: int = 8090) -> None:
    """Connect to Chrome and run the MCP server."""
    global _driver, _driver_pool, _config, _lock_cdp_port, _breakers, _parallel_tabs

    _config = config
    _lock_cdp_port = config.chrome.cdp_port
    _parallel_tabs = config.chatgpt.parallel_tabs

    if config.chatgpt.mcp_session_pool_enabled:
        # B1: pool mode. Do NOT connect to Chrome at startup. The pool
        # materializes one owned CDPDriver/tab per session on first request.
        from .mcp_driver_pool import McpSessionDriverPool

        _driver = None
        _breakers = None
        _driver_pool = McpSessionDriverPool(
            config, transport=transport, port=port,
        )
        await _driver_pool.start_sweeper()
        logger.info(
            "MCP session pool enabled (size=%d, ttl=%ds); drivers materialize on first request",
            config.chatgpt.mcp_session_pool_size,
            config.chatgpt.mcp_session_pool_ttl_seconds,
        )
    else:
        # Singleton mode: connect immediately (unchanged pre-B1 behavior).
        _driver_pool = None
        _breakers = BreakerRegistry()
        _driver = CDPDriver(
            cdp_port=config.chrome.cdp_port,
            tab_mode=config.chatgpt.tab_mode,
            instance_id=TabRegistry.derive_instance_id(
                cdp_port=config.chrome.cdp_port,
                server_identity=_mcp_server_identity(config, transport, port),
            ),
            breakers=_breakers,
            parallel_tabs=config.chatgpt.parallel_tabs,
        )
        try:
            await _driver.connect()
            logger.info("Connected to Chrome on CDP port %d", config.chrome.cdp_port)
        except Exception as e:
            logger.error(
                "Cannot connect to Chrome on CDP port %d. "
                "Run 'chatgpt-web2api' first to start Chrome. Error: %s",
                config.chrome.cdp_port,
                e,
            )
            await _driver.close()
            return

    server = create_server()
    init_options = server.create_initialization_options()

    try:
        if transport == "stdio":
            async with stdio_server() as (read, write):
                await server.run(read, write, init_options, raise_exceptions=True)
        elif transport == "sse":
            await _run_sse(server, init_options, config, port)
    finally:
        if _driver_pool is not None:
            await _driver_pool.close_all()
        elif _driver is not None:
            await _driver.close()


async def _run_sse(server: Server, init_options, config: Config, port: int) -> None:
    """Run MCP server with SSE transport via Starlette + uvicorn.

    The MCP library's ``SseServerTransport`` is ASGI-native (built on
    ``sse_starlette``, designed for Starlette). The previous
    implementation tried to bridge aiohttp requests into ASGI scopes —
    that was broken since inception (``request.scope`` doesn't exist on
    aiohttp) and the aiohttp→ASGI rewrite couldn't flush SSE chunks to
    the wire. Instead of fighting the framework mismatch, we run a
    proper Starlette ASGI app under uvicorn for the SSE transport.

    The stdio transport is unaffected — it stays on its existing path.
    """
    import uvicorn
    from mcp.server.sse import SseServerTransport
    from starlette.applications import Starlette
    from starlette.responses import Response
    from starlette.routing import Mount, Route

    warn_non_loopback(config.server.host, "SSE")

    sse = SseServerTransport("/messages")

    async def handle_sse(request):
        async with sse.connect_sse(request.scope, request.receive, request._send) as streams:
            await server.run(streams[0], streams[1], init_options, raise_exceptions=True)
        return Response()

    # handle_post_message is a raw ASGI app (scope, receive, send) that
    # sends its own HTTP response. Mount it directly — not as a Starlette
    # endpoint, which would try to wrap it in a second response.
    app = Starlette(
        routes=[
            Route("/sse", endpoint=handle_sse, methods=["GET"]),
            Mount("/messages", app=sse.handle_post_message),
        ]
    )

    logger.info("MCP SSE server on http://%s:%d/sse", config.server.host, port)

    uconfig = uvicorn.Config(
        app,
        host=config.server.host,
        port=port,
        log_level="warning",
        loop="asyncio",
    )
    uvi = uvicorn.Server(uconfig)
    await uvi.serve()


# ═══════════════════════════════════════════════════════════════
# CLI Entry Point
# ═══════════════════════════════════════════════════════════════


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="chatgpt-web2api-mcp",
        description="MCP server for ChatGPT-Web2API",
    )
    parser.add_argument("--config", "-c", help="Config file path")
    parser.add_argument(
        "--transport",
        choices=["stdio", "sse"],
        default="stdio",
        help="Transport layer (default: stdio)",
    )
    parser.add_argument("--port", type=int, default=8090, help="SSE port (default: 8090)")
    parser.add_argument("--cdp-port", type=int, help="Chrome CDP port (default: from config)")
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        stream=sys.stderr,
    )
    logging.getLogger("websockets").setLevel(logging.WARNING)
    logging.getLogger("mcp").setLevel(logging.WARNING)

    config = Config.load(args.config)
    if args.cdp_port:
        config.chrome.cdp_port = args.cdp_port

    try:
        asyncio.run(run_mcp(config, transport=args.transport, port=args.port))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
