"""E2E through the REAL MCP server + a real MCP client, against a live account.

This is the highest-fidelity test: it exercises the entire stack —
  real MCP client  ->  real create_server()  ->  real CDPDriver  ->  Chrome  ->  ChatGPT
including the access-gating logic, not just the driver in isolation.

It creates real conversations (registered for cleanup) but only reads +
chats; no destructive ops here (those are in test_e2e_destructive.py).

Run with:  W2A_E2E_RUN=1 pytest tests/test_e2e_mcp.py -m e2e -v
"""

import asyncio

import pytest
from mcp.shared.memory import create_connected_server_and_client_session

import chatgpt_web2api.mcp_server as mod
from chatgpt_web2api.cdp_driver import CDPDriver

pytestmark = pytest.mark.e2e


async def _live_server(e2e_driver: CDPDriver):
    """Wire the module globals to the live driver and build the real server."""
    mod._driver = e2e_driver
    mod._config = mod.Config.load(None)
    mod._lock = asyncio.Lock()
    return mod.create_server()


async def test_mcp_client_lists_tools_over_live_stack(e2e_driver: CDPDriver):
    """A real MCP client sees the gated tool surface over the live stack."""
    server = await _live_server(e2e_driver)
    ctx = create_connected_server_and_client_session(server)
    session = await ctx.__aenter__()
    try:
        await session.initialize()
        resp = await session.list_tools()
        names = {t.name for t in resp.tools}
        # Safe reads + core chat are visible by default
        assert "list_models" in names
        assert "chat_completion" in names
        # Destructive tools must be hidden without W2A_ENABLE_DESTRUCTIVE
        assert "delete_memory" not in names
    finally:
        await ctx.__aexit__(None, None, None)


async def test_mcp_client_calls_list_models_live(e2e_driver: CDPDriver):
    """list_models round-trips through the real MCP protocol to ChatGPT."""
    server = await _live_server(e2e_driver)
    ctx = create_connected_server_and_client_session(server)
    session = await ctx.__aenter__()
    try:
        await session.initialize()
        result = await session.call_tool("list_models", {})
        assert result.isError is not True
        text = result.content[0].text
        # The response is JSON; it must mention a known model slug
        assert "gpt" in text.lower() or "auto" in text.lower(), \
            f"no model slug in response: {text[:200]!r}"
    finally:
        await ctx.__aexit__(None, None, None)


async def test_mcp_client_chats_live(
    e2e_driver: CDPDriver, e2e_created: dict
):
    """chat_completion works end-to-end through the real MCP server."""
    server = await _live_server(e2e_driver)
    ctx = create_connected_server_and_client_session(server)
    session = await ctx.__aenter__()
    try:
        await session.initialize()
        result = await session.call_tool(
            "chat_completion",
            {"message": "Reply with exactly this token: W2A-E2E-MCP-OK"},
        )
        assert result.isError is not True
        # chat_completion returns (TextContent, structuredContent); the
        # machine-readable payload is in structuredContent.
        data = result.structuredContent or {}
        cid = data.get("conversation_id", "")
        if cid:
            e2e_created["conversations"].add(cid)
        assert "W2A-E2E-MCP-OK" in str(data.get("content", "")), \
            f"marker missing from structured content: {data}"
    finally:
        await ctx.__aexit__(None, None, None)
