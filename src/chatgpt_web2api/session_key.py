"""B1 session-key extraction — resolves a stable per-session identifier for
the MCP driver pool.

Gate 0.1 #1 (verified 2026-07-04): the SSE transport mints a uuid4
``session_id`` on every ``connect_sse`` call, propagated to the handler via
``server.request_context.request.query_params["session_id"]``. This is stable
for the connection's lifetime (required) but not across reconnect/restart
(accepted per B1 §0.3).

Resolution order (per B1 §6):
  1. SSE session_id from ``request_context.request.query_params`` → ``f"sse:{session_id}"``
  2. stdio transport → ``"stdio-singleton"``
  3. Pool disabled → ``"singleton"``
  4. Pool enabled + SSE + no session_id → ``None`` (fail-closed)

Never generates a random per-request key.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def current_mcp_session_key(
    server,
    *,
    transport: str,
    pool_enabled: bool,
) -> str | None:
    """Resolve the session key for the current MCP request.

    Args:
        server: The MCP ``Server`` instance (has ``request_context`` property).
        transport: ``"sse"`` or ``"stdio"``.
        pool_enabled: Whether the session pool is enabled.

    Returns:
        A stable session key string, or ``None`` if pool-enabled SSE has no
        resolvable session identity (fail-closed → caller returns
        ``mcp_session_identity_unavailable``).
    """
    # Try to extract the SSE session_id from the request context.
    try:
        ctx = server.request_context
        request = getattr(ctx, "request", None)
        if request is not None:
            query_params = getattr(request, "query_params", None)
            if query_params is not None:
                session_id = query_params.get("session_id")
                if session_id:
                    return f"sse:{session_id}"
    except LookupError:
        # Not in a request context (e.g., list_tools at startup, or outside
        # a handler). Fall through to transport/pool-mode defaults below.
        pass
    except Exception:
        logger.debug("session_key: unexpected error reading request_context", exc_info=True)

    # No session_id found — resolve by transport and pool mode.
    if transport == "stdio":
        return "stdio-singleton"

    if not pool_enabled:
        return "singleton"

    # Pool-enabled SSE with no session_id: fail-closed.
    logger.warning(
        "session_key: pool-enabled SSE but no session_id in request context; "
        "returning None (fail-closed → mcp_session_identity_unavailable)"
    )
    return None
